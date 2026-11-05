import sys; sys.path.insert(0, "/home/operator/claude-stack")
from core import clarity_gate as cg
seen = []
def fake_score(text, lane="x_hailports", context=None):
    seen.append(context); return (8, "ok") if context or "Side quest" in text else (3, "no subject")
cg.score = fake_score
ok, why = cg.check_thread("x_hailports", ["Side quest: Arrange app", "It drives System Settings", "Link"])
assert ok, why
assert seen == [None, ["Side quest: Arrange app"], ["Side quest: Arrange app", "It drives System Settings"]], seen
ok, why = cg.check_thread("x_hailports", ["no subject here", "reply"])
assert not ok and why.startswith("part0"), why
print("PASS")
