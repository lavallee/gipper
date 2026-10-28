"""The reaper ends forgotten gipper machines on every provider, and only those."""
import time

from gipper import ledger, lifecycle
from gipper.reap import reap


class Provider:
    def __init__(self, name, machines):
        self.name, self.machines, self.terminated = name, machines, []

    def has_key(self):
        return True

    def list_ours(self):
        return self.machines

    def terminate(self, mid):
        self.terminated.append(mid)
        self.machines = [m for m in self.machines if str(m["id"]) != str(mid)]


def test_only_forgotten_machines_and_never_confused_across_providers(monkeypatch):
    monkeypatch.setattr(lifecycle.time, "sleep", lambda s: None)
    ledger.write("pod.create", {"pod": "live", "deadline": time.time() + 3600}, job="j1")   # 0.1 receipt: Runpod's
    ledger.write("pod.create", {"pod": "late", "deadline": time.time() - 10}, job="j2")
    ledger.write("pod.create", {"provider": "vast", "pod": "77", "deadline": time.time() + 3600}, job="j3")
    rp = Provider("runpod", [{"id": "live", "name": "gipper-a"}, {"id": "late", "name": "gipper-b"},
                             {"id": "orphan", "name": "gipper-c", "started": 0}])
    va = Provider("vast", [{"id": 77, "name": "gipper-d"}, {"id": "live", "name": "gipper-e", "started": 0},
                           {"id": 78, "name": "gipper-f", "started": time.time()}])   # brand new: still in grace
    acted = reap(providers=[rp, va])
    assert sorted((p, str(i)) for p, i, _, _ in acted) == [("runpod", "late"), ("runpod", "orphan"), ("vast", "live")]
    assert rp.terminated == ["late", "orphan"] and va.terminated == ["live"]
