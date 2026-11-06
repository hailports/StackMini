#!/usr/bin/env node
/*
 * Zoom Team Chat via the live nx-chat SDK over raw CDP.
 *
 * Usage:
 *   node tools/zoom_write.js drafts
 *   node tools/zoom_write.js history --session-id <jid>
 *   node tools/zoom_write.js save --session-id <jid> --text-file <path> [--attach <file>...]
 *   node tools/zoom_write.js attachments --session-id <jid> [--since <iso>]
 *
 * Drafts and history are query-only. save calls sdk.mutation.createDraft /
 * updateDraft, then fetchDraftsList readback. It never calls a send method.
 * After a main (draftType 0) save, it opens that 1:1 and types into the
 * main composer over the same raw CDP session so the text is in the box
 * the operator looks at. Nested replies do not hydrate. ZOOM_HYDRATE_COMPOSER=0
 * or --no-hydrate skips typing. A bare Enter is never dispatched.
 */
const { spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const CDP = process.env.ZOOM_WEB_CDP || 'http://127.0.0.1:18812';  // 2026-10-12: stale tailnet default timed out every read
const ZOOM_APP_ORIGIN = 'https://app.zoom.us';
// fetchDraftsList is the captured POST /xms/drafts/fetch/userid transport.
const STEP_MS = Math.max(1_000, Math.min(15_000, Number(process.env.ZOOM_WRITE_STEP_MS || 8_000)));
const RESPONSE_BYTES = 1_000_000;
const DRAFTS_EVAL_MS = 70_000;
// 2026-09-14: raw CDP Runtime.evaluate save hung 90s on a busy/stale
// team-chat tab. Prefer settle + one retry over remint; allow env bump.
const SAVE_EVAL_MS = Math.max(60_000, Math.min(180_000, Number(process.env.ZOOM_WRITE_SAVE_EVAL_MS || 90_000)));
const SAVE_SETTLE_MS = Math.max(0, Math.min(30_000, Number(process.env.ZOOM_WRITE_SAVE_SETTLE_MS || 4_000)));
const SAVE_EVAL_RETRIES = Math.max(0, Math.min(2, Number(process.env.ZOOM_WRITE_SAVE_EVAL_RETRIES || 1)));
const HYDRATE_EVAL_MS = 80_000;
const HISTORY_EVAL_MS = STEP_MS * 3;
const EXACT_SID = /^[^\s@]+@(?:xmpp|conference\.xmpp)\.zoom\.us$/i;
const ACTIONS = new Set(['drafts', 'save', 'history', 'delete', 'attachments']);
const ATTACH_MAX_BYTES = 25 * 1024 * 1024;
const ATTACH_DOWNLOAD_MAX_BYTES = 8 * 1024 * 1024;

function fail(message) {
  process.stderr.write(`zoom_write: ${message}\n`);
  process.exitCode = 2;
}

function isLoopback(hostname) {
  return ['127.0.0.1', 'localhost', '::1', '[::1]'].includes(String(hostname || '').toLowerCase());
}

function isGrokBridgeCdp(value) {
  if (String(process.env.GROK_BRIDGE_CDP || '').trim() === '0') return false;
  let endpoint;
  try { endpoint = new URL(value); } catch { return false; }
  const host = String(endpoint.hostname || '').toLowerCase();
  const port = String(endpoint.port || '');
  const allowedHost = String(process.env.GROK_CDP_HOST || '').toLowerCase();
  const allowedPort = String(process.env.GROK_CDP_PORT || '9227');
  if (port !== allowedPort) return false;
  if (host === allowedHost) return true;
  return String(process.env.GROK_BRIDGE_CDP || '').trim() !== '' && port === allowedPort;
}

function cdpPort(value) {
  let endpoint;
  try { endpoint = new URL(value); } catch { throw new Error('invalid Zoom CDP endpoint'); }
  if (isGrokBridgeCdp(value)) {
    if (endpoint.protocol !== 'http:') throw new Error('Zoom CDP must be http');
    return endpoint.port || String(process.env.GROK_CDP_PORT || '9227');
  }
  if (endpoint.protocol !== 'http:' || !isLoopback(endpoint.hostname) || endpoint.port !== '18812') {
    throw new Error('Zoom CDP must be the local bridge on port 18812');
  }
  return endpoint.port;
}

function browserSocket(value, discovery) {
  const port = cdpPort(value);
  let endpoint;
  try { endpoint = new URL(String(discovery && discovery.webSocketDebuggerUrl || '')); }
  catch { throw new Error('Zoom CDP discovery has no browser websocket'); }
  if (isGrokBridgeCdp(value)) {
    if (endpoint.protocol !== 'ws:' || endpoint.port !== port) {
      throw new Error('Zoom CDP discovery returned an untrusted browser websocket');
    }
    return endpoint.toString();
  }
  if (endpoint.protocol !== 'ws:' || !isLoopback(endpoint.hostname) || endpoint.port !== port) {
    throw new Error('Zoom CDP discovery returned an untrusted browser websocket');
  }
  return endpoint.toString();
}

async function cdpDiscovery() {
  const endpoint = new URL('/json/version', new URL(CDP));
  const response = await fetch(endpoint, { signal: AbortSignal.timeout(STEP_MS) });
  if (!response.ok) throw new Error(`Zoom CDP discovery returned HTTP ${response.status}`);
  return browserSocket(CDP, await response.json());
}

class RawCdp {
  constructor(endpoint) {
    this.endpoint = endpoint;
    this.id = 0;
    this.pending = new Map();
    this.closed = false;
  }

  async connect() {
    this.ws = new WebSocket(this.endpoint);
    this.ws.addEventListener('message', event => this.onMessage(event));
    this.ws.addEventListener('error', () => this.rejectAll(new Error('Zoom CDP websocket error')));
    this.ws.addEventListener('close', () => this.rejectAll(new Error('Zoom CDP websocket closed')));
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`Zoom CDP websocket timed out after ${STEP_MS}ms`)), STEP_MS);
      this.ws.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
      this.ws.addEventListener('error', () => { clearTimeout(timer); reject(new Error('Zoom CDP websocket error')); }, { once: true });
    });
    await this.command('Browser.getVersion');
  }

  onMessage(event) {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    if (!message.id) return;
    const pending = this.pending.get(message.id);
    if (!pending) return;
    this.pending.delete(message.id);
    clearTimeout(pending.timer);
    if (message.error) pending.reject(new Error(`raw CDP ${pending.method}: ${message.error.message || 'failed'}`));
    else pending.resolve(message.result || {});
  }

  rejectAll(error) {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
  }

  command(method, params = {}, sessionId = '', timeoutMs = STEP_MS) {
    if (this.closed) return Promise.reject(new Error('Zoom CDP client is closed'));
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`raw CDP ${method} timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      this.pending.set(id, { method, resolve, reject, timer });
      try {
        const message = { id, method, params };
        if (sessionId) message.sessionId = sessionId;
        this.ws.send(JSON.stringify(message));
      }
      catch (error) { clearTimeout(timer); this.pending.delete(id); reject(error); }
    });
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.rejectAll(new Error('Zoom CDP client closed'));
    try { this.ws.close(); } catch {}
  }
}

const PAGE_BOOTSTRAP = `
    const oneValue = (source, label, timeoutMs) => new Promise((resolve, reject) => {
      let settled = false;
      let subscription = null;
      const timer = setTimeout(() => finish(reject, new Error(label + '_timeout')), timeoutMs || ${STEP_MS});
      const finish = (fn, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        try { if (subscription && typeof subscription.unsubscribe === 'function') subscription.unsubscribe(); } catch (_) {}
        fn(value);
      };
      try {
        if (source && typeof source.subscribe === 'function') {
          subscription = source.subscribe({
            next: value => finish(resolve, value),
            error: error => finish(reject, error),
            complete: () => finish(reject, new Error(label + '_empty')),
          });
          if (settled && subscription && typeof subscription.unsubscribe === 'function') subscription.unsubscribe();
          return;
        }
        if (source && typeof source.then === 'function') {
          Promise.resolve(source).then(value => finish(resolve, value), error => finish(reject, error));
          return;
        }
        if (source !== undefined && source !== null) { finish(resolve, source); return; }
        finish(reject, new Error(label + '_empty'));
      } catch (error) { finish(reject, error); }
    });
    const nxQueryOf = value => value && value.sdk && value.sdk.query ? value.sdk.query
      : (value && value.query ? value.query : null);
    const nxWrapClient = value => {
      const query = nxQueryOf(value);
      if (!query) return null;
      return value.sdk && value.sdk.query ? value : { sdk: value };
    };
    const nxCollectClients = (mod, need) => {
      const found = [];
      if (!mod) return found;
      const consider = value => {
        const wrapped = nxWrapClient(value);
        if (wrapped && need(wrapped.sdk.query)) found.push(wrapped);
      };
      for (const value of Object.values(mod)) {
        consider(value);
        if (typeof value !== 'function' || value.length !== 0) continue;
        const src = String(value);
        if (src.length > 240 || /throw n\\[0\\]|destroy|clearStorage|reload/.test(src)) continue;
        let ret; try { ret = value(); } catch (_) { continue; }
        consider(ret);
      }
      return found;
    };
    const nxDedupeClients = list => {
      const seen = new Set(), out = [];
      for (const client of list) {
        const query = client && client.sdk && client.sdk.query;
        if (!query || seen.has(query)) continue;
        seen.add(query);
        out.push(client);
      }
      return out;
    };
    // Zoom 7.0.5 index.js is a 38-byte boot stub. The live client is the
    // SDK singleton inside chunk-packages-sdk.js (getSDK export).
    const nxLiveSdk = async resourceNames => {
      const chunks = [];
      for (const raw of resourceNames) {
        if (!/\\/nx-chat\\/[^/]+\\/chunk-packages-sdk\\.js(?:[?#]|$)/.test(raw)) continue;
        let url;
        try { url = new URL(raw); } catch (_) { continue; }
        if (url.protocol !== 'https:' || !/(^|\\.)zoom\\.us$/i.test(url.hostname) || url.username || url.password || url.port || url.hash
          || !/^\\/fe-static\\/nx-chat\\/[^/]+\\/chunk-packages-sdk\\.js$/.test(url.pathname)) continue;
        if (!chunks.includes(url.href)) chunks.push(url.href);
      }
      if (chunks.length !== 1) {
        chunks.length = 0;
        // ZOOM-COMPAT 2026-10-12: long-lived page fills the 250-entry resource-timing buffer, so the sdk chunk
        // drops out of the entries; derive it as a sibling of the validated nx-chat index.js (same as zoom_compat_resolver nxcSib).
        for (const raw of resourceNames) {
          let u; try { u = new URL(raw); } catch (_) { continue; }
          if (u.protocol === 'https:' && /(^|\\.)zoom\\.us$/i.test(u.hostname) && /^\\/fe-static\\/nx-chat\\/[^/]+\\/index\\.js$/.test(u.pathname)) {
            chunks.push(new URL('chunk-packages-sdk.js', u.href).href); break;
          }
        }
        if (chunks.length !== 1) return null;
      }
      let mod;
      try { mod = await import(chunks[0]); } catch (_) { return null; }
      let getSDK = null;
      for (const value of Object.values(mod || {})) {
        if (typeof value !== 'function' || value.length !== 0) continue;
        const src = String(value);
        if (src.includes('SDK instance not initialized')) { getSDK = value; break; }
        if (src.indexOf('()=>{if(n)throw') === 0 && src.length < 160) {
          let inner;
          try { inner = value(); } catch (_) { continue; }
          if (typeof inner === 'function' && String(inner).includes('SDK instance not initialized')) {
            getSDK = inner;
            break;
          }
        }
      }
      if (!getSDK) return null;
      let root;
      try { root = getSDK(); } catch (_) { return null; }
      const session = root && root.sessionModule;
      const message = root && root.messageModule;
      const auth = root && root.auth;
      if (!session || !message || !auth) return null;
      if (typeof session.fetchDraftsList !== 'function' || typeof session.createDraft !== 'function'
        || typeof session.updateDraft !== 'function' || typeof auth.getConfig !== 'function'
        || typeof message.fetchHistoryLatestMainMsg !== 'function') return null;
      return {
        query: {
          getConfig: arg => auth.getConfig(arg),
          fetchDraftsList: arg => session.fetchDraftsList(arg),
          fetchHistoryLatestMainMsg: arg => message.fetchHistoryLatestMainMsg(arg),
        },
        mutation: {
          createDraft: arg => session.createDraft(arg),
          updateDraft: arg => session.updateDraft(arg),
          markChatAsUnread: typeof session.markChatAsUnread === 'function'
            ? arg => session.markChatAsUnread(arg) : undefined,
          deleteDraft: typeof session.deleteDraft === 'function' ? arg => session.deleteDraft(arg) : undefined,
        },
      };
    };
    // 2026-10-15 Zoom 7.0.5.21146: nx-chat is on st1.zoom.us inside the team-chat
    // iframe. The /wc shell's 250-entry resource buffer drops index.js and
    // style\\.css (resource_count_0). Derive index.js from any same-version
    // /fe-static/nx-chat/<ver>/ file in this document or a same-origin iframe.
    // Import stays https, *.zoom.us, and that exact index.js path.
    const resources = [];
    const nxPush = (name) => { const s = String(name || ''); if (s) resources.push(s); };
    const nxVisit = (doc) => {
      if (!doc) return;
      const view = doc.defaultView;
      if (view && view.performance && view.performance.getEntriesByType) {
        for (const entry of view.performance.getEntriesByType('resource')) nxPush(entry && entry.name);
      }
      for (const row of [...doc.scripts]) nxPush(row && row.src);
      for (const row of [...doc.querySelectorAll('link[href]')]) nxPush(row.href);
      for (const frame of [...doc.querySelectorAll('iframe')]) {
        try { if (frame.contentDocument) nxVisit(frame.contentDocument); } catch (_) {}
      }
    };
    nxVisit(document);
    const urls = [];
    const nxIndexOk = (url) => url.protocol === 'https:' && /(^|\\.)zoom\\.us$/i.test(url.hostname)
      && !url.username && !url.password && !url.port && !url.hash
      && /^\\/fe-static\\/nx-chat\\/[^/]+\\/index\\.js$/.test(url.pathname);
    for (const raw of resources) {
      let url;
      try { url = new URL(raw, location.href); } catch (_) { continue; }
      if (url.protocol !== 'https:' || !/(^|\\.)zoom\\.us$/i.test(url.hostname) || url.username || url.password || url.port || url.hash) continue;
      if (!/^\\/fe-static\\/nx-chat\\/[^/]+\\/[^/?#]+$/.test(url.pathname)) continue;
      let indexUrl;
      try { indexUrl = new URL('index.js', url.href); } catch (_) { continue; }
      if (!nxIndexOk(indexUrl)) continue;
      if (!urls.includes(indexUrl.href)) urls.push(indexUrl.href);
    }
    if (urls.length !== 1) return { error: 'resource_count_' + Math.min(urls.length, 9) };
    const nxChat = await import(urls[0]);
    let store = null;
    try {
      const storeUrl = new URL('store.js', urls[0]);
      if (storeUrl.protocol === 'https:' && /(^|\\.)zoom\\.us$/i.test(storeUrl.hostname)
        && /^\\/fe-static\\/nx-chat\\/[^/]+\\/store\\.js$/.test(storeUrl.pathname)) {
        store = await import(storeUrl.href);
      }
    } catch (_) {}
    const need = q => typeof q.getConfig === 'function'
      && typeof q.fetchDraftsList === 'function'
      && typeof q.fetchHistoryLatestMainMsg === 'function';
    const clients = nxDedupeClients(nxCollectClients(nxChat, need).concat(nxCollectClients(store, need)));
    let sdk = clients.length === 1 ? clients[0].sdk : null;
    if (!sdk || !sdk.query || !sdk.mutation
      || typeof sdk.mutation.createDraft !== 'function'
      || typeof sdk.mutation.updateDraft !== 'function') {
      sdk = await nxLiveSdk(resources.concat(urls));
    }
    if (!sdk || !sdk.query || !sdk.mutation
      || typeof sdk.mutation.createDraft !== 'function'
      || typeof sdk.mutation.updateDraft !== 'function') {
      return { error: 'drafts_client' };
    }
`;

const DRAFTS_EVALUATION = `
  (async () => {
    let stage = 'resource_discovery';
    try {
    ${PAGE_BOOTSTRAP}
    stage = 'config_query';
    // A cold attach can list zero drafts before the client finishes waking up.
    await new Promise(r => setTimeout(r, 1500));
    const config = await oneValue(sdk.query.getConfig(), 'config');
    const jid = String(config && config.jid || '');
    if (!jid || jid.length > 512 || jid.includes('\\u0000')) throw new Error('Zoom chat config has no usable jid');
    const rows = [];
    let lastValue = null;
    let hasMore = true;
    while (hasMore) {
      if (rows.length > ${RESPONSE_BYTES}) throw new Error('Zoom drafts response exceeded the 1 MB limit');
      const args = { userId: jid, limit: 300 };
      if (lastValue !== null) args.lastValue = lastValue;
      stage = 'drafts_list';
      const page = await oneValue(sdk.query.fetchDraftsList(args), 'drafts_list');
      if (!page || Object.prototype.toString.call(page) !== '[object Object]' || !Array.isArray(page.data)
        || typeof page.hasMore !== 'boolean') throw new Error('Zoom drafts response shape is unproven');
      rows.push(...page.data);
      hasMore = page.hasMore && rows.length < 900;
      if (hasMore) {
        if (!Number.isSafeInteger(page.lastValue) || page.lastValue <= 0
          || (lastValue !== null && page.lastValue <= lastValue)) throw new Error('Zoom drafts pagination cursor is unproven');
        lastValue = page.lastValue;
      }
    }
    if (rows.length === 0) {
      await new Promise(r => setTimeout(r, 4000));
      const page2 = await oneValue(sdk.query.fetchDraftsList({ userId: jid, limit: 300 }), 'drafts_list');
      if (!page2 || !Array.isArray(page2.data) || typeof page2.hasMore !== 'boolean') throw new Error('Zoom drafts response shape is unproven');
      rows.push(...page2.data);
    }
    return { rows };
    } catch (_) {
      return { error: stage };
    }
  })()
`;

function saveEvaluation(request) {
  return `
  (async () => {
    const request = ${JSON.stringify(request)};
    let stage = 'resource_discovery';
    try {
    ${PAGE_BOOTSTRAP}
    const exactSid = /^[^\\s@]+@(?:xmpp|conference\\.xmpp)\\.zoom\\.us$/i;
    const sessionId = String(request.sessionId || '');
    const text = String(request.text || '').replace(/\\u200b/g, '').replace(/\\r\\n?/g, '\\n');
    if (!exactSid.test(sessionId) || sessionId.length > 512) return { error: 'session_id' };
    if (!text || text.length > 20000) return { error: 'text' };
    const draftType = request.draftType === 1 ? 1 : 0;
    const reply = request.reply && Object.prototype.toString.call(request.reply) === '[object Object]'
      ? request.reply : null;
    if (draftType === 1) {
      if (!reply || typeof reply.replyThreadId !== 'string' || !reply.replyThreadId
        || !Number.isSafeInteger(reply.replyThreadTime) || reply.replyThreadTime <= 0) {
        return { error: 'reply_binding' };
      }
    }
    const clean = value => String(value || '').replace(/\\u200b/g, '').replace(/\\r\\n?/g, '\\n').trim();
    const sameDraft = row => {
      if (!row || row.sessionId !== sessionId || row.draftType !== draftType) return false;
      if (draftType === 0) return true;
      return row.reply && row.reply.replyThreadId === reply.replyThreadId
        && Number(row.reply.replyThreadTime) === Number(reply.replyThreadTime);
    };
    const listDrafts = async () => {
      const config = await oneValue(sdk.query.getConfig(), 'config');
      const jid = String(config && config.jid || '');
      if (!jid || jid.length > 512) throw new Error('config');
      const rows = [];
      let lastValue = null;
      let hasMore = true;
      while (hasMore) {
        if (rows.length > 900) throw new Error('limit');
        const args = { userId: jid, limit: 300 };
        if (lastValue !== null) args.lastValue = lastValue;
        const page = await oneValue(sdk.query.fetchDraftsList(args), 'drafts_list');
        if (!page || !Array.isArray(page.data) || typeof page.hasMore !== 'boolean') throw new Error('shape');
        rows.push(...page.data);
        hasMore = page.hasMore && rows.length < 900;
        if (hasMore) {
          if (!Number.isSafeInteger(page.lastValue) || page.lastValue <= 0
            || (lastValue !== null && page.lastValue <= lastValue)) throw new Error('cursor');
          lastValue = page.lastValue;
        }
      }
      return rows;
    };
    const receiptOf = row => {
      const body = clean(row && row.draftContent && row.draftContent.body);
      return {
        draftId: String(row.draftId || ''),
        sessionId: String(row.sessionId || ''),
        draftType: row.draftType,
        modifiedTime: String(row.modifiedTime || ''),
        createTime: row.createTime,
        sendTime: row.sendTime,
        body: body,
        replyThreadId: row.reply && row.reply.replyThreadId || null,
        replyThreadTime: row.reply && row.reply.replyThreadTime || null,
      };
    };
    stage = 'drafts_list';
    const before = await listDrafts();
    const existing = before.filter(sameDraft);
    if (existing.length > 1) return { error: 'draft_ambiguous' };
    const prior = existing[0] || null;
    if (prior && clean(prior.draftContent && prior.draftContent.body) === text.trim()) {
      const receipt = receiptOf(prior);
      // Saving a draft must not mark the chat unread. The runner re-badges
      // at most once, only after it opened a chat that was already unread.
      let unread = false;
      return { ok: true, already: true, receipt, postsave: receipt, unread };
    }
    if (prior && !request.supersede && clean(prior.draftContent && prior.draftContent.body) !== text.trim()) {
      return { error: 'existing_draft_conflict', draftId: String(prior.draftId || '') };
    }
    const payload = {
      sessionId,
      draftType,
      sendTime: 0,
      isAnnouncement: false,
      asText: true,
      body: text,
      fontStyle: [],
      mentions: [],
      messageContent: [],
      id: request.msgId || crypto.randomUUID(),
    };
    if (draftType === 1) {
      payload.reply = {
        replyThreadId: reply.replyThreadId,
        replyThreadTime: reply.replyThreadTime,
        replyOwnerJid: typeof reply.replyOwnerJid === 'string' ? reply.replyOwnerJid : '',
        mainMsgId: typeof reply.mainMsgId === 'string' ? reply.mainMsgId : '',
      };
    }
    let mutation;
    if (prior) {
      stage = 'update_draft';
      mutation = await oneValue(sdk.mutation.updateDraft({
        ...payload,
        draftId: String(prior.draftId),
        modifiedTime: Number(prior.modifiedTime) || Date.now(),
      }), 'update_draft', 12000);
    } else {
      stage = 'create_draft';
      mutation = await oneValue(sdk.mutation.createDraft({
        ...payload,
        draftId: request.draftId || crypto.randomUUID(),
        createTime: Date.now(),
      }), 'create_draft', 12000);
    }
    stage = 'readback';
    const after = await listDrafts();
    const matches = after.filter(row => sameDraft(row) && clean(row.draftContent && row.draftContent.body) === text.trim());
    if (matches.length !== 1) return { error: 'readback', mutation_keys: mutation && Object.keys(mutation) };
    const receipt = receiptOf(matches[0]);
    if (!receipt.draftId || receipt.sessionId !== sessionId) return { error: 'readback' };
    // Saving a draft must not mark the chat unread. The runner re-badges
    // at most once, only after it opened a chat that was already unread.
    let unread = false;
    return {
      ok: true, already: false, created: !prior, receipt, postsave: receipt,
      mutation_draftId: mutation && mutation.draftId || null, unread,
    };
    } catch (_) {
      return { error: stage };
    }
  })()
`;
}

function historyEvaluation(request) {
  return `
  (async () => {
    const request = ${JSON.stringify(request)};
    let stage = 'resource_discovery';
    try {
    ${PAGE_BOOTSTRAP}
    const exactSid = /^[^\\s@]+@(?:xmpp|conference\\.xmpp)\\.zoom\\.us$/i;
    const sids = [...new Set((Array.isArray(request.sids) ? request.sids : [])
      .filter(value => typeof value === 'string' && exactSid.test(value)))].slice(0, 20);
    if (!sids.length) return { ok: true, threads: [] };
    const config = await oneValue(sdk.query.getConfig(), 'config', 4000);
    const selfJid = String(config && config.jid || '');
    if (!exactSid.test(selfJid)) return { error: 'self_jid' };
    const threads = [];
    for (const sid of sids) {
      let raw;
      try {
        raw = await oneValue(sdk.query.fetchHistoryLatestMainMsg({ session: sid, limit: 50 }), 'history', 8000);
      } catch (_) {
        threads.push({ sid, ok: false, error: 'history_failed', messages: [] });
        continue;
      }
      if (!Array.isArray(raw) || !raw.length) {
        threads.push({ sid, ok: false, error: 'history_empty', messages: [] });
        continue;
      }
      const messages = [];
      for (const row of raw) {
        if (!row || row.visible === false) continue;
        const id = String(row.id || '');
        const sender = String(row.senderName || '').trim();
        let text = String(row.body || '').replace(/\\u200b/g, '').trim();
        if (!text && sender && Array.isArray(row.files) && row.files.length)
          text = '[shared ' + row.files.length + ' file(s)/image(s)]';
        const time = Number(row.time);
        if (!id || !Number.isSafeInteger(time) || time <= 0 || String(row.sessionId || '') !== sid) continue;
        messages.push({
          message_id: id,
          time,
          text,
          from_self: String(row.senderJid || row.from || '') === selfJid,
          sender,
        });
      }
      threads.push({ sid, ok: messages.length > 0, messages });
    }
    return { ok: true, threads };
    } catch (_) {
      return { error: stage };
    }
  })()
`;
}

function evalValue(result, label) {
  const value = result.result && result.result.value;
  if (value && typeof value.error === 'string') throw new Error(`Zoom ${label} ${value.error} failed`);
  if (result.exceptionDetails) {
    const name = String(result.exceptionDetails.exception && result.exceptionDetails.exception.className || '');
    const code = ['SyntaxError', 'ReferenceError', 'TypeError'].includes(name) ? name.toLowerCase() : 'runtime_exception';
    const line = Number.isSafeInteger(result.exceptionDetails.lineNumber) ? result.exceptionDetails.lineNumber + 1 : 0;
    const column = Number.isSafeInteger(result.exceptionDetails.columnNumber) ? result.exceptionDetails.columnNumber + 1 : 0;
    const description = String(result.exceptionDetails.exception && result.exceptionDetails.exception.description || '');
    const parser = /Unexpected identifier/.test(description) ? 'unexpected_identifier'
      : /Unexpected token/.test(description) ? 'unexpected_token'
      : /Invalid or unexpected token/.test(description) ? 'invalid_token' : '';
    throw new Error('Zoom ' + label + ' ' + code + (parser ? '_' + parser : '') + '_line_' + line + '_column_' + column);
  }
  return value;
}

function canonicalDraft(text) {
  return String(text || '').replace(/\u200b/g, '').replace(/\r\n?/g, '\n').trim();
}

function draftsMatch(a, b) {
  const na = canonicalDraft(a).replace(/\s+/g, ' ');
  const nb = canonicalDraft(b).replace(/\s+/g, ' ');
  if (!na || !nb) return false;
  return na === nb || nb.startsWith(na.slice(0, 48)) || na.startsWith(nb.slice(0, 48));
}

function composerDomHelpers() {
  return `
    const exactConversation = /^[^\\s@]+@(?:xmpp|conference\\.xmpp)\\.zoom\\.us$/i;
    const bareSid = v => {
      const raw = String(v || '');
      const stripped = raw.replace(/^[a-z_]+(?:-[A-Z]{2,})+-/, '');
      return exactConversation.test(stripped) ? stripped : '';
    };
    const shown = el => {
      if (!el || el.offsetParent === null) return false;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    };
    const rowsFor = s => [...document.querySelectorAll('li[class*="_sessionItem_"]')]
      .filter(el => bareSid(el.getAttribute('data-sessionid') || el.getAttribute('data-testid') || '') === s);
    const rowFor = s => {
      const hits = rowsFor(s);
      return hits.find(shown) || hits[0] || null;
    };
    const realClick = el => {
      const t = el.querySelector('[role="button"],a,button') || el;
      const r = t.getBoundingClientRect();
      const o = {bubbles:true, cancelable:true, view:window, button:0,
                 clientX:r.left + r.width/2, clientY:r.top + r.height/2};
      for (const type of ['pointerover','pointerdown','mousedown','pointerup','mouseup','click']) {
        const C = type.indexOf('pointer') === 0 ? (window.PointerEvent || MouseEvent) : MouseEvent;
        try { t.dispatchEvent(new C(type, o)); } catch (_) {}
      }
      try { t.click(); } catch (_) {}
    };
    const activeSid = () => {
      const rows = [...document.querySelectorAll('li[class*="_sessionItem_"]')];
      const marked = rows.find(r => (
        /select|active|current/i.test((r.className || '').toString())
        || r.getAttribute('aria-selected') === 'true'
        || r.getAttribute('aria-current')
      ));
      if (!marked) return '';
      return bareSid(marked.getAttribute('data-sessionid') || marked.getAttribute('data-testid') || '');
    };
    const headerName = () => {
      const raw = (el) => String((el && el.innerText) || '').split('\\n')[0].trim();
      const skip = t => /download the zoom app/i.test(t);
      const h = document.querySelector('[class*="_sessionHeader_"],[class*="conversationHeader"],[class*="chatHeader"]');
      const ht = raw(h);
      if (ht && !skip(ht)) return ht;
      const t = document.querySelector('[class*="_title_"],[class*="headerTitle"]');
      const tt = raw(t);
      return skip(tt) ? '' : tt;
    };
    const dismissPromo = () => {
      const label = el => ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
      for (const n of document.querySelectorAll('button,a,[role="button"]')) {
        const t = label(n);
        if (/continue in (the )?browser|use (zoom )?in (the )?browser|not now|maybe later|skip for now/.test(t)) {
          try { n.click(); } catch (_) {}
        }
      }
      for (const h of document.querySelectorAll('h1,h2,h3,[class*="title"]')) {
        if (!/download the zoom app/i.test(h.innerText || '')) continue;
        const root = h.closest('[role="dialog"],[class*="modal"],[class*="dialog"],[class*="banner"]') || h.parentElement;
        if (!root) continue;
        const close = [...root.querySelectorAll('button,[aria-label],a')].find(b =>
          /close|dismiss|not now|later|skip|browser/i.test(label(b)));
        if (close) { try { close.click(); } catch (_) {} }
      }
    };
    const mainEditor = () => {
      const editors = [...document.querySelectorAll('[data-slate-editor="true"]')]
        .filter(shown)
        .filter(e => !e.closest('[data-reply-id]'))
        .filter(e => !e.closest('[class*="search"],[class*="Search"]'));
      const usable = editors.filter(e => {
        const r = e.getBoundingClientRect();
        return r.width >= 80 && r.height >= 18;
      });
      if (!usable.length) return { count: editors.length, box: null, text: '' };
      usable.sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
      const e = usable[usable.length - 1];
      const r = e.getBoundingClientRect();
      const text = String(e.innerText || '').replace(/\\u200b/g, '')
        .replace(/(shift|option)\\s*\\+\\s*enter.*/ig, '')
        .replace(/\\bnew line\\b/ig, '').trim();
      return { count: usable.length, box: { x: r.x, y: r.y, w: r.width, h: r.height }, text };
    };
    const snapshot = () => {
      const editor = mainEditor();
      return {
        activeSid: activeSid(),
        header: headerName(),
        editorCount: editor.count,
        composerText: editor.text,
        box: editor.box,
      };
    };
    const scrollLists = () => {
      const boxes = new Set();
      for (const li of document.querySelectorAll('li[class*="_sessionItem_"]')) {
        let box = li.parentElement, fallback = null, preferred = null;
        for (let i = 0; i < 14 && box; i++, box = box.parentElement) {
          if (box.scrollHeight <= box.clientHeight + 20) continue;
          if (!fallback) fallback = box;
          const oy = window.getComputedStyle(box).overflowY;
          if (['auto','scroll','overlay'].includes(oy)) { preferred = box; break; }
        }
        const chosen = preferred || fallback;
        if (chosen) boxes.add(chosen);
      }
      for (const b of boxes) {
        const max = Math.max(0, b.scrollHeight - b.clientHeight);
        b.scrollTop = b.scrollTop >= max - 4 ? 0 : Math.min(b.scrollTop + Math.floor(b.clientHeight * 0.85), max);
        b.dispatchEvent(new Event('scroll', {bubbles:true}));
      }
      return boxes.size;
    };
    const expandDms = () => {
      const nodes = [...document.querySelectorAll('[class*="_groupItemWrapper_"],[class*="_sectionHeader_"]')];
      for (const n of nodes) {
        const t = (n.innerText || '').toLowerCase();
        if (t.includes('dm') || t.includes('starred') || t.includes('chat')) {
          try { n.click(); } catch (_) {}
        }
      }
    };
`;
}

function hydrateOpenEvaluation(sessionId) {
  return `
  (async () => {
    const sessionId = ${JSON.stringify(sessionId)};
    ${composerDomHelpers()}
    expandDms();
    dismissPromo();
    await new Promise(r => setTimeout(r, 400));
    let opened = false;
    for (let i = 0; i < 36; i++) {
      dismissPromo();
      let li = rowFor(sessionId);
      if (li) {
        if (!shown(li)) {
          try { li.scrollIntoView({block:'center'}); } catch (_) {}
          await new Promise(r => setTimeout(r, 250));
          li = rowFor(sessionId);
        }
        if (li && shown(li)) {
          realClick(li);
          await new Promise(r => setTimeout(r, 700));
          if (activeSid() === sessionId) { opened = true; break; }
        }
      }
      scrollLists();
      await new Promise(r => setTimeout(r, 350));
    }
    if (opened) {
      for (let j = 0; j < 24; j++) {
        dismissPromo();
        if (mainEditor().box) break;
        await new Promise(r => setTimeout(r, 500));
      }
    }
    return { opened, ...snapshot() };
  })()
`;
}

function hydrateReadEvaluation() {
  return `
  (() => {
    ${composerDomHelpers()}
    return snapshot();
  })()
`;
}

async function pageEval(client, cdpSession, expression, timeoutMs) {
  const result = await client.command('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
  }, cdpSession, timeoutMs);
  return evalValue(result, 'sdk');
}

async function shiftEnter(client, cdpSession) {
  await client.command('Input.dispatchKeyEvent', {
    type: 'keyDown', key: 'Shift', code: 'ShiftLeft',
    windowsVirtualKeyCode: 16, nativeVirtualKeyCode: 16, modifiers: 8,
  }, cdpSession);
  await client.command('Input.dispatchKeyEvent', {
    type: 'rawKeyDown', key: 'Enter', code: 'Enter',
    windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, modifiers: 8,
  }, cdpSession);
  await client.command('Input.dispatchKeyEvent', {
    type: 'keyUp', key: 'Enter', code: 'Enter',
    windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, modifiers: 8,
  }, cdpSession);
  await client.command('Input.dispatchKeyEvent', {
    type: 'keyUp', key: 'Shift', code: 'ShiftLeft',
    windowsVirtualKeyCode: 16, nativeVirtualKeyCode: 16, modifiers: 0,
  }, cdpSession);
}

async function clickPoint(client, cdpSession, x, y) {
  await client.command('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y }, cdpSession);
  await client.command('Input.dispatchMouseEvent', {
    type: 'mousePressed', x, y, button: 'left', clickCount: 1,
  }, cdpSession);
  await client.command('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x, y, button: 'left', clickCount: 1,
  }, cdpSession);
}

function refuseSendLabel(label) {
  const value = String(label || '').trim();
  return /^send$/i.test(value) || /more send options/i.test(value);
}

async function trustedClick(client, cdpSession, point) {
  if (!point || refuseSendLabel(point.label)) throw new Error('refusing_send_control');
  const x = Number(point.x);
  const y = Number(point.y);
  if (!Number.isFinite(x) || !Number.isFinite(y)) throw new Error('bad_click');
  await clickPoint(client, cdpSession, x, y);
}

async function selectAllBackspace(client, cdpSession) {
  await client.command('Input.dispatchKeyEvent', {
    type: 'rawKeyDown', key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65, modifiers: 4, commands: ['selectAll'],
  }, cdpSession);
  await client.command('Input.dispatchKeyEvent', {
    type: 'keyUp', key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65, modifiers: 4,
  }, cdpSession);
  await client.command('Input.dispatchKeyEvent', {
    type: 'rawKeyDown', key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8,
  }, cdpSession);
  await client.command('Input.dispatchKeyEvent', {
    type: 'keyUp', key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8,
  }, cdpSession);
}

async function typeDraftText(client, cdpSession, text) {
  const lines = canonicalDraft(text).split('\n');
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i]) {
      await client.command('Input.insertText', { text: lines[i] }, cdpSession);
    }
    if (i < lines.length - 1) {
      await shiftEnter(client, cdpSession);
      await new Promise(resolve => setTimeout(resolve, 80));
    }
  }
}

async function hydrateComposer(client, cdpSession, sessionId, text, options) {
  const exact = !!(options && options.exact);
  let snap = await pageEval(client, cdpSession, hydrateReadEvaluation(), 12_000);
  const present = snap && snap.box && (
    snap.activeSid === sessionId || draftsMatch(snap.composerText, text)
  );
  if (!present) {
    snap = await pageEval(client, cdpSession, hydrateOpenEvaluation(sessionId), HYDRATE_EVAL_MS);
    if (!snap || snap.opened !== true || snap.activeSid !== sessionId) {
      return { ok: false, error: 'composer_thread_unopened', snap };
    }
  } else if (snap.activeSid && snap.activeSid !== sessionId) {
    return { ok: false, error: 'composer_thread_unopened', snap };
  }
  if (!snap.box) {
    return { ok: false, error: 'main_composer_ambiguous', snap };
  }
  const wanted = canonicalDraft(text);
  const have = canonicalDraft(snap.composerText);
  const extra = have.length - wanted.length;
  const flat = value => String(value || '').replace(/\s+/g, '');
  const held = have === wanted || flat(have) === flat(wanted) || (extra >= 0 && extra < 180 && have.indexOf(wanted) >= 0);
  const sameExact = have === wanted;
  if (draftsMatch(snap.composerText, text) && (!exact || held)) {
    return {
      ok: true, already: true, exact: sameExact, header: snap.header,
      chars: String(snap.composerText || '').length,
    };
  }
  const composerCanon = canonicalDraft(snap.composerText);
  const rewrite = exact && draftsMatch(snap.composerText, text);
  // Placeholder/aria hint is not an operator draft — do not block hydrate.
  if (!rewrite && composerCanon && !/^write a message or type \/? for more$/i.test(composerCanon)) {
    return { ok: false, error: 'existing_composer_conflict', snap };
  }
  const x = snap.box.x + Math.min(36, Math.max(8, snap.box.w * 0.12));
  const y = snap.box.y + snap.box.h / 2;
  await clickPoint(client, cdpSession, x, y);
  await new Promise(resolve => setTimeout(resolve, 250));
  if (rewrite) {
    await selectAllBackspace(client, cdpSession);
    await new Promise(resolve => setTimeout(resolve, 200));
    await selectAllBackspace(client, cdpSession);
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  await typeDraftText(client, cdpSession, text);
  await new Promise(resolve => setTimeout(resolve, 500));
  const after = await pageEval(client, cdpSession, hydrateReadEvaluation(), 12_000);
  if (!after || (after.activeSid && after.activeSid !== sessionId)) {
    return { ok: false, error: 'composer_thread_drifted', snap: after };
  }
  if (!draftsMatch(after.composerText, text)) {
    return { ok: false, error: 'composer_readback_mismatch', snap: after };
  }
  if (exact && canonicalDraft(after.composerText) !== canonicalDraft(text)) {
    return {
      ok: true, already: false, exact: false, header: after.header,
      chars: String(after.composerText || '').length,
    };
  }
  return {
    ok: true, already: false, exact: canonicalDraft(after.composerText) === canonicalDraft(text),
    header: after.header, chars: String(after.composerText || '').length,
  };
}

// 2026-10-12: empty the on-screen composer after an API draft delete. Only acts when the open chat is exactly
// sessionId and the composer holds exactly expectText. Select-all + Backspace only; never Enter. Never sends.
async function clearComposer(client, cdpSession, sessionId, expectText) {
  const empty = t => { const c = canonicalDraft(t); return !c || /^write a message or type \/? for more$/i.test(c); };
  const snap = await pageEval(client, cdpSession, hydrateOpenEvaluation(sessionId), HYDRATE_EVAL_MS);
  if (!snap || snap.opened !== true || snap.activeSid !== sessionId || !snap.box) return { ok: false, error: 'composer_thread_unopened', snap };
  if (empty(snap.composerText)) return { ok: true, already_empty: true };
  if (!draftsMatch(snap.composerText, expectText)) return { ok: false, error: 'composer_text_not_ours', snap };
  await clickPoint(client, cdpSession, snap.box.x + Math.min(36, Math.max(8, snap.box.w * 0.12)), snap.box.y + snap.box.h / 2);
  await new Promise(r => setTimeout(r, 200));
  for (let pass = 0; pass < 3; pass += 1) {
    await client.command('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65, modifiers: 4, commands: ['selectAll'] }, cdpSession);
    await client.command('Input.dispatchKeyEvent', { type: 'keyUp', key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65, modifiers: 4 }, cdpSession);
    await client.command('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8 }, cdpSession);
    await client.command('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8 }, cdpSession);
    await new Promise(r => setTimeout(r, 400));
    const after = await pageEval(client, cdpSession, hydrateReadEvaluation(), 12_000);
    if (!after || after.activeSid !== sessionId) return { ok: false, error: 'composer_thread_drifted', snap: after };
    if (empty(after.composerText)) return { ok: true, passes: pass + 1 };
  }
  return { ok: false, error: 'composer_not_empty' };
}
function markUnreadEvaluation(sessionId) {
  return `(async () => { try { ${PAGE_BOOTSTRAP}
    if (typeof sdk.mutation.markChatAsUnread !== 'function') return { unread: false };
    const m = await oneValue(sdk.mutation.markChatAsUnread({ sessionId: ${JSON.stringify(sessionId)} }), 'mark_unread');
    return { unread: Boolean(m && m.sessionId === ${JSON.stringify(sessionId)}) }; } catch (_) { return { unread: false }; } })()`;
}

async function withClient(fn) {
  const client = new RawCdp(await cdpDiscovery());
  let cdpSession = '';
  let targetId = '';
  try {
    await client.connect();
    const { targetInfos } = await client.command('Target.getTargets');
    const pages = Array.isArray(targetInfos)
      ? targetInfos.filter(info => (
        info.type === 'page' && /^https:\/\/app\.zoom\.us(?:\/|$)/.test(String(info.url || ''))
      ))
      : [];
    // The Workplace shell at /wc/team-chat does not boot nx-chat. The live
    // client is https://app.zoom.us/team-chat?integrate=true (Zoom 7.0.5).
    const pageRank = url => {
      const value = String(url || '');
      if (/\/team-chat\?integrate=true(?:&|$)/.test(value)) return 0;
      if (/\/team-chat(?:\/|\?|$)/.test(value) && !/\/wc\/team-chat/.test(value)) return 1;
      if (/\/wc\/team-chat(?:\/|\?|$)/.test(value)) return 2;
      if (/\/wc\//.test(value)) return 3;
      return 4;
    };
    pages.sort((a, b) => pageRank(a.url) - pageRank(b.url));
    const target = pages[0];
    if (!target || !target.targetId) throw new Error('no existing authenticated Zoom web page');
    targetId = target.targetId;
    ({ sessionId: cdpSession } = await client.command('Target.attachToTarget', { targetId, flatten: true }));
    if (!cdpSession) throw new Error('Zoom CDP did not create a page session');
    return await fn(client, cdpSession);
  } finally {
    if (cdpSession && targetId) {
      try { await client.command('Target.detachFromTarget', { sessionId: cdpSession }); } catch {}
    }
    client.close();
  }
}

async function withPage(evaluate, timeoutMs) {
  return withClient((client, cdpSession) => pageEval(client, cdpSession, evaluate, timeoutMs));
}

async function drafts() {
  const result = await withPage(DRAFTS_EVALUATION, DRAFTS_EVAL_MS);
  if (!result || !Array.isArray(result.rows)) {
    throw new Error('Zoom drafts request returned an invalid result');
  }
  process.stdout.write(`${JSON.stringify({
    ok: true,
    action: 'drafts',
    data: result.rows,
    write: { supported: true, via: 'sdk.mutation.createDraft' },
  })}\n`);
}

// delete (discard) one draft; 2026-10-12 for QWEN-AB-TEST cleanup. Requires exact --session-id, --draft-id and
// --expect-body (must equal the stored body) so it can only remove the draft the caller staged. Never sends.
function deleteEvaluation(request) {
  return `
  (async () => {
    const request = ${JSON.stringify(request)};
    let stage = 'resource_discovery';
    try {
    ${PAGE_BOOTSTRAP}
    stage = 'config_query';
    const config = await oneValue(sdk.query.getConfig(), 'config');
    const jid = String(config && config.jid || '');
    const list = async () => (await oneValue(sdk.query.fetchDraftsList({ userId: jid, limit: 300 }), 'drafts_list')).data || [];
    stage = 'find';
    const row = (await list()).find(r => r.draftId === request.draftId && r.sessionId === request.sessionId);
    if (!row) return { error: 'not_found' };
    const body = String(row.draftContent && row.draftContent.body || '');
    if (body !== request.expectBody) return { error: 'body_mismatch' };
    stage = 'delete';
    const del = sdk.mutation.deleteDraft || (sdk.sessionModule && sdk.sessionModule.deleteDraft);
    if (typeof del !== 'function') return { error: 'no_delete' };
    await del({ sessionId: request.sessionId, draftId: request.draftId });
    stage = 'verify';
    const still = (await list()).some(r => r.draftId === request.draftId);
    return { deleted: !still };
    } catch (_) { return { error: stage }; }
  })()`;
}
async function deleteDraftAction(args) {
  const request = { sessionId: argValue(args, '--session-id'), draftId: argValue(args, '--draft-id'), expectBody: readExpect(args) };
  if (!request.sessionId || !request.draftId || !request.expectBody) throw new Error('delete needs --session-id --draft-id --expect-body[-file]');
  const clear = args.includes('--clear-composer'); const keepUnread = args.includes('--keep-unread');
  const out = await withClient(async (client, cdpSession) => {
    const r = await pageEval(client, cdpSession, deleteEvaluation(request), DRAFTS_EVAL_MS);
    let composer = null, unread = null;
    if (clear && r && (r.deleted || r.error === 'not_found')) composer = await clearComposer(client, cdpSession, request.sessionId, request.expectBody);
    if (keepUnread) unread = (await pageEval(client, cdpSession, markUnreadEvaluation(request.sessionId), DRAFTS_EVAL_MS) || {}).unread;
    return { r, composer, unread };
  });
  const ok = !!(out.r && (out.r.deleted || (clear && out.r.error === 'not_found'))) && (!clear || !!(out.composer && out.composer.ok));
  process.stdout.write(JSON.stringify({ ok, action: 'delete', result: out.r, composer: out.composer, unread: out.unread }) + '\n');
}
function readExpect(args) { const f = argValue(args, '--expect-body-file'); return f ? fs.readFileSync(f, 'utf8') : argValue(args, '--expect-body'); }

function argValue(args, name) {
  const index = args.indexOf(name);
  if (index < 0 || index + 1 >= args.length) return '';
  return String(args[index + 1] || '');
}

function readText(args) {
  const file = argValue(args, '--text-file');
  if (file) return fs.readFileSync(file, 'utf8');
  return argValue(args, '--text');
}

function attachPaths(args) {
  const out = [];
  for (let i = 0; i < args.length; i += 1) {
    if (args[i] !== '--attach') continue;
    const file = String(args[i + 1] || '');
    if (!file || file.startsWith('--')) throw new Error('save --attach needs a path');
    const abs = path.resolve(file);
    let st;
    try { st = fs.statSync(abs); } catch (_) { throw new Error('attach file is missing'); }
    if (!st.isFile() || st.size <= 0 || st.size > ATTACH_MAX_BYTES) throw new Error('attach file is an unsupported size');
    out.push(abs);
    i += 1;
  }
  return out;
}

function idKey(bag, field) {
  const rows = bag && Array.isArray(bag[field]) ? bag[field] : [];
  return rows.join('\n');
}

function sameIds(left, right) {
  return idKey(left, 'ids') === idKey(right, 'ids');
}

function selfChanged(left, right) {
  return idKey(left, 'selfIds') !== idKey(right, 'selfIds');
}

function attachStoreDir(sessionId) {
  return path.join(os.homedir(), 'claude-stack', 'data', 'zoom_attachments', sessionId);
}

const FILE_ROOT_JS = `
    const nxFileModule = async () => {
      const chunk = new URL('chunk-packages-sdk.js', urls[0]);
      if (chunk.protocol !== 'https:' || !/(^|\\.)zoom\\.us$/i.test(chunk.hostname) || chunk.username || chunk.password || chunk.port
        || !/^\\/fe-static\\/nx-chat\\/[^/]+\\/chunk-packages-sdk\\.js$/.test(chunk.pathname)) {
        throw new Error('chunk');
      }
      const mod = await import(chunk.href);
      let getSDK = null;
      for (const value of Object.values(mod || {})) {
        if (typeof value !== 'function' || value.length !== 0) continue;
        if (String(value).includes('SDK instance not initialized')) { getSDK = value; break; }
      }
      if (!getSDK) throw new Error('sdk');
      const root = getSDK();
      const file = root && root.fileModule;
      const auth = root && root.auth;
      if (!file || !auth || typeof file.uploadFile !== 'function' || typeof file.shareFiles !== 'function') throw new Error('file_module');
      return { file, auth };
    };
    const nxBytes = async (value) => {
      const data = value && Object.prototype.toString.call(value) === '[object Object]' && ('data' in value) ? value.data : value;
      if (!data) return null;
      if (typeof data === 'string') {
        if (!data.startsWith('data:')) return null;
        const raw = data.split(',')[1] || '';
        if (!raw) return null;
        return { b64: raw, bytes: atob(raw).length };
      }
      let buf = null;
      if (typeof data.arrayBuffer === 'function') buf = await data.arrayBuffer();
      else if (data instanceof ArrayBuffer) buf = data;
      else if (ArrayBuffer.isView(data)) buf = data.buffer;
      if (!buf) return null;
      if (buf.byteLength > ${ATTACH_DOWNLOAD_MAX_BYTES}) return { tooBig: true, bytes: buf.byteLength };
      const bytes = new Uint8Array(buf);
      let bin = '';
      for (let i = 0; i < bytes.length; i += 8192) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
      }
      return { b64: btoa(bin), bytes: bytes.length };
    };
`;

function historyIdsEvaluation(sessionId) {
  return `
  (async () => {
    const sessionId = ${JSON.stringify(sessionId)};
    let stage = 'resource_discovery';
    try {
    ${PAGE_BOOTSTRAP}
    stage = 'history';
    const config = await oneValue(sdk.query.getConfig(), 'config', 4000);
    const selfJid = String(config && config.jid || '');
    const raw = await oneValue(sdk.query.fetchHistoryLatestMainMsg({ session: sessionId, limit: 50 }), 'history', 8000);
    const rows = Array.isArray(raw) ? raw : [];
    const ids = [];
    const selfIds = [];
    for (const row of rows) {
      if (!row || row.visible === false) continue;
      const id = String(row.id || '');
      const time = Number(row.time);
      if (!id || !Number.isSafeInteger(time) || time <= 0) continue;
      const key = id + ':' + String(time);
      ids.push(key);
      if (selfJid && String(row.senderJid || row.from || '') === selfJid) selfIds.push(key);
    }
    return { ok: true, ids, selfIds };
    } catch (_) { return { error: stage }; }
  })()`;
}

function draftProofEvaluation(sessionId, text, names) {
  return `
  (async () => {
    const sessionId = ${JSON.stringify(sessionId)};
    const text = ${JSON.stringify(text)};
    const names = ${JSON.stringify(names)};
    let stage = 'resource_discovery';
    try {
    ${PAGE_BOOTSTRAP}
    stage = 'drafts_list';
    const config = await oneValue(sdk.query.getConfig(), 'config');
    const jid = String(config && config.jid || '');
    const page = await oneValue(sdk.query.fetchDraftsList({ userId: jid, limit: 300 }), 'drafts_list');
    const rows = (page && page.data) || [];
    const row = rows.find(item => item && item.sessionId === sessionId && item.draftType === 0);
    if (!row) return { error: 'draft_missing' };
    const body = String(row.draftContent && row.draftContent.body || '').replace(/\\u200b/g, '').replace(/\\r\\n?/g, '\\n').trim();
    const want = String(text || '').replace(/\\u200b/g, '').replace(/\\r\\n?/g, '\\n').trim();
    const marks = [];
    const types = [];
    const walk = (node, trail, skipBody) => {
      if (node == null) return;
      if (typeof node === 'string') {
        if (!skipBody) {
          for (const name of names) {
            if (name && node.indexOf(name) >= 0) marks.push(trail);
          }
        }
        return;
      }
      if (typeof node !== 'object') return;
      if (Array.isArray(node)) {
        node.forEach((item, index) => walk(item, trail + '.' + index, skipBody));
        return;
      }
      if (typeof node.type === 'string') types.push(node.type);
      Object.keys(node).forEach(key => {
        if ((key === 'fileId' || key === 'fileIds' || key === 'sharedFileId') && node[key]) marks.push(trail + '.' + key);
        walk(node[key], trail + '.' + key, skipBody || key === 'body');
      });
    };
    walk(row, 'row', false);
    return {
      ok: true,
      draftId: String(row.draftId || ''),
      sendTime: row.sendTime,
      bodyExact: body === want,
      bodyLen: body.length,
      marks: marks.slice(0, 12),
      contentTypes: types.slice(0, 24),
    };
    } catch (_) { return { error: stage }; }
  })()`;
}

function attachLocateEvaluation(sessionId, needle) {
  return `
  (() => {
    const sessionId = ${JSON.stringify(sessionId)};
    const needle = ${JSON.stringify(needle)};
    ${composerDomHelpers()}
    const bodyText = String((document.body && document.body.innerText) || '');
    const oops = /it seems that something went wrong/i.test(bodyText);
    const rectOf = el => {
      if (!el) return null;
      const rect = el.getBoundingClientRect();
      if (rect.width < 2 || rect.height < 2) return null;
      return {
        x: rect.left + Math.min(rect.width / 2, 180),
        y: rect.top + rect.height / 2,
        label: String(el.getAttribute('aria-label') || el.innerText || '').trim().slice(0, 80),
      };
    };
    const editor = mainEditor();
    const snap = snapshot();
    const identity = snap.activeSid === sessionId || (needle && String(editor.text || '').indexOf(needle) >= 0);
    const retryEl = [...document.querySelectorAll('button')].find(el => /^retry$/i.test(String(el.innerText || '').trim()));
    if (oops) return { phase: 'oops', retry: rectOf(retryEl), identity: false, ...snap };
    if (identity && editor.box) {
      return { phase: 'composer', identity: true, fileCount: document.querySelectorAll('input[type="file"]').length, ...snap };
    }
    const tab = [...document.querySelectorAll('button,[role="tab"],[role="button"]')]
      .find(el => String(el.getAttribute('aria-label') || '').startsWith('Drafts,'));
    const tabSelected = !!(tab && (tab.getAttribute('aria-selected') === 'true' || tab.getAttribute('aria-pressed') === 'true'));
    const hits = [...document.querySelectorAll('[role="option"]')].filter(el => needle && String(el.innerText || '').indexOf(needle) >= 0);
    let row = null;
    if (tabSelected && hits.length === 1) {
      const title = hits[0].querySelector('[class*="_title_"]');
      row = rectOf(title);
    }
    return { phase: tabSelected ? 'drafts' : 'list', tab: rectOf(tab), tabSelected, rowCount: hits.length, row, identity: false, ...snap };
  })()`;
}

function chipEvaluation(names) {
  return `
  (() => {
    const names = ${JSON.stringify(names)};
    const stems = names.map(name => String(name || '').replace(/\\.[^.]+$/, '')).filter(name => name.length >= 8);
    const input = document.querySelector('input[type="file"]');
    const got = [];
    if (input && input.files) {
      for (const file of input.files) got.push(String(file.name || ''));
    }
    const bagOf = el => [
      el && el.innerText, el && el.textContent,
      el && el.getAttribute && el.getAttribute('aria-label'),
      el && el.getAttribute && el.getAttribute('title'),
    ].join(' ');
    let domChip = false;
    const nodes = document.querySelectorAll('*');
    for (const el of nodes) {
      const bag = bagOf(el);
      if (stems.some(stem => bag.indexOf(stem) >= 0 || bag.indexOf(stem.slice(0, 18)) >= 0)) { domChip = true; break; }
      const root = el.shadowRoot;
      if (root && stems.some(stem => bagOf(root).indexOf(stem.slice(0, 18)) >= 0)) { domChip = true; break; }
    }
    const bodyBag = String((document.body && (document.body.innerText || document.body.textContent)) || '');
    const caption = /people in this chat can view/i.test(bodyBag);
    const stemHit = stems.some(stem => bodyBag.indexOf(stem) >= 0 || bodyBag.indexOf(stem.slice(0, 18)) >= 0);
    const namesOk = names.length > 0 && names.every(name => got.indexOf(name) >= 0) && got.length === names.length;
    return { names: got, namesOk, domChip, caption, stemHit, chip: namesOk && (domChip || caption || stemHit) };
  })()`;
}

function draftFileEvaluation(sessionId, names) {
  return `
  (async () => {
    const sessionId = ${JSON.stringify(sessionId)};
    const names = ${JSON.stringify(names)};
    let stage = 'resource_discovery';
    try {
    ${PAGE_BOOTSTRAP}
    ${FILE_ROOT_JS}
    stage = 'file_root';
    const handle = await nxFileModule();
    const input = document.querySelector('input[type="file"]');
    if (!input || !input.files || input.files.length !== names.length) return { error: 'input_files' };
    const config = await oneValue(handle.auth.getConfig(), 'config');
    const ownerId = String(config && config.jid || '');
    if (!ownerId) return { error: 'owner' };
    const fileIds = [];
    for (const file of input.files) {
      if (names.indexOf(file.name) < 0) return { error: 'name_mismatch' };
      stage = 'upload_draft';
      const uploaded = await oneValue(handle.file.uploadFile(
        { file, sessionId, isDraft: true },
        { onProgress: function () {} }
      ), 'upload_draft', 60000);
      const fileId = uploaded && (uploaded.fileId || uploaded.id);
      if (!fileId) return { error: 'upload_no_id' };
      fileIds.push(String(fileId));
    }
    stage = 'share_draft';
    const draftFlag = true;
    if (draftFlag !== true) return { error: 'refusing_publish' };
    await oneValue(handle.file.shareFiles({
      fileIds, sessions: [sessionId], ownerId, isDraft: draftFlag,
    }), 'share_draft', 20000);
    return { ok: true, fileIds };
    } catch (err) { return { failed: stage, detail: String(err && err.message || err).slice(0, 160) }; }
  })()`;
}

function listFilesEvaluation(sessionId, sinceMs) {
  return `
  (async () => {
    const sessionId = ${JSON.stringify(sessionId)};
    const sinceMs = ${JSON.stringify(sinceMs)};
    let stage = 'resource_discovery';
    try {
    ${PAGE_BOOTSTRAP}
    stage = 'history';
    const raw = await oneValue(sdk.query.fetchHistoryLatestMainMsg({ session: sessionId, limit: 50 }), 'history', 8000);
    const rows = Array.isArray(raw) ? raw : [];
    const files = [];
    const take = item => {
      if (!item || typeof item !== 'object') return;
      const fileId = String(item.fileId || item.id || '');
      const name = String(item.fileName || item.name || item.file_name || '');
      if (!fileId || !name) return;
      files.push({
        fileId,
        name,
        size: Number(item.fileSize || item.size || 0) || 0,
        type: String(item.fileType || item.type || item.ext || ''),
      });
    };
    for (const row of rows) {
      if (!row || row.visible === false) continue;
      const time = Number(row.time);
      if (!Number.isSafeInteger(time) || time <= 0) continue;
      if (sinceMs && time < sinceMs) continue;
      const buckets = [];
      if (Array.isArray(row.files)) buckets.push(...row.files);
      if (Array.isArray(row.attachments)) buckets.push(...row.attachments);
      for (const item of buckets) {
        const before = files.length;
        take(item);
        if (files.length > before) {
          files[files.length - 1].messageId = String(row.id || '');
          files[files.length - 1].time = time;
        }
      }
    }
    return { ok: true, files: files.slice(0, 20) };
    } catch (_) { return { error: stage }; }
  })()`;
}

function downloadEvaluation(file) {
  const image = /image|png|jpe?g|gif|webp/i.test(String(file.type || '') + ' ' + String(file.name || ''));
  return `
  (async () => {
    const file = ${JSON.stringify({ fileId: file.fileId, name: file.name, image })};
    let stage = 'resource_discovery';
    try {
    ${PAGE_BOOTSTRAP}
    ${FILE_ROOT_JS}
    stage = 'file_root';
    const handle = await nxFileModule();
    const attempts = [];
    if (typeof handle.file.downloadFile === 'function') {
      attempts.push(() => handle.file.downloadFile({ fileId: file.fileId, fileName: file.name, isImage: false }));
    }
    if (file.image && typeof handle.file.fetchImageMsgFile === 'function') {
      attempts.push(() => handle.file.fetchImageMsgFile({ fileId: file.fileId }));
    }
    if (!file.image && typeof handle.file.fetchImageMsgFile === 'function') {
      attempts.push(() => handle.file.fetchImageMsgFile({ fileId: file.fileId }));
    }
    let last = 'no_bytes';
    for (const run of attempts) {
      stage = 'download';
      try {
        const value = await oneValue(run(), 'download', 20000);
        const bytes = await nxBytes(value);
        if (bytes && bytes.tooBig) return { error: 'too_large', bytes: bytes.bytes };
        if (bytes && bytes.b64) return { ok: true, b64: bytes.b64, bytes: bytes.bytes };
        last = 'unexpected_payload';
      } catch (err) {
        last = String(err && err.message || err).slice(0, 120);
      }
    }
    return { error: last };
    } catch (_) { return { error: stage }; }
  })()`;
}

async function readHistoryIds(client, cdpSession, sessionId) {
  const value = await pageEval(client, cdpSession, historyIdsEvaluation(sessionId), 20_000);
  if (!value || value.ok !== true || !Array.isArray(value.ids) || !Array.isArray(value.selfIds)) {
    throw new Error('history_ids_unproven');
  }
  return { ids: value.ids, selfIds: value.selfIds };
}

function proofHasFile(proof, names) {
  if (!proof || proof.ok !== true) return false;
  const marks = Array.isArray(proof.marks) ? proof.marks.join(' ') : '';
  if (names.some(name => name && marks.indexOf(name) >= 0)) return true;
  const types = Array.isArray(proof.contentTypes) ? proof.contentTypes : [];
  return types.some(kind => /file|attachment/i.test(String(kind || '')) && !/page|paragraph|text/i.test(String(kind || '')));
}

async function setComposerFiles(client, cdpSession, files) {
  await client.command('DOM.enable', {}, cdpSession, 10_000);
  const doc = await client.command('DOM.getDocument', { depth: 1 }, cdpSession, 10_000);
  const root = doc.root && doc.root.nodeId;
  if (!root) throw new Error('dom_root_missing');
  const found = await client.command('DOM.querySelectorAll', {
    nodeId: root, selector: 'input[type="file"]',
  }, cdpSession, 10_000);
  const nodes = found.nodeIds || [];
  if (nodes.length !== 1) throw new Error('file_input_count_' + nodes.length);
  await client.command('DOM.setFileInputFiles', { nodeId: nodes[0], files: [] }, cdpSession, 15_000);
  await new Promise(resolve => setTimeout(resolve, 600));
  const doc2 = await client.command('DOM.getDocument', { depth: 1 }, cdpSession, 10_000);
  const root2 = doc2.root && doc2.root.nodeId;
  const found2 = await client.command('DOM.querySelectorAll', {
    nodeId: root2, selector: 'input[type="file"]',
  }, cdpSession, 10_000);
  const nodes2 = found2.nodeIds || [];
  if (nodes2.length !== 1) throw new Error('file_input_count_' + nodes2.length);
  await client.command('DOM.setFileInputFiles', { nodeId: nodes2[0], files }, cdpSession, 15_000);
  await new Promise(resolve => setTimeout(resolve, 500));
}

async function waitForChip(client, cdpSession, names) {
  let last = null;
  for (let n = 0; n < 10; n += 1) {
    await new Promise(resolve => setTimeout(resolve, 800));
    last = await pageEval(client, cdpSession, chipEvaluation(names), 12_000);
    if (last && last.chip) return last;
  }
  return last || { chip: false, names: [] };
}

async function saveShot(client, cdpSession, sessionId) {
  const profile = await pageEval(client, cdpSession, `(() => /Available on Mobile/i.test(String((document.body && document.body.innerText) || '')))()`, 8_000);
  if (profile === true) {
    await client.command('Input.dispatchKeyEvent', {
      type: 'rawKeyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27,
    }, cdpSession);
    await client.command('Input.dispatchKeyEvent', {
      type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27,
    }, cdpSession);
    await new Promise(resolve => setTimeout(resolve, 400));
  }
  const shot = await client.command('Page.captureScreenshot', { format: 'png' }, cdpSession, 20_000);
  const dir = path.join(attachStoreDir('_proof'));
  fs.mkdirSync(dir, { recursive: true });
  const safe = String(sessionId || 'chat').replace(/[^A-Za-z0-9._@-]/g, '_');
  const file = path.join(dir, safe + '-' + Date.now() + '.png');
  fs.writeFileSync(file, Buffer.from(String(shot.data || ''), 'base64'));
  return file;
}

async function openComposerForAttach(client, cdpSession, sessionId, text) {
  const needle = canonicalDraft(text).slice(0, 24);
  if (needle.length < 12) return { ok: false, error: 'attach_needle_short' };
  let retried = false;
  for (let step = 0; step < 6; step += 1) {
    const snap = await pageEval(client, cdpSession, attachLocateEvaluation(sessionId, needle), 20_000);
    if (!snap) return { ok: false, error: 'composer_locate_failed' };
    if (snap.phase === 'composer' && snap.identity && snap.box) return { ok: true };
    if (snap.phase === 'oops') {
      if (retried || !snap.retry) return { ok: false, error: 'composer_page_error' };
      await trustedClick(client, cdpSession, snap.retry);
      retried = true;
      await new Promise(resolve => setTimeout(resolve, 1500));
      continue;
    }
    if (!snap.tabSelected && snap.tab) {
      await trustedClick(client, cdpSession, snap.tab);
      await new Promise(resolve => setTimeout(resolve, 900));
      continue;
    }
    if (snap.tabSelected && snap.row) {
      await trustedClick(client, cdpSession, snap.row);
      await new Promise(resolve => setTimeout(resolve, 1400));
      continue;
    }
    if (snap.tabSelected && snap.rowCount === 0) return { ok: false, error: 'draft_row_missing' };
    if (snap.tabSelected && snap.rowCount > 1) return { ok: false, error: 'draft_row_ambiguous' };
    return { ok: false, error: 'composer_thread_unopened' };
  }
  return { ok: false, error: 'composer_thread_unopened' };
}

async function stagePendingFiles(client, cdpSession, sessionId, text, files) {
  const names = files.map(file => path.basename(file));
  const before = await readHistoryIds(client, cdpSession, sessionId);
  await setComposerFiles(client, cdpSession, files);
  const chip = await waitForChip(client, cdpSession, names);
  let after = await readHistoryIds(client, cdpSession, sessionId);
  if (!sameIds(before, after)) {
    return { chip: false, historyChanged: true, sent: selfChanged(before, after), files: names, serverPersisted: false, bodyExact: false, sendTime: null };
  }
  let proof = await pageEval(client, cdpSession, draftProofEvaluation(sessionId, text, names), 25_000);
  let share = null;
  if (chip && chip.chip && proof && proof.ok === true && Number(proof.sendTime) === 0 && !proofHasFile(proof, names)) {
    try {
      share = await pageEval(client, cdpSession, draftFileEvaluation(sessionId, names), 120_000);
    } catch (error) {
      share = { failed: 'upload_draft', detail: String(error && error.message || error).slice(0, 160) };
    }
    if (share && share.failed && !share.detail) share.detail = share.failed;
    after = await readHistoryIds(client, cdpSession, sessionId);
    if (!sameIds(before, after)) {
      return {
        chip: true, historyChanged: true, sent: selfChanged(before, after), files: names, share,
        serverPersisted: false, bodyExact: proof.bodyExact === true, sendTime: proof.sendTime, draftId: proof.draftId,
      };
    }
    proof = await pageEval(client, cdpSession, draftProofEvaluation(sessionId, text, names), 25_000);
  }
  let restoreError = '';
  if (proof && proof.ok === true && proof.bodyExact !== true && Number(proof.sendTime) === 0) {
    try {
      await pageEval(client, cdpSession, saveEvaluation({
        sessionId, text, draftType: 0, supersede: true,
      }), SAVE_EVAL_MS);
    } catch (error) {
      restoreError = String(error && error.message || error).slice(0, 160);
    }
    after = await readHistoryIds(client, cdpSession, sessionId);
    if (!sameIds(before, after)) {
      return { chip: !!(chip && chip.chip), historyChanged: true, sent: selfChanged(before, after), files: names, share, serverPersisted: false, sendTime: null };
    }
    proof = await pageEval(client, cdpSession, draftProofEvaluation(sessionId, text, names), 25_000);
    const again = await pageEval(client, cdpSession, chipEvaluation(names), 12_000);
    if (!(again && again.chip)) {
      await setComposerFiles(client, cdpSession, files);
      await waitForChip(client, cdpSession, names);
    }
  }
  const screenshot = await saveShot(client, cdpSession, sessionId);
  const finalChip = await pageEval(client, cdpSession, chipEvaluation(names), 12_000);
  const shown = (finalChip && finalChip.chip) || (chip && chip.chip);
  return {
    chip: shown === true,
    files: names,
    inputFiles: (finalChip && finalChip.names) || (chip && chip.names) || [],
    domChip: !!((finalChip && finalChip.domChip) || (chip && chip.domChip)),
    caption: !!((finalChip && finalChip.caption) || (chip && chip.caption)),
    serverPersisted: proofHasFile(proof, names),
    bodyExact: !!(proof && proof.bodyExact),
    bodyLen: proof && proof.bodyLen,
    historyChanged: false,
    sent: false,
    draftId: proof && proof.draftId,
    sendTime: proof && proof.sendTime,
    marks: proof && proof.marks,
    contentTypes: proof && proof.contentTypes,
    share: share && share.ok ? { ok: true, fileIds: share.fileIds } : (share ? { ok: false, error: share.error || share.detail || 'share_failed' } : null),
    restoreError,
    screenshot,
  };
}

function safeAttachName(name, fileId) {
  const base = path.basename(String(name || 'file')).replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 80) || 'file';
  const prefix = String(fileId || '').replace(/[^A-Za-z0-9]/g, '').slice(0, 12);
  return prefix ? prefix + '-' + base : base;
}

async function attachments(args) {
  const sessionId = argValue(args, '--session-id');
  if (!EXACT_SID.test(sessionId)) throw new Error('attachments needs --session-id');
  const sinceRaw = argValue(args, '--since');
  let sinceMs = 0;
  if (sinceRaw) {
    sinceMs = Date.parse(sinceRaw);
    if (!Number.isFinite(sinceMs)) throw new Error('attachments --since is not a time');
  }
  const listed = await withPage(listFilesEvaluation(sessionId, sinceMs), HISTORY_EVAL_MS);
  if (!listed || listed.ok !== true || !Array.isArray(listed.files)) {
    throw new Error('Zoom attachments list failed');
  }
  const dir = attachStoreDir(sessionId);
  fs.mkdirSync(dir, { recursive: true });
  const saved = [];
  for (const file of listed.files.slice(0, 8)) {
    const row = {
      fileId: file.fileId, name: file.name, size: file.size, type: file.type,
      messageId: file.messageId, time: file.time,
    };
    if (file.size && file.size > ATTACH_DOWNLOAD_MAX_BYTES) {
      saved.push({ ...row, skipped: 'too_large' });
      continue;
    }
    const got = await withPage(downloadEvaluation(file), 40_000);
    if (!got || got.ok !== true || !got.b64) {
      saved.push({ ...row, error: got && (got.error || 'download_failed') });
      continue;
    }
    if (got.bytes > ATTACH_DOWNLOAD_MAX_BYTES) {
      saved.push({ ...row, skipped: 'too_large', bytes: got.bytes });
      continue;
    }
    const dest = path.join(dir, safeAttachName(file.name, file.fileId));
    const tmp = dest + '.tmp-' + process.pid;
    fs.writeFileSync(tmp, Buffer.from(got.b64, 'base64'));
    fs.renameSync(tmp, dest);
    saved.push({ ...row, bytes: got.bytes, path: dest });
  }
  process.stdout.write(`${JSON.stringify({
    ok: true,
    action: 'attachments',
    sent: false,
    sessionId,
    count: saved.length,
    files: saved,
  })}\n`);
}


async function save(args) {
  const sessionId = argValue(args, '--session-id');
  const text = readText(args);
  if (!EXACT_SID.test(sessionId)) throw new Error('save needs --session-id');
  if (!String(text || '').trim()) throw new Error('save needs --text or --text-file');
  const draftType = argValue(args, '--draft-type') === '1' ? 1 : 0;
  const replyThreadId = argValue(args, '--reply-thread-id');
  const replyThreadTime = Number(argValue(args, '--reply-thread-time') || 0);
  const request = {
    sessionId,
    text,
    draftType,
    supersede: args.includes('--supersede'),
  };
  if (draftType === 1) {
    request.reply = {
      replyThreadId,
      replyThreadTime,
      replyOwnerJid: argValue(args, '--reply-owner-jid'),
      mainMsgId: argValue(args, '--main-msg-id'),
    };
  }
  const hydrateOff = args.includes('--no-hydrate')
    || String(process.env.ZOOM_HYDRATE_COMPOSER || '') === '0';
  const attaches = attachPaths(args);
  if (attaches.length && (draftType !== 0 || hydrateOff)) throw new Error('attach needs the main composer');
  const payload = await withClient(async (client, cdpSession) => {
    // Settle after attach so nx-chat modules finish booting post tab-recycle.
    if (SAVE_SETTLE_MS > 0) {
      await new Promise(resolve => setTimeout(resolve, SAVE_SETTLE_MS));
    }
    try {
      await pageEval(client, cdpSession, 'document.readyState', 8_000);
    } catch (_) {
      // Soft: save retry path below still attempts createDraft.
    }
    let result;
    let attempts = 0;
    const maxAttempts = 1 + SAVE_EVAL_RETRIES;
    while (attempts < maxAttempts) {
      attempts += 1;
      try {
        result = await pageEval(client, cdpSession, saveEvaluation(request), SAVE_EVAL_MS);
        if (result && result.ok === true) break;
        // Soft SDK errors (existing_draft_conflict etc.) should not retry.
        if (result && result.error && !/timed out|timeout/i.test(String(result.error))) break;
      } catch (error) {
        result = { ok: false, error: String(error && error.message || error).slice(0, 200) };
      }
      const timedOut = /timed out|timeout/i.test(String(result && result.error || ''));
      if (!timedOut || attempts >= maxAttempts) break;
      await new Promise(resolve => setTimeout(resolve, 3_000));
    }
    let composer = { skipped: true };
    let beforeIds = null;
    if (attaches.length) beforeIds = await readHistoryIds(client, cdpSession, sessionId);
    if (draftType === 0 && !hydrateOff && attaches.length) {
      const opened = await openComposerForAttach(client, cdpSession, sessionId, text);
      if (!opened.ok) composer = opened;
      else {
        try {
          composer = await hydrateComposer(client, cdpSession, sessionId, text, { exact: true });
        } catch (error) {
          composer = { ok: false, error: String(error && error.message || error).slice(0, 200) };
        }
      }
    } else if (draftType === 0 && !hydrateOff) {
      const retryable = new Set(['main_composer_ambiguous', 'composer_thread_unopened']);
      for (let attempt = 0; attempt < 3; attempt += 1) {
        if (attempt) await new Promise(resolve => setTimeout(resolve, 2000));
        try {
          composer = await hydrateComposer(client, cdpSession, sessionId, text);
        } catch (error) {
          composer = { ok: false, error: String(error && error.message || error).slice(0, 200) };
        }
        if (composer && composer.ok === true) break;
        if (!retryable.has(String(composer && composer.error || ''))) break;
      }
    }
    // 2026-09-13: Runtime.evaluate timed out on createDraft, zap
    // returned zoom_sdk_save_failed, and the 1:1 composer stayed empty.
    if (composer && composer.ok === true && (!result || result.ok !== true)) {
      result = {
        ok: true,
        already: Boolean(composer.already),
        created: false,
        unread: true,
        receipt: (result && result.receipt) || {},
        postsave: (result && result.postsave) || {},
      };
    }
    if (!result || result.ok !== true) {
      throw new Error(`Zoom save ${result && result.error || 'failed'} failed`);
    }
    let attachment = null;
    if (attaches.length) {
      let midIds = null;
      try { midIds = await readHistoryIds(client, cdpSession, sessionId); }
      catch (error) { attachment = { chip: false, historyChanged: false, error: 'history_ids_unproven' }; }
      if (!attachment && !sameIds(beforeIds, midIds)) {
        attachment = {
          chip: false, historyChanged: true, sent: selfChanged(beforeIds, midIds),
          sendTime: result.receipt && result.receipt.sendTime,
        };
      } else if (!attachment && (!composer || composer.ok !== true)) {
        attachment = { chip: false, historyChanged: false, error: composer && composer.error || 'composer_failed' };
      } else if (!attachment) {
        try {
          attachment = await stagePendingFiles(client, cdpSession, sessionId, text, attaches);
        } catch (error) {
          attachment = { chip: false, historyChanged: false, error: String(error && error.message || error).slice(0, 180) };
        }
      }
    }
    return { result, composer, attachment };
  });
  const attachment = payload.attachment || null;
  const historyChanged = !!(attachment && attachment.historyChanged);
  const sent = !!(attachment && attachment.sent);
  const attachOk = !attaches.length || (
    attachment && attachment.chip === true && !historyChanged && !sent && Number(attachment.sendTime) === 0
  );
  const out = {
    ok: attachOk,
    action: 'save',
    sent,
    already: Boolean(payload.result.already),
    created: Boolean(payload.result.created),
    unread: Boolean(payload.result.unread),
    receipt: payload.result.receipt,
    postsave: payload.result.postsave,
    composer: payload.composer,
  };
  if (attaches.length) out.attachment = attachment;
  process.stdout.write(`${JSON.stringify(out)}\n`);
  if (!attachOk) process.exitCode = 2;
}

async function history(args) {
  const sids = [];
  for (let i = 0; i < args.length; i += 1) {
    if (args[i] === '--session-id' && args[i + 1]) {
      sids.push(args[i + 1]);
      i += 1;
    }
  }
  const result = await withPage(historyEvaluation({ sids }), HISTORY_EVAL_MS);
  if (!result || result.ok !== true || !Array.isArray(result.threads)) {
    throw new Error('Zoom history request returned an invalid result');
  }
  process.stdout.write(`${JSON.stringify({
    ok: true,
    action: 'history',
    threads: result.threads,
  })}\n`);
}

function runUnderLock(kind, arguments_) {
  // DRAFT_RUNNER_READER_TURN_V1 (2026-09-16): under ZOOM_DRAFT_RELAX / ZOOM_HONOR_READER_TURN=0
  // skip the 240s reader-turn wait so writers can turnstile-preempt the read-bridge.
  const lockCall = kind === 'writer'
    ? (
        'from core.zoom_transport_lock import writer_transport_lease as _lock\n'
        + 'with _lock(turnstile_timeout=float(os.environ.get("ZOOM_WRITE_LOCK_TIMEOUT_S", "20")), '
        + 'client_timeout=float(os.environ.get("ZOOM_WRITE_LOCK_TIMEOUT_S", "20")), '
        + 'honor_reader_turn=not (os.environ.get("ZOOM_DRAFT_RELAX", "").strip().lower() in {"1","true","yes"} '
        + 'or os.environ.get("ZOOM_HONOR_READER_TURN", "1").strip().lower() in {"0","false","no"})) as held:'
      )
    : 'from core.zoom_transport_lock import reader_transport_lock\nwith reader_transport_lock(timeout=float(os.environ.get("ZOOM_WRITE_LOCK_TIMEOUT_S", "15"))) as held:';
  const wrapper = [
    'import os, subprocess, sys',
    'sys.path[:0] = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]',
    lockCall,
    '    if not held:',
    '        sys.stderr.write("zoom_write: Zoom transport is busy\\n")',
    '        raise SystemExit(75)',
    '    env = dict(os.environ)',
    '    env["ZOOM_WRITE_LOCK_HELD"] = "1"',
    '    raise SystemExit(subprocess.run(sys.argv[1:], env=env, check=False).returncode)',
  ].join('\n');
  const heavy = arguments_.includes('--attach') || arguments_[0] === 'attachments';
  const timeout = Number(process.env.ZOOM_WRITE_LOCK_TIMEOUT_S || 20) * 1_000
    + SAVE_EVAL_MS + HYDRATE_EVAL_MS + STEP_MS * 4 + (heavy ? 300_000 : 0);
  const result = spawnSync(process.env.PYTHON || 'python3', ['-c', wrapper, process.execPath, __filename, ...arguments_], {
    cwd: process.cwd(),
    encoding: 'utf8',
    timeout,
    env: {
      ...process.env,
      PYTHONPATH: process.env.PYTHONPATH || process.cwd(),
    },
  });
  if (result.stdout) process.stdout.write(result.stdout);
  if (result.stderr) process.stderr.write(result.stderr);
  if (result.error) throw result.error;
  process.exitCode = Number.isInteger(result.status) ? result.status : 2;
}

async function main() {
  const [action, ...extra] = process.argv.slice(2);
  if (!ACTIONS.has(action)) {
    throw new Error('usage: node tools/zoom_write.js drafts|history|save|attachments');
  }
  // ZOOM-COMPAT 2026-10-11: fail closed. If the compat self-test marked the WRITER capability failed for the current Zoom build, refuse to
  // save a draft rather than guess. Gate flag: data/flags/zoom_compat_writer_gate (rm = off). Reads/history/drafts-list are never blocked.
  if (action === 'save') {
    try {
      const home = require('os').homedir() + '/claude-stack';
      if (fs.existsSync(home + '/data/flags/zoom_compat_writer_gate') && fs.existsSync(home + '/data/state/zoom_compat.json')) {
        const st = JSON.parse(fs.readFileSync(home + '/data/state/zoom_compat.json', 'utf8'));
        const b = (st.builds || {})[st.current_build || ''] || {};
        if (b.writer_ok === false) {
          fail('zoom_compat_writer_blocked: build ' + st.current_build + ' failed the writer self-test; drafting disabled until it passes');
          return;
        }
      }
    } catch (_) { /* unreadable compat file never blocks */ }
  }
  if (process.env.ZOOM_WRITE_LOCK_HELD !== '1') {
    runUnderLock((action === 'save' || action === 'delete') ? 'writer' : 'reader', process.argv.slice(2));
    return;
  }
  if (action === 'delete') { await deleteDraftAction(extra); return; }
  if (action === 'drafts') {
    if (extra.length) throw new Error('usage: node tools/zoom_write.js drafts');
    await drafts();
    return;
  }
  if (action === 'history') {
    await history(extra);
    return;
  }
  if (action === 'attachments') {
    await attachments(extra);
    return;
  }
  await save(extra);
}

main().catch(error => fail(error && error.message ? error.message : String(error)));
