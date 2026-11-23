"""A hung or locked Cursor CLI must hand the draft on fast, and Zoom's "You" label is the writer."""
import os
import subprocess
import sys
import time

import pytest

from hailports.clip import hops
from hailports.clip.scene import addressee_first, shape_voice


def test_run_group_kills_grandchildren_on_timeout(tmp_path):
    marker = tmp_path / "child.pid"
    # The parent starts a grandchild that holds stdout open, then hangs.
    script = (
        "import subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);open({str(marker)!r},'w').write(str(p.pid));"
        "time.sleep(60)"
    )
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        hops.run_group([sys.executable, "-c", script], timeout=1.5)
    assert time.time() - t0 < 8
    time.sleep(0.3)
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_cursor_timeout_capped():
    assert hops.CURSOR_TIMEOUT_S == 45 and hops.CURSOR_TIMEOUT_MAX_S <= 60


def test_keychain_locked_fails_fast_and_falls_through(monkeypatch, tmp_path):
    fake = tmp_path / "agent"
    fake.write_text("#!/bin/sh\necho 'Error: Your macOS login keychain is locked.' >&2\nexit 1\n")
    fake.chmod(0o755)
    asked = []
    monkeypatch.setattr(hops, "unlock_keychain_prompt", lambda timeout=90: asked.append(1) or False)
    t0 = time.time()
    with pytest.raises(hops.ModelOffline) as err:
        hops._cursor({"bin": str(fake)}, [{"role": "user", "content": "hi"}])
    assert "keychain" in str(err.value)
    assert asked == [1]
    assert time.time() - t0 < 5


def test_zoom_you_label_is_not_the_addressee():
    screen = "Dana Lee 6:52 AM\ncan you check the case?\nYou 6:59 AM\nmorning! just created it"
    assert addressee_first(screen, ["Sam Writer", "Sam"]) == "Dana"
    assert not shape_voice("Dana, -- done.", "", "You").lower().startswith("hey you")
