#!/usr/bin/env python3
"""Public, dated snapshots from hp_daily facts. No Mini access.
The only estimates are the labelled would-be cloud spend tiles: measured prompt/generated tokens x public list rates."""
import json, re, html
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
SITE=Path.home()/'hailports/site'; DIST=SITE/'dist'
FACTS=Path.home()/'claude-stack/data/hustle/hp_daily'
# Standard (non-batch, short-context) public list prices, USD per 1M tokens, checked 2026-11-15.
# No cache discount applied: llama-server prompt_tokens_total only counts prompt tokens it actually evaluated.
RATES_CHECKED='Nov 15, 2026'
RATES=[
 ('Claude Sonnet 5.5',2.00,10.00,'Anthropic list price; GPT-6.1 Sol is the same $2/$10','https://docs.anthropic.com/en/docs/about-claude/pricing'),
 ('Claude Opus 5.5',4.00,20.00,'Anthropic list price','https://docs.anthropic.com/en/docs/about-claude/pricing'),
 ('GPT-5.5',5.00,30.00,'OpenAI list price, under 272K context','https://platform.openai.com/docs/pricing'),
 ('Gemini 3.8 Flash',0.75,3.75,'Google list price through Dec 31, 2026; doubles Jan 1','https://ai.google.dev/gemini-api/docs/pricing'),
]

def _fmt_day(day):
    return datetime.strptime(day[:10], '%Y-%m-%d').strftime('%b %-d, %Y')

def _load_pre_facts_days():
    """Dated prompt/generated totals from before the hp_daily /metrics facts files. Never invent a day."""
    for path in (SITE/'tools/local_token_history.json', Path(__file__).resolve().parent/'local_token_history.json', FACTS/'token_history_pre_facts.json'):
        try:
            block=(json.loads(path.read_text()).get('days') or {})
        except (OSError, ValueError, AttributeError):
            continue
        days={}
        for day, row in block.items():
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(day)):
                continue
            if isinstance(row, dict) and 'prompt' in row:
                days[day]={'prompt':int(row['prompt']),'generated':int(row.get('generated', row.get('gen', 0)))}
        if days:
            return days
    return {}

def _facts_token_days():
    """End-of-day (or in-progress today) /metrics deltas from dated hp_daily facts files."""
    out={}
    for path in FACTS.glob('facts_*.json'):
        try:
            payload=json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        day=str(payload.get('day') or '')
        if re.fullmatch(r'\d{8}', day):
            day=f'{day[:4]}-{day[4:6]}-{day[6:]}'
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            m=re.search(r'facts_(\d{8})', path.name)
            if not m:
                continue
            day=f'{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}'
        fact=next((f.get('fact','') for f in (payload.get('facts') or []) if f.get('id')=='local_tokens_today'), '')
        m=re.search(r'\(([\d,]+) prompt, ([\d,]+) generated\)', fact)
        if m:
            out[day]={'prompt':int(m.group(1).replace(',','')),'generated':int(m.group(2).replace(',',''))}
    return out

def list_price_month(at, stamp):
    """30-day list-price equivalent from every dated day of logged local-model generation.
    Same Sonnet 5.5 standard list rates as the Nov 3 write-up and the would-be spend tiles.
    Missing days are omitted, not zero-filled. Today is weighted by the elapsed fraction of the collection day."""
    days=_load_pre_facts_days(); days.update(_facts_token_days())
    if not days:
        return None
    ordered=sorted(days)
    pt=sum(days[d]['prompt'] for d in ordered); gt=sum(days[d]['generated'] for d in ordered)
    n=len(ordered); today=at.date().isoformat()
    n_eff=(n-1+max((at.hour*60+at.minute)/1440, 1/1440)) if today in days else float(n)
    name, ri, ro, note, src = RATES[0]
    usd=pt/1e6*ri+gt/1e6*ro; monthly=usd/n_eff*30
    first, last=ordered[0], ordered[-1]
    from datetime import date, timedelta
    missing=[]
    cursor=date.fromisoformat(first)
    end=date.fromisoformat(last)
    while cursor<=end:
        iso=cursor.isoformat()
        if iso not in days:
            missing.append(iso)
        cursor+=timedelta(days=1)
    bits=[]
    if today in days:
        bits.append('today partial')
    if len(missing)==1:
        bits.append('no log for '+datetime.strptime(missing[0],'%Y-%m-%d').strftime('%b %-d'))
    elif 1<len(missing)<=4:
        bits.append('no log for '+', '.join(datetime.strptime(x,'%Y-%m-%d').strftime('%b %-d') for x in missing))
    elif missing:
        bits.append(f'no log for {len(missing)} days in the span')
    extra=(' ('+'; '.join(bits)+')') if bits else ''
    sub=(f'per 30 days, averaged over all {n} days of logged generation since {_fmt_day(first)}{extra}'
         f' · {name} list price ${ri:g}/${ro:g} per 1M tokens, same basis as the Nov 3, 2026 write-up'
         f' · actually paid $0 · updated {stamp}')
    detail=dict(
        usd=round(monthly,2), window_usd=round(usd,2), prompt_tokens=pt, generated_tokens=gt,
        logged_days=n, day_weight=round(n_eff,3), first=first, last=last, gaps=missing,
        model=name, input_per_mtok=ri, output_per_mtok=ro, rates_checked=RATES_CHECKED,
        formula='(prompt/1e6*input_rate + generated/1e6*output_rate) / day_weight * 30; day_weight is logged days, with today as the elapsed fraction of the collection day; missing days are omitted, not zero-filled',
        source_note='Oct 17–Nov 1 from the timing-log daily rollup used in the Nov 3 cost write-up; later days from hp_daily /metrics deltas, the same source as the would-be spend tiles',
    )
    return detail, sub, '/articles/priced-the-work-on-my-mini'

def build():
    candidates=[]
    for p in FACTS.glob('facts_*.json'):
        j=json.loads(p.read_text())
        if j.get('collected_at_ct') and any(f['id']=='decode_today' for f in j.get('facts',[])): candidates.append((j['collected_at_ct'],p,j))
    _,p,j=max(candidates,key=lambda r:r[0]); at=datetime.fromisoformat(j['collected_at_ct']).astimezone(ZoneInfo('America/Chicago'))
    f={x['id']:x for x in j['facts']}; date=at.strftime('%b %-d, %Y'); stamp=at.strftime('%b %-d, %Y, %-I:%M %p CT')
    metrics=[]
    def add(group,label,value,sub,source): metrics.append(dict(g=group,label=label,value=str(value),sub=sub,source=source))
    for fid,label,group in [('decode_today','Median decode today','speed'),('prefill_today','Prompt prefill','speed'),('draft_accept_today','Draft acceptance','speed'),('local_tokens_today','Local tokens today','work'),('metrics_decode_today','Server-wide decode','speed'),('cache_evictions','Cache evictions','memory')]:
        if fid in f:
            x=f[fid]; val=f"{x['value']:,}" if isinstance(x['value'],int) else f"{x['value']:.1f}"
            add(group,label,val,x['fact']+' · '+stamp,x['src'])
    fast=re.search(r'fastest ([\d.]+) tok/s',f['decode_today']['fact'])
    if fast: add('speed','Fastest answer today',fast[1],'tok/s among today’s answers of 100+ tokens · '+stamp,'llama-server timing log')
    sw=re.search(r'total = ([\d.]+)M used = ([\d.]+)M',' '.join(j.get('term',[])))
    if sw: add('memory','Swap in use',sw[2],f'MB of {sw[1]} MB · '+stamp,'sysctl vm.swapusage')
    if 'wired_limit' in f: add('memory','GPU wired cap',str(f['wired_limit']['value']),f['wired_limit']['fact']+' · '+stamp,f['wired_limit']['src'])
    # 2026-11-15: use realistic daily would-be spend instead of flat figures:
    # today's measured llama-server prompt + generated tokens x public standard list rates.
    spend=[]; tk=re.search(r'\(([\d,]+) prompt, ([\d,]+) generated\)',f.get('local_tokens_today',{}).get('fact',''))
    if tk:
        pt,gt=(int(x.replace(',','')) for x in tk.groups())
        day_frac=max((at.hour*60+at.minute)/1440,1/24)
        for name,ri,ro,note,src in RATES:
            usd=pt/1e6*ri+gt/1e6*ro; spend.append(dict(model=name,input_per_mtok=ri,output_per_mtok=ro,usd=round(usd,2),note=note,rate_source=src))
            pace=f' · ~${usd/day_frac:,.2f} by midnight at this pace' if name==RATES[0][0] and day_frac<0.95 else ''
            add('cost','Would-be spend today, '+name,f'${usd:,.2f}',f'{pt/1e6:.2f}M prompt × ${ri:g} + {gt/1e6:.2f}M generated × ${ro:g} per 1M tokens · {note}{pace} · actually paid $0 · rates checked {RATES_CHECKED} · '+stamp,src)
    # 30-day list-price equivalent from the full dated token history, refreshed with the other live cards.
    month=list_price_month(at, stamp)
    if month:
        detail, sub, cost=month
        add('cost','List-price equivalent',f"${detail['usd']:,.2f}",sub,cost)
    else:
        detail=None
    hours=[r for r in f.get('hourly_decode',{}).get('value',[]) if 0<=r[0]<=at.hour]
    dropped=[r[0] for r in f.get('hourly_decode',{}).get('value',[]) if r[0]>at.hour]
    ladder=[dict(label='experts on cpu, no draft',value=22.2,sub='3,456 answers · Nov 2, 2026 write-up',source='/articles/'),dict(label='all experts on gpu',value=31.3,sub='12,385 answers · Nov 2, 2026 write-up',source='/articles/'),dict(label='daily decode median',value=f['decode_today']['value'],sub=f['decode_today']['fact']+' · '+stamp,source='/stats.json')]
    # A real >=44 hourly measurement, never a rounded-up daily median.
    hit=next((r for r in reversed(hours) if r[1]>=44),None)
    if hit: ladder.append(dict(label='hourly decode median',value=hit[1],sub=f'{date}, {hit[0]}:00 CT · n={hit[2]} answers (current hour may be partial)',source='/stats.json'))
    ticker=[dict(label='median '+date,value=str(f['decode_today']['value'])+' tok/s'),*([dict(label='would-be '+spend[0]['model']+' bill '+date,value='$'+format(spend[0]['usd'],',.2f'))] if spend else []),dict(label='draft kept '+date,value=str(f['draft_accept_today']['value'])+'%'),dict(label='local tokens '+date,value=f"{f['local_tokens_today']['value']:,}"),dict(label='prefill '+date,value=str(f['prefill_today']['value'])+' tok/s')]
    # 2026-11-15: say plainly when today has no collection yet (Mini unreachable), instead of a silent old day.
    stale=at.date()!=datetime.now(ZoneInfo('America/Chicago')).date()
    return dict(stale_day=stale,as_of=j['collected_at_ct'],mini_as_of=j['collected_at_ct'],date=date,stamp=stamp,source_file=p.name,scope='dated hp_daily timing-log and /metrics measurements; would-be spend = measured tokens x public list rates (estimate, nothing billed); list-price equivalent is the full dated local-model token history at Sonnet 5.5 list rates, averaged to 30 days',metrics=metrics,ticker=ticker,hourly_decode=hours,speed_ladder=ladder,hourly_note='Only elapsed CT hours included; current hour is partial. Future-hour rows in the collector are excluded.',excluded_future_hours=dropped,would_be_spend=dict(date=date,as_of=stamp,rates_checked=RATES_CHECKED,formula='prompt_tokens/1e6*input_rate + generated_tokens/1e6*output_rate; standard tier, no cache or batch discount',models=spend),list_price_equivalent=detail)
def esc(x): return html.escape(str(x),quote=True)
def tiles(j):
    return ''.join('<div class="tile"><div class="g">'+esc(m['g'])+'</div><div class="v" data-v="'+esc(m['value'])+'">'+esc(m['value'])+'</div><div class="l">'+esc(m['label'])+'</div><div class="s">'+esc(m['sub'])+'</div></div>' for m in j['metrics'])
def bars(j):
    return ''.join(f'<div class="bar"><div>{esc(r["label"])}<small>{esc(r["sub"])}</small></div><div class="t"><i data-w="{r["value"]/60*100:.1f}%" style="width:{r["value"]/60*100:.1f}%"></i></div><b>{r["value"]:.1f}</b></div>' for r in j['speed_ladder'])
def hours(j):
    return ''.join(f'<div style="height:{v/60*100:.1f}%" title="{esc(j["date"])} {h}:00 CT · {v:.1f} tok/s · n={n}"><span>{h}</span></div>' for h,v,n in j['hourly_decode'])
def write(j):
    for name in ['stats.json','live.json']:
        tmp=DIST/(name+'.tmp');tmp.write_text(json.dumps(j,indent=1)+'\n');tmp.replace(DIST/name)
    # Dated progressive-enhancement fallback, generated by the same pipeline, never hand edited.
    for p in DIST.rglob('*.html'):
        s=p.read_text()
        if 'hp-v2.js' not in s: continue
        tick=''.join('<span class="it">'+esc(x['label'])+' <b>'+esc(x['value'])+'</b></span>' for x in j['ticker'])
        s=re.sub(r'(<div class="trk" data-ticker(?:="")?>).*?(</div>)',lambda m:m[1]+'<span class="tick-group">'+tick+'</span><span class="tick-group" aria-hidden="true">'+tick+'</span>'+m[2],s,flags=re.S)
        if p in [DIST/'index.html',DIST/'live/index.html']:
            asof=j['as_of']
            try:
                age=(datetime.now(ZoneInfo('America/Chicago'))-datetime.fromisoformat(asof)).total_seconds()
            except Exception:
                age=0
            def _board(m, asof=asof, stale=age>48*3600):
                attrs=re.sub(r'\sdata-as-of="[^"]*"','',m.group(1))
                attrs=re.sub(r'\bis-stale\b','',attrs)
                if stale and 'class="' in attrs:
                    attrs=attrs.replace('class="','class="is-stale ',1)
                elif stale:
                    attrs+=' class="is-stale"'
                attrs=attrs.rstrip()+' data-as-of="%s"'%asof
                return '<section'+attrs+'>'
            s=re.sub(r'<section([^>]*id="board"[^>]*)>',_board,s)
            s=re.sub(r'(<div class="grid" data-metrics(?:="")?>).*?(</div><!--/metrics-->)',lambda m:m[1]+tiles(j)+m[2],s,flags=re.S)
            s=re.sub(r'(<span id="asof">).*?(</span>)',lambda m:m[1]+'data as of '+esc(j['stamp'])+(' · no new measurements yet today' if j.get('stale_day') else '')+m[2],s)
            s=re.sub(r'(<div class="bars" data-speed(?:="")?>).*?(</div><!--/speed-->)',lambda m:m[1]+bars(j)+m[2],s,flags=re.S)
            s=re.sub(r'(<div class="hours" data-hours(?:="")?>).*?(</div><!--/hours-->)',lambda m:m[1]+hours(j)+m[2],s,flags=re.S)
            s=re.sub(r'(<div class="kicker" data-hour-title(?:="")?>).*?(</div>)',lambda m:m[1]+'median decode by hour, '+esc(j['date'])+' (CT)'+m[2],s)
        p.write_text(s)
    # Keep the X share card on the same live median the board just wrote.
    from render_og import apply_og
    apply_og(j, DIST)
if __name__=='__main__':
    j=build();write(j); print(j['as_of'],len(j['hourly_decode']),'elapsed hours')
