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


def test_estimates_keep_0_1_field_names():
    """Existing client reports read these from estimate receipts and offer.placement_estimate."""
    from gipper.job import Job
    from gipper.market import Offer
    from gipper.placement import Placement, estimate_offer, rank_for_job
    job = Job(name="x", workload="w", command="true", placement=Placement(compute_s=600))
    o = Offer("vast", "RTX 4090", 24, 0.4, stock="yes", metrics={"host_download_mbps": 500, "dlperf": 80})
    e = estimate_offer(job, o)
    for k in ("wall_s", "usd", "rental_usd", "estimated_bandwidth_usd", "estimated_total_usd", "bandwidth_fees_known",
              "cost_is_lower_bound", "setup_s", "setup_download_s", "upload_s", "download_s", "compute_s",
              "host_download_mbps", "host_upload_mbps", "dlperf", "assumptions"):
        assert k in e, k
    assert e["cost_is_lower_bound"] and e["estimated_bandwidth_usd"] is None   # Vast without advertised fees
    ranked = rank_for_job([o], job)
    assert ranked[0].placement_estimate["estimated_total_usd"] == e["estimated_total_usd"]
