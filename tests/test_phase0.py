"""Phase 0 tests: no network, no spending."""
import json
import time

import pytest

SECRET = "rpa_TESTSECRET0123456789"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GIPPER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("RUNPOD_API_KEY", SECRET)
    import importlib
    import gipper.config
    import gipper.ledger
    importlib.reload(gipper.config)
    importlib.reload(gipper.ledger)
    yield tmp_path


def test_ledger_never_records_the_key():
    from gipper import ledger
    ledger.write("error", {"error": f"HTTP 401 for Bearer {SECRET}"}, job="j1")
    lines = list(ledger.read())
    assert lines and SECRET not in json.dumps(lines)
    assert "<redacted>" in lines[0]["payload"]["error"]


def test_spend_since_sums_today():
    from gipper import ledger
    ledger.write("spend", {"usd": 0.25, "seconds": 600})
    ledger.write("spend", {"usd": 0.10, "seconds": 300})
    assert abs(ledger.spend_since(ledger.today_start()) - 0.35) < 1e-9


def _job(tmp_path, body):
    p = tmp_path / "job.toml"
    p.write_text(body)
    from gipper.job import load
    return load(p)


def test_job_requires_command(tmp_path):
    from gipper.job import JobError
    with pytest.raises(JobError):
        _job(tmp_path, 'name = "x"\nworkload = "w"\n')


def test_job_rejects_secrets_in_env(tmp_path):
    from gipper.job import JobError
    with pytest.raises(JobError):
        _job(tmp_path, 'name="x"\nworkload="w"\n[run]\ncommand="true"\n[env]\nHF_TOKEN="x"\n')


def test_job_resolves_paths_and_defaults(tmp_path):
    (tmp_path / "in").mkdir()
    j = _job(tmp_path, 'name="x"\nworkload="w"\n[run]\ncommand="true"\n[[inputs]]\nsrc="in"\ndst="in"\n'
                       '[[outputs]]\nsrc="out"\ndst="got"\n')
    assert j.inputs[0].src == str(tmp_path / "in")
    assert j.outputs[0].dst == str(tmp_path / "got")
    assert j.max_usd > 0 and j.sensitivity == "cloud-ok"


GPUS = [
    {"id": "A5000", "memoryInGb": 24, "communityCloud": True, "secureCloud": True, "communityPrice": 0.16, "securePrice": 0.27, "lowestPrice": {"stockStatus": None}},
    {"id": "4090", "memoryInGb": 24, "communityCloud": True, "secureCloud": True, "communityPrice": 0.34, "securePrice": 0.89, "lowestPrice": {"stockStatus": "Low"}},
    {"id": "5090", "memoryInGb": 32, "communityCloud": True, "secureCloud": True, "communityPrice": 0.69, "securePrice": 1.19, "lowestPrice": {"stockStatus": "Low"}},
    {"id": "small", "memoryInGb": 8, "communityCloud": True, "secureCloud": True, "communityPrice": 0.05, "securePrice": 0.1, "lowestPrice": {"stockStatus": "High"}},
]


def test_candidates_fit_stock_and_preference():
    from gipper.runpod.select import candidates
    c = candidates(GPUS, 16)
    assert [g["id"] for g, _, _ in c] == ["4090", "5090", "A5000"]  # stocked first, then price; 8 GB excluded
    c = candidates(GPUS, 16, prefer=["5090"])
    assert c[0][0]["id"] == "5090"


class FakeApi:
    def __init__(self, pods):
        self.pods = pods
        self.terminated = []

    def list_pods(self):
        return self.pods

    def terminate_pod(self, pid):
        self.terminated.append(pid)


def test_reaper_ends_only_forgotten_gipper_pods():
    from gipper import ledger
    from gipper.runpod.reap import reap
    ledger.write("pod.create", {"pod": "live", "deadline": time.time() + 3600}, job="j1")
    ledger.write("pod.create", {"pod": "late", "deadline": time.time() - 10}, job="j2")
    api = FakeApi([
        {"id": "live", "name": "gipper-a"},
        {"id": "late", "name": "gipper-b"},
        {"id": "orphan", "name": "gipper-c", "lastStartedAt": "2020-01-01T00:00:00Z"},
        {"id": "mine", "name": "someone-elses-pod"},
    ])
    acted = reap(api=api)
    assert sorted(p for p, _, _ in acted) == ["late", "orphan"]
    assert "mine" not in api.terminated and "live" not in api.terminated
