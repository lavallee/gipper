import dataclasses
import json

import pytest

from gipper import ledger
from gipper.http import ProviderError
from gipper.serverless import CleanupPending, Client, Spec, SubmissionUnknown
from gipper.serverless.runpod import Adapter, body


def spec(**kw):
    return Spec(**{"name": "gipper-test", "image": "example/image@sha256:abc", "rate_usd_h": 1.34,
                   "options": {"pools": ["ADA_24"]}, **kw})


class Fake:
    def __init__(self, result=None):
        self.resource = None
        self.result = result or {"id": "j1", "status": "COMPLETED", "delayTime": 10, "output": {"answer": 256}}
        self.submits = self.deletes = self.cancels = 0
        self.stuck_delete = False

    def create(self, s):
        self.resource = {"id": "ep1", **body(s)}
        return self.resource

    def get(self, endpoint):
        return self.resource

    def delete(self, endpoint):
        self.deletes += 1
        if not self.stuck_delete:
            self.resource = None

    verify = staticmethod(Adapter.verify)

    def submit(self, endpoint, payload, execution_ms):
        self.submits += 1
        return {"id": "j1"}

    def status(self, endpoint, job):
        return self.result

    def cancel(self, endpoint, job):
        self.cancels += 1
        self.result = {"status": "CANCELLED"}


def test_run_and_private_receipts():
    fake = Fake()
    result = Client(backend=fake).run(spec(), {"private_video": "not in receipts"})
    assert result["output"]["answer"] == 256
    assert fake.submits == fake.deletes == 1
    receipts = list(ledger.read())
    assert receipts[-1]["payload"]["verified_absent"]
    assert "not in receipts" not in json.dumps(receipts)
    assert "serverless.submit_intent" in {r["kind"] for r in receipts}


@pytest.mark.parametrize("kw", [dict(rate_usd_h=0), dict(rate_usd_h=float('nan')), dict(max_workers=0),
    dict(idle_seconds=3601), dict(max_usd=0.01), dict(name="someone-else"), dict(provider="vast"),
    dict(options={"pools": ["ADA_24"], "global_volume": "g", "network_volumes": ["n"]}),
    dict(options={"pools": ["ADA_24"], "typo": True}), dict(options={"pools": "ADA_24"})])
def test_admission(kw):
    with pytest.raises(ValueError):
        spec(**kw)


def test_storage_and_scale_zero():
    b = body(spec(options={"pools": ["ADA_24"], "global_volume": "g"}))
    assert b["mounts"] == {"global": [{"volumeId": "g"}]}
    assert b["workers"] == {"min": 0, "max": 1, "idleTimeout": 15}
    assert b["timeout"] > 0
    assert b["gpu"]["excludedTypes"] == []


def test_ambiguous_submission_is_never_retried():
    fake = Fake()
    def submit(*a):
        fake.submits += 1
        raise ProviderError("connection lost")
    fake.submit = submit
    with pytest.raises(SubmissionUnknown):
        Client(backend=fake).run(spec(), {})
    assert fake.submits == fake.deletes == 1
    assert any(r["kind"] == "serverless.submit_unknown" for r in ledger.read())


def test_timeout_confirms_cancel_then_deletes():
    fake = Fake({"status": "IN_PROGRESS"})
    client = Client(backend=fake)
    ep = client.create(spec())
    job = client.submit(ep, {})
    with pytest.raises(TimeoutError):
        client.wait(ep, job, timeout=0.001, poll_seconds=0.001)
    assert fake.cancels == 1
    assert list(ledger.read())[-1]["payload"]["terminal_status"] == "CANCELLED"
    client.delete(ep)


def test_delete_requires_absence_and_ownership():
    fake = Fake()
    client = Client(backend=fake)
    with pytest.raises(ValueError):
        client.delete("foreign")
    ep = client.create(spec())
    fake.stuck_delete = True
    with pytest.raises(CleanupPending):
        client.delete(ep)
    assert not list(ledger.read())[-1]["payload"]["verified_absent"]


def test_failed_readback_cleans_up():
    fake = Fake()
    fake.verify = lambda *a: (_ for _ in ()).throw(RuntimeError("mismatch"))
    with pytest.raises(RuntimeError, match="mismatch"):
        Client(backend=fake).create(spec())
    assert fake.deletes == 1


def test_post_transport_is_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr("gipper.serverless.runpod.request", lambda *a, **kw: calls.append((a, kw)) or {"id": "j"})
    Adapter().submit("ep", {}, 1000)
    assert calls[0][1]["retries"] == 1
    assert calls[0][0][1] == "https://api.runpod.ai/v2/ep/run"
    assert calls[0][1]["headers"]["Authorization"].startswith("Bearer ")


def test_gpu_readback_does_not_silently_widen():
    s = spec(options={"pools": ["ADA_24"], "excluded_types": ["other"]})
    b = body(s)
    b["gpu"]["excludedTypes"] = []
    with pytest.raises(RuntimeError, match="excludedTypes"):
        Adapter.verify(b, s)


def test_spec_roundtrip_and_unknown_fields(tmp_path):
    from gipper.serverless import load
    p = tmp_path / "spec.toml"
    p.write_text('[endpoint]\nname="gipper-test"\nimage="image"\n[budget]\nrate_usd_h=1.34\n[runpod]\npools=["ADA_24"]\n')
    assert dataclasses.asdict(load(p))["provider"] == "runpod"
    with p.open("a") as f:
        f.write('[typo]\nvalue=1\n')
    with pytest.raises(ValueError, match="unknown"):
        load(p)


def test_real_api_extra_worker_field_and_omitted_empty_exclusions():
    s = spec()
    resource = {"id": "ep", **body(s)}
    resource["workers"]["initTimeout"] = None
    resource["gpu"].pop("excludedTypes")
    Adapter.verify(resource, s)
    resource["workers"]["min"] = 1
    with pytest.raises(RuntimeError, match="workers.min"):
        Adapter.verify(resource, s)


def test_reaper_preserves_deliberately_kept_endpoint(monkeypatch):
    fake = Fake()
    fake.list = lambda: [fake.resource] if fake.resource else []
    client = Client(backend=fake)
    client.create(spec())
    monkeypatch.setattr("gipper.serverless.time.time", lambda: 9e12)
    assert client.reap() == []
    client.delete("ep1")
    client.create(spec(), temporary=True)
    monkeypatch.setattr("gipper.serverless.time.time", lambda: 9e12 + 1000)
    assert client.reap(dry_run=True) == ["ep1"]
    assert fake.resource is not None
    assert client.reap() == ["ep1"]
    assert fake.resource is None


def test_unconfirmed_closure_blocks_another_creation():
    fake = Fake()
    client = Client(backend=fake)
    ep = client.create(spec())
    fake.stuck_delete = True
    with pytest.raises(CleanupPending):
        client.delete(ep)
    with pytest.raises(CleanupPending, match="previous"):
        client.create(spec(name="gipper-replacement"))
    fake.stuck_delete = False
    client.delete(ep)
    client.create(spec(name="gipper-replacement"))


def test_cancel_network_failure_blocks_replacement_until_deleted():
    fake = Fake()
    client = Client(backend=fake)
    ep = client.create(spec())
    job = client.submit(ep, {})
    fake.cancel = lambda *a: (_ for _ in ()).throw(ProviderError("network"))
    with pytest.raises(CleanupPending, match="request failed"):
        client.cancel(ep, job)
    with pytest.raises(CleanupPending, match="previous"):
        client.create(spec(name="gipper-replacement"))
    client.delete(ep)
    client.create(spec(name="gipper-replacement"))


@pytest.mark.parametrize("status,absent", [(404, True), (500, False), (403, False)])
def test_only_actual_404_proves_absence(monkeypatch, status, absent):
    def failure(*a, **kw):
        raise ProviderError("HTTP 500 body mentions HTTP 404:", status=status)
    monkeypatch.setattr("gipper.serverless.runpod.request", failure)
    if absent:
        assert Adapter().get("ep") is None
    else:
        with pytest.raises(ProviderError):
            Adapter().get("ep")
