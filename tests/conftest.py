import os

import pytest

os.environ.setdefault("HAILPORTS_NO_KEYCHAIN", "1")
os.environ.setdefault("HAILPORTS_PROMPT", "plain")


@pytest.fixture(autouse=True)
def isolated_route(tmp_path, monkeypatch):
    monkeypatch.setenv("PAISTE_ROUTE", str(tmp_path / "route.yaml"))
