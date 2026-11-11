"""Status file builder. No model, no live process table."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "contrib/stack-sweep/box/claude-stack/bin/grokjobs-now.py"


def load():
    spec = importlib.util.spec_from_file_location("grokjobs_now", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_redact_and_render(tmp_path):
    mod = load()
    job = tmp_path / "alpha"
    job.mkdir()
    (job / "REPORT.md").write_text("done\n", encoding="utf-8")
    secret = "sk-" + "abc123456789"
    mail = "a" + "@" + "b.co"
    addr = "10" + ".1.2.3"
    (job / "run.log").write_text(f"user {mail} {addr} {secret}\n", encoding="utf-8")
    (tmp_path / "beta.log").write_text("last beta\n", encoding="utf-8")
    jobs = mod.discover(tmp_path)
    assert set(jobs) == {"alpha", "beta"}
    text = mod.tail_line(job / "run.log")
    assert "[email]" in text and "[ip]" in text and "[secret]" in text
    assert mail not in text and addr not in text and "sk-" not in text
    mod.JOBS = tmp_path
    proc = {
        "cwd": str(job),
        "prompt_file": "",
        "prompt_chars": 0,
        "parent": "bash",
        "etime": "1m",
        "age_s": 60,
        "pid": "1",
    }
    rendered = mod.render(jobs, [proc], 1_794_350_000)
    assert "alpha | running" in rendered
    assert "beta | idle" in rendered or "beta |" in rendered
    assert mail not in rendered and addr not in rendered
