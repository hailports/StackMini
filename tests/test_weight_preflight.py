"""Boot-time weight preflight: restore, fetch, wait, no crash-loop."""

from __future__ import annotations

from pathlib import Path

from hailports_setup.services import appliance_specs
from hailports_setup.weight_preflight import (
    argv_weight_paths,
    ensure,
    fetch,
    parse_fetch,
    present,
    restore,
    wait_until_ready,
    wrap_model_command,
)


def test_argv_weight_paths_reads_main_projector_and_draft():
    paths = argv_weight_paths([
        "llama-server",
        "-m",
        "models/model.gguf",
        "--mmproj",
        "models/mmproj.gguf",
        "-md",
        "models/draft.gguf",
        "--host",
        "127.0.0.1",
    ])
    assert paths == [
        "models/model.gguf",
        "models/mmproj.gguf",
        "models/draft.gguf",
    ]
    assert argv_weight_paths(["llama-server", "--mmproj=models/mmproj.gguf"]) == ["models/mmproj.gguf"]


def test_restore_copies_same_basename_from_backup(tmp_path):
    dest = tmp_path / "models" / "mmproj.gguf"
    spare = tmp_path / "weight-backup" / "mmproj.gguf"
    spare.parent.mkdir()
    spare.write_bytes(b"projector")
    assert restore(str(dest), str(spare.parent))
    assert dest.read_bytes() == b"projector"


def test_ensure_restores_then_reports_empty_missing(tmp_path):
    dest = tmp_path / "mmproj.gguf"
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "mmproj.gguf").write_bytes(b"ok")
    missing = ensure([str(dest)], str(backup))
    assert missing == []
    assert present(dest)


def test_ensure_lists_missing_when_no_backup(tmp_path):
    dest = tmp_path / "mmproj.gguf"
    missing = ensure([str(dest)], str(tmp_path / "empty"))
    assert missing == [str(dest)]


def test_fetch_writes_file_and_rejects_wrong_size(tmp_path, monkeypatch):
    dest = tmp_path / "mmproj.gguf"
    payload = b"x" * 32

    def retrieve(url, path):
        assert url.startswith("https://example.invalid/")
        Path(path).write_bytes(payload)

    monkeypatch.setattr("hailports_setup.weight_preflight.urllib.request.urlretrieve", retrieve)
    assert fetch(str(dest), "https://example.invalid/mmproj.gguf", expected=32)
    assert dest.read_bytes() == payload
    dest.unlink()
    assert not fetch(str(dest), "https://example.invalid/mmproj.gguf", expected=99)
    assert not dest.exists()


def test_parse_fetch_keeps_url_and_trailing_size():
    name, url, size = parse_fetch(
        "mmproj.gguf=https://huggingface.co/openbmb/example/resolve/main/mmproj-model-f16-4x.gguf:910491648"
    )
    assert name == "mmproj.gguf"
    assert url.endswith("mmproj-model-f16-4x.gguf")
    assert size == 910491648


def test_wait_gives_up_after_max_attempts(tmp_path):
    missing = wait_until_ready(
        [str(tmp_path / "missing.gguf")],
        backup_dir=str(tmp_path / "none"),
        poll_s=0.1,
        max_attempts=1,
    )
    assert missing
    assert missing[0].endswith("missing.gguf")


def test_wrap_adds_preflight_and_projector_fetch():
    cmd = wrap_model_command(
        "python3",
        ["llama-server", "-m", "models/model.gguf", "--mmproj", "models/mmproj.gguf"],
    )
    assert cmd[:4] == ["python3", "-m", "hailports_setup.weight_preflight", "--backup-dir"]
    assert "weight-backup" in cmd
    assert "--fetch" in cmd
    joined = " ".join(cmd)
    assert "mmproj.gguf=" in joined
    assert "mmproj-model-f16-4x.gguf=" in joined
    assert "910491648" in joined
    assert cmd[-5:] == ["llama-server", "-m", "models/model.gguf", "--mmproj", "models/mmproj.gguf"]


def test_appliance_model_service_runs_preflight():
    specs = appliance_specs("com.hailports.local", "local", "state", "python3")
    model = next(item for item in specs if item.kind == "model")
    assert model.command[:3] == ["python3", "-m", "hailports_setup.weight_preflight"]
    assert "--host" in model.command and "127.0.0.1" in model.command
    assert "llama-server" in model.command
