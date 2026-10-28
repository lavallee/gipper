"""Promises to clients: keys never leak, and pre-0.2 import paths keep working
(or fail loudly where silently working would mislead)."""
import json

import pytest
from conftest import SECRET


def test_keys_never_reach_the_ledger(monkeypatch):
    monkeypatch.setenv("VAST_API_KEY", "vast_SECRET_abc")
    from gipper import ledger
    ledger.write("error", {"error": f"HTTP 401 for Bearer {SECRET} and vast_SECRET_abc"}, job="j1")
    text = json.dumps(list(ledger.read()))
    assert SECRET not in text and "vast_SECRET_abc" not in text and text.count("<redacted>") == 2


def test_client_surface_still_there():
    from gipper import run
    for name in ("run", "plan", "estimate", "preflight", "settings", "BadHost", "Unsuitable"):
        assert hasattr(run, name), name
    from gipper.runpod.api import Api, RunpodError  # noqa: F401
    from gipper.runpod.run import _wait_ready  # noqa: F401
    import gipper.runpod.run as old
    assert Api()._key == SECRET and hasattr(Api(), "_gql")
    with pytest.raises(AttributeError, match="plan"):
        old.pick_gpu   # scripts that patch the old selection must fail, not silently patch nothing
