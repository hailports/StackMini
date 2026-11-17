#!/usr/bin/env python3
"""Publish dated hp_daily + follower-ledger + article snapshots via the site's
existing guarded deploy.sh. Called by existing live-export-loop every 15 minutes.
No KV credential, no model call, no Mini access, no social posting.
--deploy publishes only changed source data; --local-only rebuilds without publishing.
The old KV writer returned HTTP 401; it is no longer invoked or retried.
"""
import hashlib, json, subprocess, sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from build_live_stats import build, write, SITE
CT=ZoneInfo('America/Chicago')
def main():
    base=build(); now=datetime.now(CT).isoformat(timespec='seconds')
    # Existing hourly profile collector is the source, not an invented fresh timestamp.
    latest=None
    try:
        for line in open(str(Path.home()/'claude-stack/data/hustle/x_followers.jsonl')):
            try: r=json.loads(line)
            except ValueError: continue
            if r.get('lane')=='x_hailports' and isinstance(r.get('followers'),int) and r.get('ts'):
                if latest is None or r['ts']>latest['ts']: latest=r
    except FileNotFoundError: pass
    if latest:
        fol=latest['followers'];at=datetime.fromisoformat(latest['ts']).astimezone(CT).strftime('%b %-d, %Y, %-I:%M %p CT')
        base['ticker'].append({'label':'followers on x ('+at+')','value':f'{fol:,}'})
        base['metrics'].append({'g':'reach','label':'Followers on x','value':f'{fol:,}','sub':'public profile hourly log · '+at,'source':'x_followers.jsonl'})
        base['followers_updated_at']=latest['ts']
    arts=len([f for f in (SITE/'dist/articles').glob('*.html') if f.name!='index.html'])
    base['metrics'].append({'g':'reach','label':'Articles published','value':str(arts),'sub':'site article count · '+base['date'],'source':'/articles/'})
    # Measurement/source-content fingerprint excludes the build clock.
    public_state=SITE/'.public_content_state.json'
    try: public_digest=json.loads(public_state.read_text()).get('sha256','')
    except (OSError,ValueError): public_digest=''
    digest=hashlib.sha256((json.dumps(base,sort_keys=True)+public_digest).encode()).hexdigest()
    base['updated_at']=now;write(base)
    state=SITE/'.last_live_stats_deploy.json'
    try: previous=json.loads(state.read_text())
    except (OSError,ValueError): previous={}
    if '--deploy' in sys.argv and digest!=previous.get('sha256'):
        subprocess.run([str(SITE/'deploy.sh')],check=True,timeout=240)
        state.write_text(json.dumps({'sha256':digest,'as_of':base['as_of'],'deployed_at_ct':now})+'\n')
        print('site snapshot deployed',base['as_of'])
    else: print('snapshot built',base['as_of'],'no deployment requested or sources unchanged')
if __name__=='__main__': main()
