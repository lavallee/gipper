"""The whole-run score: short jobs go to reliable hosts, long ones to cheap ones,
and each figure comes from the best source there is."""
from gipper.costs import estimate
from gipper.history import History
from gipper.job import Job
from gipper.market import Offer, rank
from gipper.placement import Placement


def _job(compute_s, **kw):
    return Job(name="x", workload="w", command="true", max_hours=24, placement=Placement(compute_s=compute_s, **kw))


def _history(bad_vast):
    """A ledger where Runpod hosts always worked and `bad_vast` of 10 Vast hosts didn't."""
    from conftest import receipt
    recs = []
    for i in range(10):
        for prov, bad in (("runpod", False), ("vast", i < bad_vast)):
            job = f"{prov}{i}"
            recs.append(receipt("pod.create", {"provider": prov, "pod": job}, job=job))
            recs.append(receipt("host.reject" if bad else "run.start", {"provider": prov, "pod": job, "why": "busy"}, job=job))
    return History.load(records=recs)


CHEAP_FLAKY = Offer("vast", "RTX 4090", 24, 0.40, stock="yes")
STEADY = Offer("runpod", "NVIDIA GeForce RTX 4090", 24, 0.69, stock="Low")


def test_short_jobs_pay_for_reliability_long_jobs_for_price():
    h = _history(bad_vast=3)
    short = rank([CHEAP_FLAKY, STEADY], job=_job(15 * 60), history=h)
    long = rank([CHEAP_FLAKY, STEADY], job=_job(10 * 3600), history=h)
    assert short[0].provider == "runpod"     # 29 cents of rent apart is less than a 30% chance of a bad host
    assert long[0].provider == "vast"        # three dollars apart is more


def test_bandwidth_fees_and_transfer_time_count():
    h = History.load(records=[])
    fees = Offer("vast", "RTX 4090", 24, 0.40, stock="yes",
                 metrics={"host_download_mbps": 800, "host_upload_mbps": 800,
                          "host_download_usd_per_gb": 0.02, "host_upload_usd_per_gb": 0.02})
    slow = Offer("vast", "RTX 4090", 24, 0.40, stock="yes", metrics={"host_download_mbps": 100, "host_upload_mbps": 100})
    job = _job(600, setup_download_gb=10, download_gb=1)
    e_fees, e_slow = estimate(job, fees, h), estimate(job, slow, h)
    assert e_fees["bandwidth_usd"] == round(11 * 0.02, 4)
    assert e_slow["phases_s"]["fetch"] > 5 * e_fees["phases_s"]["fetch"]
    assert e_slow["bandwidth_known"] is False      # Vast without advertised fees: flagged, not assumed free


def test_compute_comes_from_the_job_then_memory_then_a_default():
    from conftest import receipt
    h = History.load(records=[
        receipt("pod.create", {"provider": "vast", "pod": "1", "gpu": "RTX 4090"}, workload="w"),
        receipt("run.start", {"provider": "vast", "pod": "1"}),
        receipt("run.end", {"outcome": "succeeded", "compute_s": 333}, workload="w")])
    o = Offer("vast", "RTX 4090", 24, 0.4, stock="yes")
    assert estimate(_job(None, compute_s_by_gpu={"4090": 111}), o, h)["phases_s"]["compute"] == 111
    assert estimate(_job(222), o, h)["phases_s"]["compute"] == 222
    e = estimate(_job(None), o, h)
    assert e["phases_s"]["compute"] == 333 and e["basis"]["compute"] == "memory"
    assert estimate(_job(None), Offer("vast", "A40", 48, 0.3, stock="yes"), h)["basis"]["compute"] == "default"


def test_measured_speed_beats_advertised():
    from conftest import receipt
    h = History.load(records=[
        receipt("pod.create", {"provider": "vast", "pod": "1", "machine": "9"}),
        receipt("transfer", {"provider": "vast", "pod": "1", "direction": "up", "bytes": 100e6, "seconds": 40})])
    o = Offer("vast", "RTX 4090", 24, 0.4, ref={"machine": "9"}, metrics={"host_download_mbps": 1000})
    e = estimate(_job(60, upload_gb=1.0), o, h)
    assert e["basis"]["upload"] == "measured" and e["phases_s"]["upload"] == 400   # 1 GB at the measured 20 Mb/s
