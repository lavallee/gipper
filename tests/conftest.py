"""Every test runs against an empty ledger in a temp dir and fake keys: no network,
no spending, never the real .env."""
import importlib

import pytest

SECRET = "rpa_TESTSECRET0123456789"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GIPPER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("GIPPER_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("GIPPER_ENV", str(tmp_path / "no.env"))
    monkeypatch.setenv("RUNPOD_API_KEY", SECRET)
    for k in ("VAST_API_KEY", "SHADEFORM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    import gipper.config
    import gipper.ledger
    importlib.reload(gipper.config)
    importlib.reload(gipper.ledger)
    yield tmp_path


def receipt(kind, payload, job="j1", ts="2026-10-27T12:00:00+00:00", workload=None):
    """A ledger record as gipper writes it, for building histories."""
    return {"schema": "gipper.receipt/v1", "id": "r", "ts": ts, "kind": kind, "job": job,
            "refs": {"workload": workload} if workload else {}, "payload": payload}
