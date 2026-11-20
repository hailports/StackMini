import errno
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "materialize", Path(__file__).resolve().parents[1] / "contrib/synced-drive/materialize.py"
)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class FakeLib:
    def __init__(self, rc=0, boom=False):
        self.rc, self.boom, self.calls = rc, boom, []

    def setiopolicy_np(self, *args):
        if self.boom:
            raise OSError("nope")
        self.calls.append(args)
        return self.rc


def test_noop_off_macos():
    lib = FakeLib()
    assert m.allow_materialize(platform="linux", lib=lib) is False
    assert lib.calls == []


def test_sets_process_policy_on_macos():
    lib = FakeLib()
    assert m.allow_materialize(platform="darwin", lib=lib) is True
    assert lib.calls == [(3, 0, 2)]


def test_failure_returns_false_and_never_raises():
    assert m.allow_materialize(platform="darwin", lib=FakeLib(rc=-1)) is False
    assert m.allow_materialize(platform="darwin", lib=FakeLib(boom=True)) is False


def test_placeholder_error_detection():
    assert m.is_placeholder_error(OSError(errno.EDEADLK, "Resource deadlock avoided"))
    assert not m.is_placeholder_error(OSError(errno.ENOENT, "missing"))
    assert not m.is_placeholder_error(ValueError("x"))
