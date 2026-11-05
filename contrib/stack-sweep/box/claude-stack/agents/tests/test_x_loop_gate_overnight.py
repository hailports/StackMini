import sys; sys.path.insert(0,'/home/operator/claude-stack')
from core import x_loop_gate as g
def test_reply_rows():
    assert g.is_reply_row({"part": "3"}) and g.is_reply_row({"ref": "manual_reply_uncle_20261103"})
    assert g.is_reply_row({"ref": "manual_stackmini_relaunch_20261104_r1"})
    assert g.is_reply_row({"ref": "manual_stackmini_announce_20261103_reply"})
    assert not g.is_reply_row({"ref": "loop_x_hailports_20261102_124521986755_r"})
    assert not g.is_reply_row({"ref": "manual_arrange_sidequest_20261104", "part": "0"})
    assert not g.is_reply_row({"ref": "article:2118031717567025348"})
if __name__ == "__main__":
    test_reply_rows(); print("PASS test_reply_rows")
