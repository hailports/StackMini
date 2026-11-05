import sys, types; sys.path.insert(0,'/home/operator/claude-stack/agents'); sys.path.insert(0,'/home/operator/claude-stack')
import x_publisher as xp
xp.time.sleep = lambda s: None
class Tab:
    def __init__(s, seq, clears_on_click=True, clears_on_kbd=True):
        s.seq=list(seq); s.cmds=[]; s.cleared=False; s.coc=clears_on_click; s.cok=clears_on_kbd
    def js(s, code):
        if "scrollIntoView" in code: return s.seq.pop(0) if len(s.seq)>1 else s.seq[0]
        if "tweetTextarea_0" in code: return s.cleared
    def cmd(s, m, **k):
        s.cmds.append((m,k))
        if k.get("type")=="mouseReleased" and s.coc: s.cleared=True
        if m=="Input.dispatchKeyEvent" and k.get("modifiers") and k["type"]=="keyUp" and s.cok: s.cleared=True
def test_waits_for_visible_enabled():
    t=Tab([{"found":True,"enabled":False,"visible":False},{"found":True,"enabled":True,"visible":True,"x":5,"y":5}])
    assert xp.real_click_post(t) and t.cleared
    assert not any(m=="Input.dispatchKeyEvent" for m,_ in t.cmds)
def test_offscreen_falls_back_to_kbd():
    t=Tab([{"found":True,"enabled":True,"visible":False,"x":9999,"y":9999}])
    xp.time.time=iter(range(0,10**6,5)).__next__
    assert xp.real_click_post(t) and t.cleared
    assert any(k.get("modifiers")==2 for m,k in t.cmds if m=="Input.dispatchKeyEvent")
def test_missed_click_falls_back():
    import time as T; xp.time.time=T.time
    t=Tab([{"found":True,"enabled":True,"visible":True,"x":1,"y":1}], clears_on_click=False)
    assert xp.real_click_post(t) and t.cleared
def test_disabled_returns_false():
    xp.time.time=iter(range(0,10**6,5)).__next__
    assert xp.real_click_post(Tab([{"found":True,"enabled":False,"visible":True}])) is False
def test_linebreaks_kept():
    p=xp.load_thread_parts("Line one\nline two\n\npara  \n---\nreply\nx")
    assert p==["Line one\nline two\n\npara","reply\nx"]
def test_reply_row_gets_own_id():
    src=open(xp.__file__).read()
    assert '"id": found["id"]' in src and '"parent_id": parent["id"]' in src
if __name__=="__main__":
    import inspect; n=0
    for k,f in list(globals().items()):
        if k.startswith("test_"): f(); n+=1; print("PASS",k)
    print(n,"passed")
