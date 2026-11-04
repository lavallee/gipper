"""Bills replace computed spend where they exist, and reconciling twice changes nothing."""
from gipper import ledger
from gipper.billing import reconcile, spend_since


class Billed:
    name = "vast"

    def __init__(self, bills):
        self.bills = bills

    def has_key(self):
        return True

    def billed(self, since):
        return self.bills


def test_reconcile_records_only_our_machines_once():
    ledger.write("pod.create", {"provider": "vast", "pod": "1"}, job="j")
    ledger.write("spend", {"provider": "vast", "pod": "1", "usd": 0.006, "seconds": 47}, job="j")
    p = Billed({"1": (0.031, {"gpu": 0.028, "disk": 0.003}), "someone-else": (5.0, {})})
    assert [w[1] for w in reconcile(providers=[p])] == ["1"]
    assert reconcile(providers=[p]) == []                       # nothing new
    assert spend_since(0) == 0.031                              # the bill, not the 0.006 computed
    p.bills["1"] = (0.035, {})                                  # the day's charges grew
    assert len(reconcile(providers=[p])) == 1 and spend_since(0) == 0.035


def test_a_late_bill_counts_toward_its_machines_day_not_todays():
    from conftest import receipt
    import json
    import time
    from gipper.config import DATA
    old = [receipt("pod.create", {"provider": "vast", "pod": "9"}, ts="2026-10-20T10:00:00+00:00"),
           receipt("spend", {"provider": "vast", "pod": "9", "usd": 1.0}, ts="2026-10-20T11:00:00+00:00")]
    (DATA / "receipts").mkdir(parents=True, exist_ok=True)
    with open(DATA / "receipts" / "2026-10.jsonl", "a") as f:
        f.writelines(json.dumps(r) + "\n" for r in old)
    reconcile(days=30, providers=[Billed({"9": (1.2, {})})])    # recorded now, for a machine from the 20th
    assert spend_since(time.time() - 3600) == 0                 # not today's spend
    assert spend_since(0) == 1.2


def test_a_run_across_midnight_counts_on_the_day_it_ended():
    from conftest import receipt
    import json
    import time
    from gipper.config import DATA
    now = time.time()
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(t))
    recs = [receipt("pod.create", {"provider": "vast", "pod": "n"}, ts=iso(now - 7200)),
            receipt("spend", {"provider": "vast", "pod": "n", "usd": 7.5}, ts=iso(now - 60))]
    (DATA / "receipts").mkdir(parents=True, exist_ok=True)
    with open(DATA / "receipts" / time.strftime("%Y-%m.jsonl", time.gmtime(now)), "a") as f:
        f.writelines(json.dumps(r) + "\n" for r in recs)
    assert spend_since(now - 3600) == 7.5      # created before the window, ended inside it


class Broken(Billed):
    name = "runpod"

    def billed(self, since):
        raise RuntimeError("billing API down")


def test_bills_only_grow_and_one_provider_failing_doesnt_stop_another():
    ledger.write("pod.create", {"provider": "vast", "pod": "1"}, job="j")
    reconcile(providers=[Broken({}), Billed({"1": (0.05, {})})])
    assert spend_since(0) == 0.05
    reconcile(providers=[Billed({"1": (0.02, {})})])           # a clipped window: smaller, ignored
    assert spend_since(0) == 0.05


def test_cost_report_survives_spend_written_without_a_job(capsys):
    """A spend receipt with job=None (a lease run outside `pod run`) crashed `gipper pod cost`
    with a format error and hid every row after it."""
    from gipper import cli, ledger
    ledger.write("pod.create", {"provider": "runpod", "pod": "1"}, job="j")
    ledger.write("spend", {"provider": "runpod", "pod": "1", "usd": 0.5, "seconds": 600}, job="j", refs={"name": "named"})
    ledger.write("spend", {"provider": "runpod", "pod": "2", "usd": 1.25, "seconds": 3600}, refs={"name": "phone-lease"})
    cli.main(["pod", "cost"])
    out = capsys.readouterr().out
    assert "(no job) phone-lease" in out
    assert "total computed $1.750" in out
