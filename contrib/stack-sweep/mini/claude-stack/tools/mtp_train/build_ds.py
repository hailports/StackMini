"""Rebuild operator-text dataset from the learning store (HARD DATA LINE filtered). Stable heldout split by hash."""
import json,hashlib,re,os,time
L=os.path.expanduser('~/claude-stack/data/learning'); O=os.path.expanduser('~/claude-stack/data/mtp_train')
BAD=re.compile(r"\b(legal|lawyer|attorney|counsel|lawsuit|litigation|accountant|cpa|bank|banking|wire transfer|routing|iban|swift|invoice|payroll|tax|irs|finance|financial|password|passcode|2fa|mfa code|one[- ]time code|otp|verification code|seed phrase|recovery phrase|ssn|social security)\b",re.I)

def _refuse_heldout(path):
    """Preference-pair held-out file is not a training input."""
    if os.path.basename(os.fspath(path)) == "pref_pairs_heldout.jsonl":
        raise SystemExit("refusing pref_pairs_heldout.jsonl")

def _train_open(path, mode="r", *args, **kwargs):
    _refuse_heldout(path)
    return open(path, mode, *args, **kwargs)

def main():
    texts=[];drafts=[]
    for l in _train_open(f'{L}/draft_edits.jsonl'):
        try:d=json.loads(l)
        except Exception:continue
        t=d.get('final') or (d.get('after') if d.get('kind') in('preferred_rewrite_guidance','operator_preferred_rewrite') else None)
        if isinstance(t,str):texts.append((t,d.get('ts','')))
        if isinstance(d.get('draft'),str):drafts.append((d['draft'],d.get('ts','')))
    for l in _train_open(f'{L}/qwen_lora_dataset_prep.jsonl'):
        try:d=json.loads(l)
        except Exception:continue
        for m in d.get('messages',[]):
            if m.get('role')=='assistant':texts.append((m['content'],''))
    def clean(pairs,kind):
        seen=set();out=[];drop=0
        for t,ts in pairs:
            t=t.strip()
            if len(t.split())<8:continue
            h=hashlib.sha1(t.encode()).hexdigest()[:16]
            if h in seen:continue
            seen.add(h)
            if BAD.search(t):drop+=1;continue
            out.append({'id':h,'text':t,'ts':ts,'kind':kind})
        return out,drop
    out,drop=clean(texts,'real'); dr,drop2=clean(drafts,'draft')
    FZ=f'{O}/heldout_frozen.json'
    if os.path.exists(FZ): frozen=set(json.load(_train_open(FZ)))
    else:
        frozen={r['id'] for r in out+dr if int(r['id'],16)%8==0}; json.dump(sorted(frozen),_train_open(FZ,'w'))
    today=time.strftime('%Y-%m-%d'); yday=time.strftime('%Y-%m-%d',time.localtime(time.time()-86400))
    prev=set(json.load(_train_open(f'{O}/seen_ids.json'))) if os.path.exists(f'{O}/seen_ids.json') else None
    allr=out+dr; new=[r['id'] for r in allr if prev is not None and r['id'] not in prev]
    # day's new items: go to 'today' eval split (not trained tonight), trained from tomorrow on
    for r in allr: r['split']='heldout' if r['id'] in frozen else ('today' if r['id'] in new else 'train')
    for n in('train','heldout','today'):
        with _train_open(f'{O}/{n}.jsonl','w') as f:
            for r in allr:
                if r['split']==n: f.write(json.dumps(r)+'\n')
    json.dump(sorted({r['id'] for r in allr}),_train_open(f'{O}/seen_ids.json','w'))
    st={'n_real':len(out),'n_draft':len(dr),'dropped_hard_line':drop+drop2,'train':sum(r['split']=='train' for r in allr),'heldout':sum(r['split']=='heldout' for r in allr),'today':len(new),
        'words':sum(len(r['text'].split()) for r in allr),'data_hash':hashlib.sha1(json.dumps(sorted(r['id'] for r in allr)).encode()).hexdigest()[:12]}
    json.dump(st,_train_open(f'{O}/stats.json','w')); print(json.dumps(st))

if __name__ == "__main__":
    main()
