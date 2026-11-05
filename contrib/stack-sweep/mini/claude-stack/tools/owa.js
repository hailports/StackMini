#!/usr/bin/env node
/* Outlook READ bridge — mail + calendar straight from Outlook on the Web via the documented
 * REST API (/api/v2.0), reusing OWA's own first-party token from the logged-in CDP session.
 * No native app, no IT. Companion to owa_write.js (drafts/send). Replaces the native SQLite
 * reads (outlook_local/outlook_calendar_sqlite) so the Mac Outlook app can be uninstalled.
 *
 *   node tools/owa.js inbox [--top 25] [--unread]
 *   node tools/owa.js unread                       # true unread counts per folder
 *   node tools/owa.js unread-json                   # same, machine-readable
 *   node tools/owa.js msgid-probe [--top 5]         # InternetMessageId/ConversationId, for the
 *                                                   # identity migration (docs/OUTLOOK_IDENTITY_MIGRATION.md)
 *   node tools/owa.js attention-json [--days 14] [--limit 25]
 *                                                   # raw conversation heads + replied-after, for
 *                                                   # get_attention_items_owa (same migration)
 *   node tools/owa.js agenda                        # today's events
 *   node tools/owa.js upcoming [--days 7]
 *   node tools/owa.js calendar-json [--hours 2]     # structured upcoming work events
 *   node tools/owa.js calendar-sync-json [--back-days 7] [--forward-days 60]
 *                                                   # paged full-detail mirror feed (calendar_mirror.py)
 *   node tools/owa.js search-events --q "UAT" [--days 30]
 *   node tools/owa.js read --match "subject text"   # newest matching message, full body
 *   node tools/owa.js index [--top 2500] [--body-top 500]
 *   node tools/owa.js bodies --ids-json '["REST-ID", ...]'
 */
const { chromium } = require('/Users/operator/.npm-global/lib/node_modules/openclaw/node_modules/playwright-core');
const https = require('https');
const fs = require('fs');
const { execFile, execFileSync } = require('child_process');

// Bridge lock handle — see the acquire in the main IIFE below. Released on every exit path,
// including the SIGTERM launchd sends when it kills a slow job; without that the lockfile would
// outlive its holder and the next caller would have to wait out the staleness ceiling.
let __bridge = null;
function __release() {
  try { require('./bridge_lock.js').release(__bridge); } catch (e) { /* nothing to release */ }
  __bridge = null;
}
process.once('SIGINT', () => { __release(); process.exit(130); });
process.once('SIGTERM', () => { __release(); process.exit(143); });
process.once('exit', __release);
const { grokPrimary, resolveCdp, safariOpsPrimary } = require('./grok_cdp_endpoint.js');
const CDP = resolveCdp('OWA_WEB_CDP', 'http://127.0.0.1:9227');
const MANAGED_CDP = 'http://127.0.0.1:9227';
const CACHE_ONLY = safariOpsPrimary();
const BRIDGECTL = process.env.OWA_BRIDGECTL || (process.env.HOME || '') + '/operator-drafter/bridgectl.sh';
const TOKEN_SURFACE = 'https://outlook.office.com/calendar/view/day';
// Readers and writers share one authenticated browser transport. A healthy
// TCP port does not mean its DevTools websocket can safely accept concurrent
// Playwright clients, so serialize only token capture on the writer's lock.
const LOCK_DIR = (process.env.HOME || '') + '/claude-stack/data/runtime/owa-write.lockdir';
const INBOX_WRITER_LOCK = (process.env.HOME || '') + '/.stack-locks/inbox_reply_draft.lock';
const BACKGROUND_READER = /^(?:1|true|yes)$/i.test(process.env.OWA_BACKGROUND_READER || '');
const boundedInt = (raw, fallback, low, high) => {
  const value = Number.parseInt(raw || String(fallback), 10);
  return Number.isFinite(value) ? Math.min(high, Math.max(low, value)) : fallback;
};
// Playwright's default connect timeout is 30s. A busy OWA renderer (a pegged Edge helper
// pinned by memory pressure) needs longer to answer the handshake, and the timeout string
// is indistinguishable from a signed-out profile downstream — outlook_transport_guard then
// pkills a perfectly healthy signed-in browser and relaunches it headful into operator's face.
// Background probes/indexers are different: the inbox writer is the load-bearing lane, so
// their entire token-capture lease must fit inside its 60s queue budget even when Playwright
// is blocked in native CDP code. 8s connect + 6s navigation + 3s token wait = 17s maximum.
const CDP_CONNECT_MS = boundedInt(
  process.env.OWA_CDP_CONNECT_MS,
  BACKGROUND_READER ? 8000 : 90000,
  1000,
  BACKGROUND_READER ? 8000 : 180000,
);
const NAV_TIMEOUT_MS = boundedInt(
  process.env.OWA_NAV_TIMEOUT_MS,
  BACKGROUND_READER ? 6000 : 45000,
  1000,
  BACKGROUND_READER ? 6000 : 90000,
);
const TOKEN_WAIT_TICKS = boundedInt(
  process.env.OWA_TOKEN_WAIT_TICKS,
  BACKGROUND_READER ? 3 : 32,
  1,
  BACKGROUND_READER ? 3 : 60,
);
const CDP_OPTIONS = { noDefaults: true, timeout: CDP_CONNECT_MS };
const A = process.argv.slice(2);
const cmd = A[0];
const arg = (k, d = '') => { const i = A.indexOf('--' + k); return i >= 0 ? A[i + 1] : d; };
const flag = k => A.includes('--' + k);
const SUPPORTED_COMMANDS = new Set([
  'agenda', 'attention-json', 'bodies', 'body', 'calendar-json', 'calendar-sync-json',
  'drafts-json', 'find-by-subject', 'inbox', 'index', 'msgid-probe', 'read',
  'search-events', 'sent-drafts-json', 'unread', 'unread-json', 'upcoming', 'zoom-summaries',
  // 2026-10-16: read-only attachment text for the work brain; its handler existed but was never allowlisted.
  ...(process.env.OWA_ATTACH_TEXTS_OFF === '1' ? [] : ['attachment-texts']),
]);

// A continuation is bearer-authenticated once it reaches `get`, so never
// replay an arbitrary URL from state.  The unread scanner owns exactly this
// Inbox query and retains its shape around OWA's server continuation. OWA can
// emit either an opaque $skiptoken or a positive numeric $skip. Keeping that
// continuation, rather than a ReceivedDateTime cutoff, is what makes page
// boundaries lossless when several messages share the same timestamp.
const UNREAD_CURSOR_PATH = '/api/v2.0/me/mailfolders/inbox/messages';
const UNREAD_CURSOR_MAX = 8192;
const UNREAD_CURSOR_SELECT = 'Id,InternetMessageId,ConversationId,Subject,From,ToRecipients,CcRecipients,'
  + 'ReceivedDateTime,IsRead,Flag,BodyPreview,HasAttachments';
const unreadCursorError = raw => {
  if (typeof raw !== 'string' || !raw || raw.length > UNREAD_CURSOR_MAX || /[\x00-\x1f\x7f]/.test(raw)) return null;
  let u;
  try {
    // A relative path is allowed; an absolute URL must be the exact OWA host.
    if (!raw.startsWith('/')) {
      u = new URL(raw);
      if (u.protocol !== 'https:' || u.host.toLowerCase() !== 'outlook.office.com') return null;
    } else {
      u = new URL(raw, 'https://outlook.office.com');
    }
  } catch { return null; }
  if (u.protocol !== 'https:' || u.host.toLowerCase() !== 'outlook.office.com'
      || u.username || u.password || u.hash || u.pathname !== UNREAD_CURSOR_PATH) return null;
  const allowed = new Set(['$top', '$orderby', '$filter', '$select', '$skiptoken', '$skip']);
  const values = new Map();
  for (const [key, value] of u.searchParams) {
    if (!allowed.has(key) || !value || values.has(key)) return null;
    values.set(key, value);
  }
  return { u, values };
};
const validateUnreadCursor = raw => {
  const parsed = unreadCursorError(raw);
  if (!parsed) return null;
  const { u, values } = parsed;
  const topRaw = values.get('$top') || '';
  const top = Number(topRaw);
  const hasSkiptoken = values.has('$skiptoken');
  const hasSkip = values.has('$skip');
  const skipRaw = values.get('$skip') || '';
  if (values.size !== 5 || !/^[1-9]\d*$/.test(topRaw)
      || !Number.isSafeInteger(top) || top > 500
      || values.get('$orderby') !== 'ReceivedDateTime desc'
      || values.get('$filter') !== 'IsRead eq false'
      || values.get('$select') !== UNREAD_CURSOR_SELECT
      || hasSkiptoken === hasSkip
      || (hasSkip && (!/^[1-9]\d*$/.test(skipRaw)
        || !Number.isSafeInteger(Number(skipRaw))))) return null;
  // The cursor stays relative even when OWA emitted an absolute nextLink.
  return u.pathname + u.search;
};
const unreadCursorFromNextLink = (raw, top) => {
  const parsed = unreadCursorError(raw);
  const validated = validateUnreadCursor(raw);
  if (!parsed || !validated || Number(parsed.values.get('$top')) !== top) return null;
  const params = new URLSearchParams({
    '$top': String(top), '$orderby': 'ReceivedDateTime desc',
    '$filter': 'IsRead eq false', '$select': UNREAD_CURSOR_SELECT,
  });
  const continuation = parsed.values.has('$skiptoken') ? '$skiptoken' : '$skip';
  params.set(continuation, parsed.values.get(continuation));
  const cursor = UNREAD_CURSOR_PATH + '?' + params.toString();
  return validateUnreadCursor(cursor);
};

function ensureManagedBridge() {
  if (grokPrimary()) return;
  if (CDP.replace(/\/+$/, '') !== MANAGED_CDP) return;
  const output = execFileSync(BRIDGECTL, ['outlook', 'ensure'], {
    encoding: 'utf8',
    timeout: 45000,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (String(output || '').trim().split(/\r?\n/).pop() !== 'UP') {
    throw new Error(`unexpected bridge status: ${String(output || '').trim() || 'empty'}`);
  }
  const heartbeat = setInterval(() => {
    const child = execFile(BRIDGECTL, ['outlook', 'touch'], { timeout: 15000 }, () => {});
    child.unref();
  }, 30000);
  heartbeat.unref();
}

const configuredHttpTimeout = Number.parseInt(process.env.OWA_HTTP_TIMEOUT_MS || '30000', 10);
const HTTP_TIMEOUT_MS = Number.isFinite(configuredHttpTimeout)
  ? Math.min(120000, Math.max(5000, configuredHttpTimeout))
  : 30000;
const get = (token, p) => new Promise(res => {
  let settled = false;
  const finish = value => {
    if (settled) return;
    settled = true;
    res(value);
  };
  const req = https.get(
    'https://outlook.office.com' + p,
    { headers: { Authorization: 'Bearer ' + token, Accept: 'application/json' } },
    r => {
      let d = '';
      r.on('data', c => d += c);
      r.on('aborted', () => finish({ s: 0, err: 'OWA HTTP response aborted' }));
      r.on('end', () => {
        try { finish({ s: r.statusCode, j: JSON.parse(d) }); }
        catch { finish({ s: r.statusCode, raw: d.slice(0, 200) }); }
      });
    },
  );
  req.setTimeout(HTTP_TIMEOUT_MS, () => {
    req.destroy(new Error(`OWA HTTP GET timed out after ${HTTP_TIMEOUT_MS}ms`));
  });
  req.on('error', e => finish({ s: 0, err: e.message }));
});
async function keepUnread(token, id) {
  if (!id) return;
  const obs = await get(token, `/api/v2.0/me/messages/${encodeURIComponent(id)}?$select=Id,IsRead`);
  if (!(obs.s >= 200 && obs.s < 300) || !(obs.j) || obs.j.IsRead !== true) return;
  const data = JSON.stringify({IsRead: false});
  await new Promise(res => {
    const req = https.request('https://outlook.office.com/api/v2.0/me/messages/' + encodeURIComponent(id), {
      method: 'PATCH',
      headers: {
        Authorization: 'Bearer ' + token,
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(data),
      },
    }, r => { r.resume(); r.on('end', () => res()); });
    req.on('error', () => res());
    req.write(data);
    req.end();
  });
}
const writeOut = value => new Promise((resolve, reject) => {
  process.stdout.write(String(value), err => err ? reject(err) : resolve());
});
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const fmt = s => { try { return new Date(s).toISOString().replace('T', ' ').slice(0, 16); } catch { return String(s || ''); } };
const strip = h => (h || '').replace(/<style[\s\S]*?<\/style>/gi, ' ').replace(/<[^>]+>/g, ' ').replace(/&nbsp;/gi, ' ').replace(/&amp;/gi, '&').replace(/\s+/g, ' ').trim();
const prohibited = /\b(?:github|fullsand|tavantfull)\b/i;

const liveInboxWriterPid = () => {
  let pid = 0;
  try { pid = Number.parseInt(fs.readFileSync(INBOX_WRITER_LOCK, 'utf8').trim(), 10); }
  catch { return 0; }
  if (pid <= 1) return 0;
  try { process.kill(pid, 0); }
  catch { return 0; }
  return pid;
};
const mustDeferToInboxWriter = () => {
  const writerPid = liveInboxWriterPid();
  // owa.js launched directly by the singleton-owning inbox process is the writer's
  // own source read and must retain priority. Every unrelated reader yields.
  return writerPid > 1 && writerPid !== process.pid && writerPid !== process.ppid;
};

let lockHeld = false;
const releaseLock = () => {
  if (!lockHeld) return;
  try { fs.unlinkSync(`${LOCK_DIR}/owner`); } catch {}
  try { fs.rmdirSync(LOCK_DIR); } catch {}
  lockHeld = false;
};
process.on('exit', releaseLock);
process.on('SIGINT', () => { releaseLock(); process.exit(130); });
process.on('SIGTERM', () => { releaseLock(); process.exit(143); });
const acquireLock = async () => {
  fs.mkdirSync((process.env.HOME || '') + '/claude-stack/data/runtime', {recursive:true, mode:0o700});
  const started = Date.now();
  const lockWaitMs = boundedInt(
    process.env.OWA_LOCK_WAIT_MS,
    BACKGROUND_READER ? 1000 : 15000,
    1000,
    BACKGROUND_READER ? 1000 : 60000,
  );
  while (Date.now() - started < lockWaitMs) {
    if (mustDeferToInboxWriter()) return false;
    try {
      fs.mkdirSync(LOCK_DIR, {mode:0o700});
      fs.writeFileSync(`${LOCK_DIR}/owner`, `${process.pid}\n`, {mode:0o600, flag:'wx'});
      lockHeld = true;
      return true;
    } catch (error) {
      if (error && error.code !== 'EEXIST') return false;
      let owner = 0;
      try { owner = Number.parseInt(fs.readFileSync(`${LOCK_DIR}/owner`, 'utf8').trim(), 10); } catch {}
      let alive = false;
      if (owner > 1) {
        try { process.kill(owner, 0); alive = true; } catch {}
      } else {
        // mkdir precedes the owner write by only a few milliseconds. Preserve
        // that live window, but reap an empty directory left by a killed peer.
        try {
          const empty = fs.readdirSync(LOCK_DIR).length === 0;
          alive = !empty || Date.now() - fs.statSync(LOCK_DIR).mtimeMs < 2000;
        } catch {}
      }
      if (!alive) {
        try { fs.unlinkSync(`${LOCK_DIR}/owner`); } catch {}
        try { fs.rmdirSync(LOCK_DIR); } catch {}
        continue;
      }
      await sleep(250);
    }
  }
  return false;
};

const exitBusy = async reason => {
  await writeOut(JSON.stringify({
    ok: false,
    error: reason,
    failure_kind: 'bridge_busy',
  }) + '\n');
  process.exit(1);
};

// OPT-IN token reuse (OWA_TOKEN_CACHE=1). The bearer OWA hands us stays valid for the better
// part of an hour and every REST call after capture is plain HTTPS — so a minute-cadence reader
// does not need the browser every tick. Cached, it touches CDP roughly once an hour instead of
// once a minute, which is what keeps it out of the inbox writer's way. Off by default: existing
// callers keep the exact capture-every-run behaviour they have today.
const TOKEN_CACHE_ENABLED = CACHE_ONLY || grokPrimary() || /^(?:1|true|yes)$/i.test(process.env.OWA_TOKEN_CACHE || '');
const TOKEN_CACHE_FILE = (process.env.HOME || '') + '/claude-stack/data/runtime/owa-token-cache.json';
// Safari remints are infrequent — allow up to 2h local cache when CACHE_ONLY.
const TOKEN_CACHE_TTL_MS = boundedInt(
  process.env.OWA_TOKEN_CACHE_TTL_S,
  CACHE_ONLY ? 7200 : 2400,
  60,
  7200,
) * 1000;
const TOKEN_CACHE_UNDER = (process.env.HOME || '') + '/claude-stack/data/runtime/owa_token_cache.json';
const readCachedToken = () => {
  if (!TOKEN_CACHE_ENABLED) return null;
  try {
    const c = JSON.parse(fs.readFileSync(TOKEN_CACHE_FILE, 'utf8'));
    if (c && c.token && Date.now() - c.at < TOKEN_CACHE_TTL_MS) return c.token;
  } catch {}
  // Safari remint / owa_write write the underscore cache (expiresAt). Accept it locally.
  try {
    const c = JSON.parse(fs.readFileSync(TOKEN_CACHE_UNDER, 'utf8'));
    if (c && c.token && Number(c.expiresAt) > Date.now()) {
      try { writeCachedToken(c.token); } catch {}
      return c.token;
    }
  } catch {}
  return null;
};
const writeCachedToken = token => {
  if (!TOKEN_CACHE_ENABLED || !token) return;
  try {
    fs.mkdirSync(TOKEN_CACHE_FILE.replace(/\/[^/]+$/, ''), { recursive: true });
    fs.writeFileSync(TOKEN_CACHE_FILE, JSON.stringify({ token, at: Date.now() }), { mode: 0o600 });
  } catch {}
  // Keep owa_write's cache warm too (local Qwen path).
  try {
    let exp = Date.now() + 600000;
    try {
      const mid = JSON.parse(Buffer.from(String(token).split('.')[1], 'base64').toString('utf8'));
      if (mid && mid.exp) exp = Number(mid.exp) * 1000;
    } catch {}
    fs.writeFileSync(TOKEN_CACHE_UNDER, JSON.stringify({ token, expiresAt: exp }), { mode: 0o600 });
  } catch {}
};
const dropCachedToken = () => { try { fs.unlinkSync(TOKEN_CACHE_FILE); } catch {} };

// Playwright's connectOverCDP attaches to EVERY target in the browser. Post-cutover OWA serves
// from outlook.cloud.microsoft and spawns mail/sw.js as a service worker that never answers
// Target.detachFromTarget — so that attach hangs to the full timeout and every reader reports
// "NOT logged in / OWA profile down" against a perfectly healthy signed-in profile. Relaunching
// onto /calendar used to dodge it; the cutover moved calendar onto the same host, so it does not
// any more (verified 2026-08-06: a freshly repaired profile spawned the worker immediately).
// Talking straight to the calendar page's own DevTools socket never enumerates or attaches to the
// service worker, so this path cannot wedge on it. Playwright stays as the fallback.
// Passive mode observes the calendar tab's own REST traffic and never reloads it, so it takes no
// lease and disturbs nothing — safe to run while the inbox writer owns the bridge. It can only
// fail by finding no bearer inside its budget, which costs the caller a skipped tick.
const FORCE_TOKEN_REFRESH = /^(?:1|true|yes)$/i.test(process.env.OWA_FORCE_TOKEN_REFRESH || '');
const RAW_PASSIVE = !FORCE_TOKEN_REFRESH && (grokPrimary() || /^(?:1|true|yes)$/i.test(process.env.OWA_RAW_PASSIVE || ''));
const RAW_CAPTURE_MS = boundedInt(process.env.OWA_RAW_CAPTURE_MS, 25000, 3000, 60000);
const RAW_RELOAD_AFTER_MS = boundedInt(process.env.OWA_RAW_RELOAD_AFTER_MS, 8000, 1000, 30000);
const httpJson = url => new Promise(resolve => {
  const req = require('http').get(url, r => {
    let d = '';
    r.on('data', c => d += c);
    r.on('end', () => { try { resolve(JSON.parse(d)); } catch { resolve(null); } });
  });
  req.setTimeout(5000, () => req.destroy());
  req.on('error', () => resolve(null));
});
const captureTokenRawCDP = async () => {
  const targets = await httpJson(CDP.replace(/\/+$/, '') + '/json/list');
  if (!Array.isArray(targets)) return null;
  const isOwa = t => t.type === 'page' && /outlook\.(?:office|cloud\.microsoft)/i.test(t.url || '');
  const target = targets.find(t => isOwa(t) && /\/calendar/i.test(t.url)) || targets.find(isOwa);
  if (!target || !target.webSocketDebuggerUrl) return null;
  return new Promise(resolve => {
    let ws, id = 0, done = false;
    const finish = v => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      try { ws.close(); } catch {}
      resolve(v);
    };
    const timer = setTimeout(() => finish(null), RAW_CAPTURE_MS);
    try { ws = new WebSocket(target.webSocketDebuggerUrl); } catch { return finish(null); }
    ws.onerror = () => finish(null);
    ws.onclose = () => finish(null);
    ws.onopen = () => {
      ws.send(JSON.stringify({ id: ++id, method: 'Network.enable' }));
      ws.send(JSON.stringify({ id: ++id, method: 'Page.enable' }));
      // OWA re-issues REST calls on its own polling cadence; the reload only exists so a quiet
      // tab still yields a bearer inside the budget.
      if (!RAW_PASSIVE) {
        setTimeout(() => { if (!done) { try { ws.send(JSON.stringify({ id: ++id, method: 'Page.reload' })); } catch {} } }, RAW_RELOAD_AFTER_MS);
      }
    };
    ws.onmessage = ev => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m.method !== 'Network.requestWillBeSent') return;
      const req = m.params && m.params.request;
      if (!req || !/outlook\.(?:cloud\.microsoft|office)/i.test(req.url || '')) return;
      const auth = (req.headers || {}).Authorization || (req.headers || {}).authorization || '';
      if (auth.startsWith('Bearer ')) finish(auth.slice(7));
    };
  });
};

(async () => {
  if (!SUPPORTED_COMMANDS.has(cmd)) {
    console.log(JSON.stringify({ ok: false, error: 'unsupported command', sent: false }));
    process.exit(1);
  }
  if (!CACHE_ONLY) {
    const dormant = await require('./owa_dormancy.js').preflight({ cdp: CDP });
    if (dormant) {
      console.log(JSON.stringify(dormant));
      process.exit(0);
    }
  }
  // One writer at a time on CDP 18820. Twenty scheduled jobs shell out to this file and every
  // one of them attaches to the same tab; overlapping runs interleave navigations and wedge the
  // bridge. Locking HERE covers all of them, including the Python callers, without touching
  // twenty call sites. Re-entrant, so a parent that already holds `owa` passes straight through.
  // CACHE_ONLY (Safari/Qwen): REST-only — no browser lock, no Grok CDP.
  if (!CACHE_ONLY) {
  try {
    __bridge = require('./bridge_lock.js').acquire('owa', Number(process.env.OWA_LOCK_WAIT_S || 120));
  } catch (error) {
    // Saturated or wedged: exit non-zero WITHOUT touching the browser. Proceeding unlocked is
    // the bug this exists to prevent, and every caller here is periodic — the next run retries.
    console.log('owa bridge busy: ' + (error && error.message));
    process.exit(3);
  }
  try {
    ensureManagedBridge();
  } catch (error) {
    console.log('NOT logged in / managed OWA bridge unavailable: ' + (error && error.message));
    process.exit(2);
  }
  }
// noDefaults: Playwright's default-context initialisation is not supported by Edge's
// default browser context and leaves a stray tab behind on every connect. owa_write runs
// several times a minute from the draft lanes, so that leaked 14 identical OWA tabs
// (~150MB each) between reaps on 2026-07-28. owa_reauth.js/operator_fetch.js already
// passed it; these did not.
  const cachedToken = readCachedToken();
  if (CACHE_ONLY) {
    if (!cachedToken) {
      console.log(JSON.stringify({
        ok: false,
        failure_kind: 'cache_miss',
        error: 'OWA_CACHE_ONLY: no Safari token — remint via ops_safari_feeds.remint_owa_token',
      }));
      process.exit(3);
    }
    var token = cachedToken;
  } else {
  const passiveToken = (!cachedToken && RAW_PASSIVE) ? await captureTokenRawCDP() : null;
  if (passiveToken) writeCachedToken(passiveToken);
  if (!cachedToken && !passiveToken && mustDeferToInboxWriter()) {
    await exitBusy('OWA bridge is busy; inbox draft writer has priority');
  }
  if (!cachedToken && !passiveToken && !(await acquireLock())) {
    await exitBusy('OWA bridge is busy; single-owner lock unavailable');
  }
  var token = cachedToken || passiveToken;
  if (!token) {
    if (mustDeferToInboxWriter()) {
      await exitBusy('OWA bridge is busy; inbox draft writer has priority');
    }
    token = await captureTokenRawCDP();
    if (token) { writeCachedToken(token); releaseLock(); }
  }
  // ---- Playwright fallback capture (skipped on a cache or raw-CDP hit) ----
  if (!token) {
  if (grokPrimary()) {
    console.log('NOT logged in / Grok OWA token capture failed (raw page socket; Playwright skipped)');
    process.exit(3);
  }
  if (mustDeferToInboxWriter()) {
    await exitBusy('OWA bridge is busy; inbox draft writer has priority');
  }
  let b; try { b = await chromium.connectOverCDP(CDP, CDP_OPTIONS); } catch (e) { console.log('NOT logged in / OWA profile down: ' + e.message); process.exit(2); }
  if (mustDeferToInboxWriter()) {
    try { b._connection.close(); } catch {}
    await exitBusy('OWA bridge is busy; inbox draft writer has priority');
  }
  let ctx = b.contexts()[0];
  if (!ctx) { console.log('OWA profile has no default context'); process.exit(2); }
  let page = ctx.pages().find(p => /outlook\.(?:office|cloud\.microsoft)\/calendar/i.test(p.url()))
    || ctx.pages().find(p => /outlook\.(?:office|cloud\.microsoft)/.test(p.url()))
    || ctx.pages()[0];
  if (!page) page = await ctx.newPage();
  const watch = p => p.on('request', q => { const h = q.headers(); if (h.authorization && h.authorization.startsWith('Bearer ') && /outlook\.(cloud\.microsoft|office)/.test(q.url())) token = h.authorization.slice(7); });
  watch(page);
  try {
    // A mail-page reload can render the selected reading pane and consume an
    // unread badge. Calendar emits the same first-party REST bearer without
    // opening a message.
    await page.goto(TOKEN_SURFACE, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT_MS });
  } catch {}
  for (let i = 0; i < TOKEN_WAIT_TICKS && !token; i++) {
    if (mustDeferToInboxWriter()) {
      try { b._connection.close(); } catch {}
      await exitBusy('OWA bridge is busy; inbox draft writer has priority');
    }
    await page.waitForTimeout(1000);
  }
  if (!token && !BACKGROUND_READER) {
    // One bounded reattach catches the Edge cold-start case where the first
    // client connects before OWA's default context finishes appearing.
    try { b._connection.close(); } catch {}
    try { b = await chromium.connectOverCDP(CDP, CDP_OPTIONS); }
    catch (e) { console.log('NOT logged in / OWA profile down: ' + e.message); process.exit(2); }
    ctx = b.contexts()[0];
    if (!ctx) { console.log('OWA profile has no default context'); process.exit(2); }
    page = ctx.pages().find(p => /outlook\.(?:office|cloud\.microsoft)\/calendar/i.test(p.url()))
      || ctx.pages().find(p => /outlook\.(?:office|cloud\.microsoft)/.test(p.url()))
      || ctx.pages()[0];
    if (!page) page = await ctx.newPage();
    watch(page);
    try {
      await page.goto(TOKEN_SURFACE, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT_MS });
    } catch {}
    for (let i = 0; i < 16 && !token; i++) await page.waitForTimeout(1000);
  }
  // connectOverCDP attaches to the persistent authenticated browser. Calling
  // browser.close() here shuts that shared OWA profile down for every reader.
  // Drop only this client's CDP socket, then release the transport lock before
  // potentially long REST pagination/body reads.
  try { b._connection.close(); } catch {}
  if (!token) { console.log('NOT logged in — re-login the OWA profile'); process.exit(3); }
  writeCachedToken(token);
  releaseLock();
  }
  } // end !CACHE_ONLY token capture
  // ---- end token capture; everything below is plain HTTPS REST ----

  const isoDay = off => { const d = new Date(); d.setUTCDate(d.getUTCDate() + off); return d.toISOString(); };

  if (cmd === 'unread') {
    const r = await get(token, '/api/v2.0/me/mailfolders?$top=50&$select=DisplayName,UnreadItemCount,TotalItemCount');
    if (r.s !== 200) return console.log('error ' + r.s);
    for (const f of (r.j.value || [])) console.log(`${f.DisplayName}: ${f.UnreadItemCount} unread / ${f.TotalItemCount}`);
    return;
  }
  if (cmd === 'msgid-probe') {
    const r = await get(token, `/api/v2.0/me/mailfolders/inbox/messages?$top=${Number(arg('top', '5'))}&$orderby=ReceivedDateTime desc&$select=Id,InternetMessageId,ConversationId,Subject,ReceivedDateTime`);
    if (r.s !== 200) { await writeOut(JSON.stringify({ error: r.s, raw: r.raw || '' }) + '\n'); return; }
    await writeOut(JSON.stringify((r.j.value || []).map(m => ({
      internet_message_id: m.InternetMessageId || null,
      conversation_id: m.ConversationId || null,
      subject: (m.Subject || '').slice(0, 40),
      received: m.ReceivedDateTime,
    }))) + '\n');
    return;
  }
  if (cmd === 'unread-json') {
    // Structured twin of `unread`. outlook_local.get_unread_summary consumes this
    // instead of scraping the human line format, so folder names containing ':'
    // or ' unread / ' cannot corrupt the counts.
    const r = await get(token, '/api/v2.0/me/mailfolders?$top=50&$select=DisplayName,UnreadItemCount,TotalItemCount');
    if (r.s !== 200) { await writeOut(JSON.stringify({ error: r.s, raw: r.raw || '' }) + '\n'); return; }
    const folders = (r.j.value || []).map(f => ({
      folder_name: String(f.DisplayName || ''),
      unread_count: Number(f.UnreadItemCount || 0),
      total_count: Number(f.TotalItemCount || 0),
    })).filter(f => f.folder_name);
    await writeOut(JSON.stringify({ folders }) + '\n');
    return;
  }
  if (cmd === 'attention-json') {
    // Phase 2 of the identity migration (docs/OUTLOOK_IDENTITY_MIGRATION.md): the OWA twin of
    // outlook_local.get_attention_items' SQL read. It returns RAW rows only -- every
    // classification decision stays in Python on the existing shared gate, so any behavior
    // delta between the two readers is attributable to the transport and nothing else.
    const days = Math.max(1, Number(arg('days', '14')) || 14);
    const limit = Math.min(500, Math.max(1, Number(arg('limit', '25')) || 25));
    const metadataOnly = flag('metadata-only');
    const unreadOnly = flag('unread-only');
    const strictPage = flag('strict-page');
    const beforeCursorRaw = arg('before-cursor', '');
    const beforeReceivedRaw = arg('before-received', '');
    if (strictPage && !unreadOnly) {
      await writeOut(JSON.stringify({ error: 'strict_page_requires_unread_only' }) + '\n'); return;
    }
    if (beforeCursorRaw && beforeReceivedRaw) {
      await writeOut(JSON.stringify({
        error: 'ambiguous_unread_cursor', failure_kind: 'invalid_unread_cursor',
      }) + '\n'); return;
    }
    const beforeCursor = beforeCursorRaw ? validateUnreadCursor(beforeCursorRaw) : null;
    if (beforeCursorRaw && !beforeCursor) {
      await writeOut(JSON.stringify({
        error: 'owa_unread_cursor_invalid', failure_kind: 'invalid_unread_cursor',
      }) + '\n'); return;
    }
    const beforeReceived = beforeReceivedRaw ? Number(beforeReceivedRaw) : 0;
    if (beforeReceivedRaw && (!Number.isFinite(beforeReceived) || beforeReceived <= 0)) {
      await writeOut(JSON.stringify({
        error: 'owa_unread_timestamp_invalid', failure_kind: 'invalid_unread_timestamp',
      }) + '\n'); return;
    }
    const since = new Date(Date.now() - days * 86400000).toISOString();
    const sel = UNREAD_CURSOR_SELECT;
    // The native reader proves account identity from AccountsExchange. Read it once here so
    // the OWA rows carry the same discovery-context field instead of an assumed address.
    const meResp = await get(token, '/api/v2.0/me?$select=EmailAddress');
    const account = (meResp.s === 200 && meResp.j && meResp.j.EmailAddress) || '';
    // The regular attention view still over-reads for conversation dedupe. The
    // unread scanner instead requests one exact server page: advancing an OData
    // nextLink after a client-side slice would silently skip mail.
    const want = strictPage ? limit : Math.max(100, limit * 5);
    const rows = [];
    let p = beforeCursor;
    if (!p) {
      let filter = unreadOnly ? 'IsRead eq false' : `ReceivedDateTime ge ${since}`;
      // Timestamp resume is migration-only. Once a server cursor exists it
      // always wins, because timestamp ties are inherently lossy.
      if (unreadOnly && beforeReceived) {
        filter += ` and ReceivedDateTime lt ${new Date(beforeReceived * 1000).toISOString()}`;
      }
      const top = strictPage ? limit : 100;
      p = `${UNREAD_CURSOR_PATH}?$top=${top}&$orderby=ReceivedDateTime desc`
        + `&$filter=${encodeURIComponent(filter)}&$select=${encodeURIComponent(sel)}`;
    }
    let nextCursor = null;
    while (p && rows.length < want) {
      const r = await get(token, p);
      if (r.s !== 200) { await writeOut(JSON.stringify({ error: r.s, raw: r.raw || '' }) + '\n'); return; }
      for (const m of (r.j.value || []).slice(0, want - rows.length)) rows.push(m);
      const next = r.j['@odata.nextLink'];
      if (strictPage) {
        if (next) {
          nextCursor = unreadCursorFromNextLink(next, limit);
          if (!nextCursor) {
            await writeOut(JSON.stringify({
              error: 'owa_unread_nextlink_invalid', failure_kind: 'invalid_unread_cursor',
            }) + '\n'); return;
          }
        }
        break;
      }
      p = next ? next.replace(/^https:\/\/outlook\.office\.com/, '') : null;
    }

    // Metadata-only unread discovery is a locator, not a reply-retirement
    // decision.  The draft lane runs reply-inspect against the exact source
    // Message-ID/account/current head before authoring, so making discovery
    // depend on a broad Sent Items query creates an availability dependency
    // without adding proof.  Keep Sent proof here for callers that consume it;
    // explicitly defer it only for the passive strict-page contract.
    const sentReplyProofDeferred = strictPage && metadataOnly;
    const sentBy = new Map();
    if (!sentReplyProofDeferred) {
      // One pass over recent Sent Items instead of a per-conversation filter
      // query. The native reader answers this from the same local table.
      let sp = `/api/v2.0/me/mailfolders/sentitems/messages?$top=100&$orderby=SentDateTime desc`
             + `&$filter=SentDateTime ge ${since}&$select=ConversationId,SentDateTime`;
      let sentPages = 0;
      while (sp && sentPages < 10) {
        const r = await get(token, sp);
        if (r.s !== 200) break;
        for (const m of (r.j.value || [])) {
          const c = m.ConversationId; if (!c) continue;
          const t = Date.parse(m.SentDateTime || '') || 0;
          if (t > (sentBy.get(c) || 0)) sentBy.set(c, t);
        }
        const next = r.j['@odata.nextLink']; sp = next ? next.replace(/^https:\/\/outlook\.office\.com/, '') : null;
        sentPages++;
      }
    }

    // Deliberately NOT capped at `limit` here. The native reader dedupes up to `want` rows,
    // then keeps the first `limit` that have an attention reason -- so capping heads at
    // `limit` before the reason filter starves the result. Measured 2026-07-30: capping here
    // returned 2 items against the native reader's 15 over the same window.
    const seen = new Set();
    const heads = [];
    for (const m of rows) {
      const conv = m.ConversationId || m.Id;
      if (seen.has(conv)) continue;
      seen.add(conv);
      heads.push(m);
      if (heads.length >= want) break;
    }

    // The normal Sent scan is deliberately recent and capped. That cannot be
    // used to declare an older unread message unanswered: its true reply may
    // be older than the scan window. For cursor pages, prove the latest Sent
    // item for every otherwise-unproven conversation directly, with no age
    // cutoff. Failure is fail-closed rather than a false unanswered verdict.
    if (strictPage && !sentReplyProofDeferred) {
      for (const m of heads) {
        const conv = m.ConversationId || m.Id;
        const received = Date.parse(m.ReceivedDateTime || '') || 0;
        if (!conv || (sentBy.get(conv) || 0) > received) continue;
        const filter = `ConversationId eq '${String(conv).replace(/'/g, "''")}'`;
        const query = new URLSearchParams({
          '$top': '1', '$orderby': 'SentDateTime desc', '$filter': filter,
          '$select': 'ConversationId,SentDateTime',
        });
        const r = await get(token, `/api/v2.0/me/mailfolders/sentitems/messages?${query.toString()}`);
        if (r.s !== 200 || !r.j || !Array.isArray(r.j.value)) {
          await writeOut(JSON.stringify({
            error: 'sent_reply_proof_unavailable',
            failure_kind: 'sent_reply_proof_unavailable',
          }) + '\n'); return;
        }
        const latest = r.j.value[0];
        const sentAt = latest ? (Date.parse(latest.SentDateTime || '') || 0) : 0;
        if (sentAt) sentBy.set(conv, sentAt);
      }
    }

    const addr = x => (x && x.EmailAddress && x.EmailAddress.Address) || '';
    const out = heads.map(m => {
      const received = Date.parse(m.ReceivedDateTime || '') || 0;
      const conv = m.ConversationId || null;
      const row = {
        id: m.Id,
        internet_message_id: m.InternetMessageId || null,
        conversation_id: conv,
        account_address: account,
        // Calendar responses arrive as derived types (EventMessage/EventMessageResponse).
        // OData emits @odata.type for derived entities even under $select, which is the
        // OWA stand-in for the native reader's vcalendar data-file sniff.
        odata_type: m['@odata.type'] || '',
        subject: m.Subject || '',
        sender: (m.From && m.From.EmailAddress && m.From.EmailAddress.Name) || '',
        sender_address: (m.From && m.From.EmailAddress && m.From.EmailAddress.Address) || '',
        to_recipients: (m.ToRecipients || []).map(addr).filter(Boolean).join('; '),
        cc_recipients: (m.CcRecipients || []).map(addr).filter(Boolean).join('; '),
        received: m.ReceivedDateTime,
        received_unix: Math.floor(received / 1000),
        is_read: Boolean(m.IsRead),
        is_flagged: Boolean(m.Flag && m.Flag.FlagStatus && m.Flag.FlagStatus !== 'NotFlagged'),
        has_attachments: Boolean(m.HasAttachments),
        // Raw, NOT whitespace-collapsed. The native reader's Message_Preview keeps its line
        // breaks and the shared reply gate reads that structure (greeting line, signature
        // block). Collapsing it here flipped one message from addressed_to_other to
        // broadcast in the 2026-07-30 shadow run.
        preview: m.BodyPreview || '',
        replied_after: Boolean(conv && (sentBy.get(conv) || 0) > received),
        body: '',
        // The native SQLite reader has no UFN exclusion, so a withheld body is a real and
        // permanent parity gap, not a fetch failure. Declare it per row: the shadow
        // comparator must be able to tell "classified on an empty body" apart from an
        // unexplained transport disagreement.
        body_withheld: false,
      };
      row.body_eligible = !prohibited.test(`${row.subject}\n${row.sender_address}\n${row.preview}`);
      return row;
    });

    // A read message that operator already replied to and did not flag can never carry an
    // attention reason, so its body is never consulted -- exactly as in the native reader.
    // Skipping those is what keeps the fetch count off the throttling ceiling.
    const needsBody = x => x.body_eligible && !(x.is_read && x.replied_after && !x.is_flagged);
    const eligible = metadataOnly ? [] : out.filter(needsBody);
    for (const row of out) if (metadataOnly || !needsBody(row)) row.body_skipped = true;
    // 8-wide with no pacing throttled hard: measured 2026-07-30, 107 heads returned 46
    // silently empty bodies. Narrower batches, one retry, and every failure declared.
    for (let i = 0; i < eligible.length; i += 4) {
      const batch = eligible.slice(i, i + 4);
      const fetchOne = async x => {
        const wasUnread = x.is_read === false;
        let r = await get(token, `/api/v2.0/me/messages/${encodeURIComponent(x.id)}?$select=Body`);
        if (r.s !== 200) {
          await sleep(1500);
          r = await get(token, `/api/v2.0/me/messages/${encodeURIComponent(x.id)}?$select=Body`);
        }
        if (wasUnread) await keepUnread(token, x.id);
        return r;
      };
      const bodies = await Promise.all(batch.map(fetchOne));
      for (let j = 0; j < batch.length; j++) {
        if (bodies[j].s !== 200) {
          // Never silent: an empty body from a failed fetch is indistinguishable from a
          // genuinely empty message, and the second one is a legitimate classification
          // input while the first one is a bug.
          batch[j].body_fetch_failed = true;
          batch[j].body_fetch_status = bodies[j].s;
          continue;
        }
        const body = strip(bodies[j].j.Body && bodies[j].j.Body.Content).slice(0, 40000);
        if (body && !prohibited.test(body)) batch[j].body = body;
        else if (body) batch[j].body_withheld = true;
      }
      if (i + 4 < eligible.length) await sleep(120);
    }
    for (const row of out) {
      if (!row.body_eligible) row.body_withheld = true;
      // Passive discovery is only a locator. Eligible attention heads need a
      // later exact, account-bound source read before body classification.
      // Prohibited rows stay false so metadata cannot route them into a body
      // read through a different layer.
      row.requires_exact_hydration = Boolean(metadataOnly && row.body_eligible);
      delete row.body_eligible;
      row.body_skipped = Boolean(row.body_skipped);
      row.body_fetch_failed = Boolean(row.body_fetch_failed);
    }
    await writeOut(JSON.stringify({
      days,
      metadata_only: metadataOnly,
      // Named, deliberate parity gap: the native reader has Message_MentionedMe and REST v2.0
      // exposes no equivalent scalar. Rows that would carry only a "mentioned" reason will
      // differ between transports. Declared here so the comparator reports it as known
      // rather than as an unexplained delta.
      parity_gaps: ['mentioned_me'],
      sent_conversations_scanned: sentBy.size,
      ...(strictPage ? {
        pagination: 'owa_unread_cursor_v1',
        next_cursor: nextCursor,
        sent_reply_proof: sentReplyProofDeferred ? 'exact_source_deferred' : 'complete',
      } : {}),
      items: out,
    }) + '\n');
    return;
  }
  if (cmd === 'sent-drafts-json') {
    // Feeds outlook_local.get_sent_and_draft_subjects -> core.draft_hygiene.needs_action,
    // the "already answered" gate. Without it the bot re-drafts threads operator has replied to,
    // so this is correctness, not decoration. Ninth SQLite reader on the drafting path.
    const days = Math.max(1, Number(arg('days', '45')) || 45);
    const limit = Math.max(1, Number(arg('limit', '4000')) || 4000);
    const since = new Date(Date.now() - days * 86400000).toISOString();
    const out = { sent: [], drafts: [] };
    for (const [key, folder, tsField] of [
      ['sent', 'sentitems', 'SentDateTime'],
      ['drafts', 'drafts', 'LastModifiedDateTime'],
    ]) {
      let p = `/api/v2.0/me/mailfolders/${folder}/messages?$top=100&$orderby=${tsField} desc`
            + `&$filter=${tsField} ge ${since}&$select=Subject,${tsField},ConversationId`;
      while (p && out[key].length < limit) {
        const r = await get(token, p);
        if (r.s !== 200) break;
        for (const m of (r.j.value || [])) {
          out[key].push({
            subject: m.Subject || '',
            ts: Math.floor((Date.parse(m[tsField] || '') || 0) / 1000),
            conversation_id: m.ConversationId || '',
          });
        }
        const next = r.j['@odata.nextLink'];
        p = next ? next.replace(/^https:\/\/outlook\.office\.com/, '') : null;
      }
    }
    await writeOut(JSON.stringify(out) + '\n');
    return;
  }
  if (cmd === 'drafts-json') {
    // Feeds tools/drafts_digest.py. Unlike sent-drafts-json (subjects only, for the
    // already-answered gate) this carries recipients and WebLink -- the per-draft OWA
    // deeplink -- so a rollup can point at the exact draft instead of the Drafts folder.
    const top = Math.max(1, Number(arg('top', '50')) || 50);
    const sel = 'Id,Subject,ToRecipients,CcRecipients,LastModifiedDateTime,BodyPreview,WebLink,ConversationId';
    const r = await get(token,
      `/api/v2.0/me/mailfolders/drafts/messages?$top=${top}&$orderby=LastModifiedDateTime desc&$select=${sel}`);
    if (r.s !== 200) { await writeOut(JSON.stringify({ error: r.s, raw: (r.raw || '').slice(0, 200) }) + '\n'); return; }
    const rows = (r.j.value || []).map(m => ({
      id: m.Id,
      subject: m.Subject || '',
      to: (m.ToRecipients || []).map(x => (x.EmailAddress || {}).Name || (x.EmailAddress || {}).Address).filter(Boolean),
      cc_count: (m.CcRecipients || []).length,
      modified: m.LastModifiedDateTime || '',
      link: m.WebLink || '',
      conversation_id: m.ConversationId || '',
      preview: (m.BodyPreview || '').replace(/\s+/g, ' ').slice(0, 200),
    }));
    await writeOut(JSON.stringify(rows) + '\n');
    return;
  }
  if (cmd === 'inbox') {
    const top = Number(arg('top', '25'));
    const sel = 'Subject,From,ReceivedDateTime,IsRead,BodyPreview,HasAttachments';
    const filt = flag('unread') ? '$filter=IsRead eq false&' : '';
    const r = await get(token, `/api/v2.0/me/mailfolders/inbox/messages?${filt}$top=${top}&$orderby=ReceivedDateTime desc&$select=${sel}`);
    if (r.s !== 200) return console.log('error ' + r.s + ' ' + (r.raw || ''));
    for (const m of (r.j.value || [])) console.log(`${m.IsRead ? '  ' : '🔵 '}${(m.Subject || '(no subject)')} — ${(m.From && m.From.EmailAddress && m.From.EmailAddress.Name) || ''} · ${fmt(m.ReceivedDateTime)}${m.HasAttachments ? ' 📎' : ''}\n   ${(m.BodyPreview || '').replace(/\s+/g, ' ').slice(0, 160)}`);
    return;
  }
  if (cmd === 'agenda' || cmd === 'upcoming') {
    const days = cmd === 'agenda' ? 1 : Number(arg('days', '7'));
    const r = await get(token, `/api/v2.0/me/calendarview?startDateTime=${isoDay(0)}&endDateTime=${isoDay(days)}&$orderby=Start/DateTime&$top=50&$select=Subject,Start,End,Location,Organizer,IsAllDay`);
    if (r.s !== 200) return console.log('error ' + r.s + ' ' + (r.raw || ''));
    const ev = r.j.value || [];
    if (!ev.length) return console.log(cmd === 'agenda' ? 'No events today.' : `No events in the next ${days} days.`);
    for (const e of ev) console.log(`${fmt(e.Start && e.Start.DateTime)} — ${e.Subject || '(untitled)'}${e.Location && e.Location.DisplayName ? ' @ ' + e.Location.DisplayName : ''}${e.Organizer && e.Organizer.EmailAddress ? ' · ' + e.Organizer.EmailAddress.Name : ''}`);
    return;
  }
  if (cmd === 'calendar-json') {
    // v2.0 emits Start/End as UTC WITHOUT a trailing Z, and `new Date()` reads a naive string as
    // LOCAL — so every event used to come out shifted by the local UTC offset (+5h in CDT). cue
    // then never saw a meeting in progress and served no in-call suggestions. Same normalizer
    // calendar-sync-json already uses.
    const utcDT = v => { const s = (v && v.DateTime) || ''; return s ? (/[Zz]|[+-]\d\d:\d\d$/.test(s) ? new Date(s).toISOString() : s.replace(/\.\d+$/, '') + 'Z') : ''; };
    const hours = Math.max(1, Number(arg('hours', '2')) || 2);
    const end = new Date(Date.now() + hours * 60 * 60 * 1000).toISOString();
    const sel = 'Subject,Start,End,Location,Organizer,Attendees,IsAllDay,BodyPreview';
    const r = await get(token, `/api/v2.0/me/calendarview?startDateTime=${isoDay(0)}&endDateTime=${end}&$orderby=Start/DateTime&$top=50&$select=${sel}`);
    if (r.s !== 200) { console.log(JSON.stringify({ error: r.s, raw: r.raw || '' })); process.exit(4); }
    const rows = (r.j.value || []).map(e => ({
      subject: e.Subject || '(untitled)',
      start: utcDT(e.Start),
      end: utcDT(e.End),
      location: (e.Location && e.Location.DisplayName) || '',
      organizer: (e.Organizer && e.Organizer.EmailAddress && (e.Organizer.EmailAddress.Address || e.Organizer.EmailAddress.Name)) || '',
      attendees: (e.Attendees || []).map(a => {
        const x = a.EmailAddress || {};
        return `${x.Name || ''} <${x.Address || ''}>`;
      }).filter(Boolean).join('; '),
      body: (e.BodyPreview || '').slice(0, 500),
      is_all_day: Boolean(e.IsAllDay),
      _work_calendar_source: true,
    }));
    console.log(JSON.stringify(rows));
    return;
  }
  if (cmd === 'calendar-sync-json') {
    // Mirror feed for tools/calendar_mirror.py. Unlike calendar-json this pages past the
    // 50-row cap, takes an explicit +/- day window, and carries the identity + change fields
    // a one-way mirror needs (Id, iCalUId, LastModifiedDateTime, IsCancelled).
    const back = Math.max(0, Number(arg('back-days', '7')) || 0);
    const fwd = Math.max(1, Number(arg('forward-days', '60')) || 60);
    const sel = [
      'Id', 'iCalUId', 'LastModifiedDateTime', 'Subject', 'Start', 'End', 'IsAllDay',
      'Location', 'Organizer', 'Attendees', 'Body', 'ShowAs', 'IsCancelled', 'Sensitivity',
      'OnlineMeetingUrl', 'Type', 'SeriesMasterId', 'ResponseStatus',
    ].join(',');
    // v2.0 emits Start/End as UTC without a trailing Z; `new Date()` would read them as local.
    const utc = v => { const s = (v && v.DateTime) || ''; return s ? (/[Zz]|[+-]\d\d:\d\d$/.test(s) ? new Date(s).toISOString() : s.replace(/\.\d+$/, '') + 'Z') : ''; };
    let path = `/api/v2.0/me/calendarview?startDateTime=${isoDay(-back)}&endDateTime=${isoDay(fwd)}&$orderby=Start/DateTime&$top=100&$select=${sel}`;
    const rows = [];
    for (let page = 0; path && page < 25; page++) {
      const r = await get(token, path);
      if (r.s === 401) {
        // A reused bearer aged out. Drop it so the next run captures a fresh one.
        dropCachedToken();
        console.log(JSON.stringify({ error: 401, failure_kind: 'token_expired', raw: 'cached OWA bearer rejected' }));
        process.exit(5);
      }
      if (r.s !== 200) { console.log(JSON.stringify({ error: r.s, raw: r.raw || r.err || '' })); process.exit(4); }
      for (const e of (r.j.value || [])) {
        rows.push({
          id: e.Id,
          ical_uid: e.iCalUId || '',
          modified: e.LastModifiedDateTime || '',
          subject: e.Subject || '(untitled)',
          start: utc(e.Start),
          end: utc(e.End),
          start_tz: (e.Start && e.Start.TimeZone) || 'UTC',
          is_all_day: Boolean(e.IsAllDay),
          location: (e.Location && e.Location.DisplayName) || '',
          organizer: (e.Organizer && e.Organizer.EmailAddress && (e.Organizer.EmailAddress.Address || e.Organizer.EmailAddress.Name)) || '',
          attendees: (e.Attendees || []).map(a => {
            const x = a.EmailAddress || {};
            return { name: x.Name || '', address: x.Address || '', type: a.Type || '' };
          }),
          body_html: (e.Body && e.Body.ContentType === 'HTML') ? (e.Body.Content || '') : '',
          body_text: (e.Body && e.Body.ContentType !== 'HTML') ? (e.Body.Content || '') : strip(e.Body && e.Body.Content),
          show_as: e.ShowAs || '',
          cancelled: Boolean(e.IsCancelled),
          sensitivity: e.Sensitivity || '',
          online_url: e.OnlineMeetingUrl || '',
          type: e.Type || '',
          series_master_id: e.SeriesMasterId || '',
          my_response: (e.ResponseStatus && e.ResponseStatus.Response) || '',
        });
      }
      const next = r.j['@odata.nextLink'] || '';
      path = next ? next.replace(/^https:\/\/outlook\.office\.com/, '') : '';
    }
    await writeOut(JSON.stringify({ window_back_days: back, window_forward_days: fwd, count: rows.length, events: rows }) + '\n');
    return;
  }
  if (cmd === 'search-events') {
    const q = (arg('q') || '').toLowerCase(); const days = Number(arg('days', '30'));
    const r = await get(token, `/api/v2.0/me/calendarview?startDateTime=${isoDay(-days)}&endDateTime=${isoDay(days)}&$orderby=Start/DateTime&$top=100&$select=Subject,Start,Location`);
    if (r.s !== 200) return console.log('error ' + r.s);
    const hits = (r.j.value || []).filter(e => (e.Subject || '').toLowerCase().includes(q));
    if (!hits.length) return console.log(`No events matching "${arg('q')}".`);
    for (const e of hits) console.log(`${fmt(e.Start && e.Start.DateTime)} — ${e.Subject}`);
    return;
  }
  if (cmd === 'read') {
    const m = arg('match');
    if (!m) return console.log('read needs --match');
    const f = await get(token, `/api/v2.0/me/messages?$search="${encodeURIComponent(m)}"&$top=1&$select=Subject,From,ReceivedDateTime,Body`);
    const hit = (f.j && f.j.value && f.j.value[0]) || null;
    if (!hit) return console.log(`No message matching "${m}".`);
    console.log(`Subject: ${hit.Subject}\nFrom: ${(hit.From && hit.From.EmailAddress && hit.From.EmailAddress.Address) || ''}\nReceived: ${fmt(hit.ReceivedDateTime)}\n\n${strip(hit.Body && hit.Body.Content).slice(0, 3000)}`);
    return;
  }
  if (cmd === 'index') {
    // JSON for the work-context indexer. /me/messages covers every mailbox folder,
    // including Sent and user-created folders; the old inbox-only path silently
    // omitted operator's own post-call notes and archived project mail.
    const top = Number(arg('top', '300'));
    const bodyTop = Math.min(top, Math.max(0, Number(arg('body-top', '0')) || 0));
    const sel = 'Id,Subject,From,ToRecipients,CcRecipients,ReceivedDateTime,BodyPreview,IsDraft,HasAttachments';
    const rows = []; let p = `/api/v2.0/me/messages?$top=100&$orderby=ReceivedDateTime desc&$select=${sel}`;
    while (p && rows.length < top) {
      const r = await get(token, p);
      if (r.s !== 200) { console.log(JSON.stringify({ error: r.s, raw: r.raw })); return; }
      for (const m of (r.j.value || [])) {
        if (m.IsDraft !== false) {
          const tombstone = { id: m.Id, is_draft: m.IsDraft === true };
          // Missing state is fail-closed, not an authoritative non-draft claim.
          if (m.IsDraft !== true) delete tombstone.is_draft;
          rows.push(tombstone);
          continue;
        }
        const parts = [
          m.From && m.From.EmailAddress && (m.From.EmailAddress.Name || m.From.EmailAddress.Address),
          ...(m.ToRecipients || []).map(x => x.EmailAddress && x.EmailAddress.Address),
          ...(m.CcRecipients || []).map(x => x.EmailAddress && x.EmailAddress.Address),
        ].filter(Boolean).join(' · ');
        const row = { id: m.Id, is_draft: false, subject: m.Subject || '(no subject)', parts,
                      received: m.ReceivedDateTime, preview: (m.BodyPreview || '').replace(/\s+/g, ' '),
                      has_attachments: Boolean(m.HasAttachments) };
        // Do not intentionally body-fetch any source covered by the standing UFN
        // exclusion. Metadata remains enough to reject the row downstream.
        row.body_eligible = !row.is_draft && !prohibited.test(`${row.subject}\n${row.parts}\n${row.preview}`);
        rows.push(row);
      }
      const next = r.j['@odata.nextLink']; p = next ? next.replace(/^https:\/\/outlook\.office\.com/, '') : null;
    }
    const out = rows.slice(0, top);
    const candidates = out.filter(x => x.body_eligible).slice(0, bodyTop);
    for (let i = 0; i < candidates.length; i += 8) {
      const batch = candidates.slice(i, i + 8);
      const bodies = await Promise.all(batch.map(x =>
        get(token, `/api/v2.0/me/messages/${encodeURIComponent(x.id)}?$select=Body`)
      ));
      for (let j = 0; j < batch.length; j++) {
        if (bodies[j].s !== 200) continue;
        const body = strip(bodies[j].j.Body && bodies[j].j.Body.Content).slice(0, 48000);
        if (body && !prohibited.test(body)) batch[j].body = body;
      }
    }
    for (const row of out) delete row.body_eligible;
    // process.exit() follows immediately after the command resolves. Await the
    // pipe callback so large all-folder/full-body payloads are not truncated.
    await writeOut(JSON.stringify(out) + '\n');
    return;
  }
  // Resolve a thread by exact subject anywhere in the mailbox. The recent-message
  // index only covers the newest few hundred, so a thread that has been sitting
  // unanswered for weeks -- precisely the ones worth surfacing -- never appeared
  // in it and produced a note with no link.
  if (cmd === 'find-by-subject') {
    const want = String(arg('subject', '')).trim();
    if (!want) { await writeOut(JSON.stringify({ error: 'need --subject' }) + '\n'); return; }
    const sel = 'Id,InternetMessageId,Subject,From,ToRecipients,CcRecipients,ReceivedDateTime,BodyPreview,ConversationId';
    // $search, not $filter: a Subject-equality filter cannot match the RE:/FW:
    // variants of the same thread, and an or-chain of them is rejected outright.
    // $search is also mutually exclusive with $orderby here, so sort locally.
    const r = await get(token,
      `/api/v2.0/me/messages?$top=25&$select=${sel}&$search=${encodeURIComponent('"subject:' + want + '"')}`);
    if (r.s !== 200) { await writeOut(JSON.stringify({ error: r.s, raw: (r.raw || '').slice(0, 200) }) + '\n'); return; }
    const rows = (r.j.value || []).map(m => ({
      id: m.Id,
      // the zap/reply writers resolve a source by InternetMessageId, so a subject hit is
      // only actionable when it carries one
      internet_message_id: m.InternetMessageId || null,
      subject: m.Subject || '',
      from: (m.From && m.From.EmailAddress && (m.From.EmailAddress.Address || m.From.EmailAddress.Name)) || '',
      from_name: (m.From && m.From.EmailAddress && m.From.EmailAddress.Name) || '',
      received: m.ReceivedDateTime,
      conversation_id: m.ConversationId || '',
      people: [
        ...(m.ToRecipients || []).map(x => (x.EmailAddress || {}).Address),
        ...(m.CcRecipients || []).map(x => (x.EmailAddress || {}).Address),
      ].filter(Boolean),
      preview: (m.BodyPreview || '').replace(/\s+/g, ' ').slice(0, 400),
    }));
    rows.sort((a, b) => String(b.received || '').localeCompare(String(a.received || '')));
    await writeOut(JSON.stringify(rows) + '\n');
    return;
  }
  if (cmd === 'body') {
    const id = arg('id'); if (!id) { console.log(''); return; }
    const r = await get(token, `/api/v2.0/me/messages/${encodeURIComponent(id)}?$select=Body`);
    console.log(r.s === 200 ? strip(r.j.Body && r.j.Body.Content).slice(0, 8000) : '');
    return;
  }
  if (cmd === 'bodies') {
    let ids = [];
    try {
      const parsed = JSON.parse(arg('ids-json', '[]'));
      if (Array.isArray(parsed)) ids = parsed.map(String).filter(Boolean).slice(0, 500);
    } catch {}
    const out = [];
    for (let i = 0; i < ids.length; i += 8) {
      const batch = ids.slice(i, i + 8);
      const bodies = await Promise.all(batch.map(id =>
        get(token, `/api/v2.0/me/messages/${encodeURIComponent(id)}?$select=Body`)
      ));
      for (let j = 0; j < batch.length; j++) {
        const result = bodies[j];
        const body = result.s === 200
          ? strip(result.j.Body && result.j.Body.Content).slice(0, 48000)
          : '';
        out.push({
          id: batch[j],
          ok: result.s === 200,
          blocked: Boolean(body && prohibited.test(body)),
          body: body && !prohibited.test(body) ? body : '',
        });
      }
    }
    await writeOut(JSON.stringify(out) + '\n');
    return;
  }

  if (cmd === 'attachment-texts') {
    const top = Math.min(80, Math.max(1, Number(arg('top', '40')) || 40));
    const sel = 'Id,Subject,From,ToRecipients,HasAttachments,IsDraft,ReceivedDateTime';
    const hits = [];
    let pth = `/api/v2.0/me/messages?$top=50&$orderby=ReceivedDateTime desc&$select=${sel}`;
    while (pth && hits.length < top) {
      const r = await get(token, pth);
      if (r.s !== 200) { await writeOut(JSON.stringify({ error: r.s }) + '\n'); return; }
      for (const m of (r.j.value || [])) {
        if (m.IsDraft !== false) continue;
        if (!m.HasAttachments) continue;
        hits.push(m);
        if (hits.length >= top) break;
      }
      const next = r.j['@odata.nextLink'];
      pth = next ? next.replace(/^https:\/\/outlook.office.com/, '') : null;
    }
    const extractable = /(pdf|docx|xlsx|pptx|txt|csv|md|html?|rtf|eml|json|xml|ya?ml|log)$/i;
    const out = [];
    for (const m of hits) {
      const att = await get(token, `/api/v2.0/me/messages/${encodeURIComponent(m.Id)}/attachments?$top=25`);
      const files = [];
      for (const a of ((att.j && att.j.value) || [])) {
        const name = String(a.Name || 'attachment');
        const size = Number(a.Size || 0);
        const row = { name, size, content_type: String(a.ContentType || ''), content_b64: '' };
        if (size > 0 && size <= 8000000 && extractable.test(name) && a.Id) {
          const full = await get(token, `/api/v2.0/me/messages/${encodeURIComponent(m.Id)}/attachments/${encodeURIComponent(a.Id)}`);
          if (full.s === 200 && full.j && full.j.ContentBytes) row.content_b64 = String(full.j.ContentBytes);
        }
        files.push(row);
      }
      const parts = [
        m.From && m.From.EmailAddress && (m.From.EmailAddress.Name || m.From.EmailAddress.Address),
        ...(m.ToRecipients || []).map(x => x.EmailAddress && x.EmailAddress.Address),
      ].filter(Boolean).join(' · ');
      out.push({ id: m.Id, is_draft: false, subject: m.Subject || '', parts, received: m.ReceivedDateTime, files });
    }
    await writeOut(JSON.stringify(out) + '\n');
    return;
  }
  if (cmd === 'zoom-summaries') {
    // Zoom AI Companion summaries arrive as "Meeting assets ... are ready!" emails from
    // redacted-email — the body carries the actual summary. Return them as JSON for indexing.
    const top = Number(arg('top', '200'));
    const sel = 'Id,Subject,From,ReceivedDateTime,Body';
    // Zoom mails the AI summary to the meeting HOST only. For a meeting someone else
    // hosted, a colleague's forward of that same mail is the ONLY copy operator ever gets —
    // it quotes Zoom's envelope and the full summary verbatim. Query both.
    // The quotes belong INSIDE the encoding: a literal quote in the template plus a quote in
    // the query itself nests them and the search silently returns nothing.
    const queries = [
      ['from:zoom.us meeting assets are ready', top],
      ['subject:Meeting assets', Math.min(top, 60)],
    ];
    const byId = new Map();
    let anyOk = false;
    for (const [q, qtop] of queries) {
      const r = await get(token, `/api/v2.0/me/messages?$search=${encodeURIComponent('"' + q + '"')}&$top=${qtop}&$select=${sel}`);
      if (r.s !== 200) continue;
      anyOk = true;
      for (const m of (r.j.value || [])) if (!byId.has(m.Id)) byId.set(m.Id, m);
    }
    if (!anyOk) { console.log('[]'); return; }
    const out = [];
    for (const m of byId.values()) {
      if (!/meeting assets.*ready/i.test(m.Subject || '')) continue;
      let body = strip(m.Body && m.Body.Content);
      // drop the external-sender banner + de-dupe the doubled body Zoom emails ship
      body = body.replace(/ZjQcmQRYFpfpt[\s\S]*?ZjQcmQRYFpfptBannerEnd/g, ' ').replace(/\s+/g, ' ').trim();
      const half = body.slice(0, Math.floor(body.length / 2));
      if (body.slice(Math.floor(body.length / 2)).trim().startsWith(half.slice(0, 40).trim())) body = half;
      const from = (m.From && m.From.EmailAddress && (m.From.EmailAddress.Address || m.From.EmailAddress.Name)) || '';
      out.push({ id: m.Id, subject: m.Subject, from, received: m.ReceivedDateTime, body: body.slice(0, 12000) });
    }
    await writeOut(JSON.stringify(out) + '\n');
    return;
  }
  console.log('unknown command: ' + cmd);
  process.exit(1);
// the CDP websocket keeps the event loop alive after the command returns, so every branch
// that falls through would hang until the caller's timeout killed it — and discarded a
// successful read. exit explicitly; never browser.close(), that kills the shared profile.
})().then(() => { __release(); process.exit(0); },
         e => { __release(); console.log('owa error: ' + (e && e.message)); process.exit(1); });
