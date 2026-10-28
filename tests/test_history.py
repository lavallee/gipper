"""What counts as a bad host, and how the memory weighs what it has seen."""
from conftest import receipt

from gipper.history import History, PRIOR_FAIL


def _rent(pod, *after, prov="vast", machine="m1", job=None):
    job = job or f"j{pod}"
    return [receipt("pod.create", {"provider": prov, "pod": pod, "machine": machine}, job=job),
            *[receipt(k, {"provider": prov, "pod": pod, **p}, job=job) for k, p in after]]


def test_what_counts_against_a_host():
    h = History.load(records=[
        *_rent("1", ("run.start", {}), ("run.end", {"outcome": "failed"})),        # the job failed: not the host
        *_rent("2", ("host.reject", {"why": "GPU busy"})),
        *_rent("3", ("run.start", {}), ("error", {"error": "ssh: Connection reset by peer"})),   # lost mid-run
        receipt("error", {"error": "no instances available"}, job="j4"),            # the market, before any machine
    ])
    outcome = {r.pod: r.outcome for r in h.rentals}
    assert outcome == {"1": "good", "2": "bad", "3": "bad"}


def test_a_stranger_machine_starts_from_its_provider():
    good = [r for i in range(16) for r in _rent(str(i), ("run.start", {}), machine=f"m{i}")]
    h = History.load(records=good + _rent("bad", ("host.reject", {"why": "GPU busy at start"}), machine="lemon"))
    provider = h.failure("vast")
    assert provider < PRIOR_FAIL                                  # 16 of 17 good
    assert h.failure("vast", "never-seen") == provider
    assert h.failure("vast", "lemon") > 3 * provider              # its one bad night sticks to it
    assert h.failure("runpod") == PRIOR_FAIL                      # no record at all


def test_only_real_transfers_measure_speed():
    h = History.load(records=_rent("1", ("host.network_probe", {"upload_mbps": 5}),
                                   ("transfer", {"direction": "up", "bytes": 50e6, "seconds": 2})))
    assert h.mbps("vast", "m1", "up") == 200
    probe_only = History.load(records=_rent("2", ("host.network_probe", {"upload_mbps": 5})))
    assert probe_only.mbps("vast", "m1", "up") is None     # a 2 MiB probe measures ssh start-up


def test_billing_factor_ignores_short_rentals():
    long = _rent("1", ("spend", {"usd": 1.0, "seconds": 3600}), ("spend.billed", {"usd": 1.2}))
    short = _rent("2", ("spend", {"usd": 0.006, "seconds": 47}), ("spend.billed", {"usd": 0.031}))   # rounding: 5x
    assert History.load(records=long + short).billing_factor("vast") == 1.2
    assert History.load(records=short).billing_factor("vast") is None


def test_unsuitable_hosts_and_local_errors_are_not_the_hosts_fault():
    h = History.load(records=[
        *_rent("1", ("host.reject", {"why": "GPU compute capability 7.5 is below the job's 8.0", "kind": "unsuitable"})),
        *_rent("2", ("host.reject", {"why": "Pinned Moss cu124 runtime does not cover this GPU architecture"})),  # pre-0.2
        *_rent("3", ("error", {"error": "upload x failed: tar: Cannot open: Permission denied"})),
        *_rent("4", ("error", {"error": "upload x failed: Connection reset by peer"})),
    ])
    assert {r.pod: r.outcome for r in h.rentals} == {"1": None, "2": None, "3": None, "4": "bad"}


def test_compute_is_learnt_only_from_jobs_that_succeeded():
    from conftest import receipt
    def run(job, outcome, compute):
        return [receipt("pod.create", {"provider": "vast", "pod": job, "gpu": "A40"}, job=job, workload="w"),
                receipt("run.start", {"provider": "vast", "pod": job}, job=job),
                receipt("run.end", {"outcome": outcome, "compute_s": compute}, job=job, workload="w")]
    h = History.load(records=run("a", "capped", 140) + run("b", "failed", 30) + run("c", "succeeded", 900))
    assert h.compute_s("w", "A40") == 900
