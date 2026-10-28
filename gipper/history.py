"""What gipper has learned from its own rentals, read from the ledger (nothing else
is stored): how often a provider's or a machine's hosts turn out bad, how long they
take to be ready, how fast transfers really go from here, how long a workload's
compute takes per GPU, and how a provider's bill compares with the rate x lifetime
gipper computes.

A rental (one machine) is *good* once its job started on it, and *bad* if the
host was broken: a busy GPU, CUDA that won't initialise, ssh that never came up,
a failed probe, or a connection lost before or during the job. A host rejected
because it doesn't suit this job (`run.Unsuitable`: compute capability, a
client's runtime check) is neither. Nor are errors on this side (a local file
that won't upload), errors before any machine existed (no stock), or a job that
fails on a working host.

Compute time is learnt only from jobs that succeeded; transfer speed only from
real transfers of at least MIN_TRANSFER_BYTES. Small probes mostly measure ssh
start-up, so they're never used for speed.

Rates are smoothed so one bad night doesn't condemn a provider and one good run
doesn't make a stranger's machine trusted: a machine starts from its provider's
rate, a provider from PRIOR_FAIL.
"""
import re
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from gipper import ledger
from gipper.config import settings

PRIOR_FAIL = 0.10      # a provider gipper has never used
PROVIDER_WEIGHT = 4    # rentals' worth of belief in the prior
MACHINE_WEIGHT = 2     # rentals' worth of belief in the provider's rate, for one machine
MIN_TRANSFER_BYTES = 20e6   # smaller transfers say more about ssh start-up than bandwidth
MIN_BILLED_S = 600          # shorter rentals mostly measure a provider's rounding, not its rate
LOST = re.compile(r"ssh|connection|timed out|timeout|broken pipe|reset by peer|no route|unreachable", re.I)
# host.reject reasons written before rejects carried a "kind": the host's fault or not
BROKEN = re.compile(r"busy|nvidia-smi|cuinit|cuda initiali|device enumeration|libcuda|ssh|never came up|no ssh endpoint|"
                    r"not active|is (exited|unknown|offline)|probe|below configured minimum|timeout|connection", re.I)


@dataclass
class Rental:
    provider: str
    pod: str
    job: str | None
    ts: float
    machine: str | None = None
    gpu: str | None = None
    ready_s: float | None = None
    started: float | None = None        # run.start time
    ended: float | None = None          # job exit (or collect, for older receipts)
    compute_s: float | None = None      # only from a job that succeeded
    succeeded: bool = False
    outcome: str | None = None          # good | bad
    why: str | None = None
    up_mbps: list = field(default_factory=list)       # real transfers (inputs up, outputs down)
    down_mbps: list = field(default_factory=list)
    computed_usd: float = 0.0
    computed_s: float = 0.0
    billed_usd: float | None = None
    cleanup_pending: bool = False


def _ts(r):
    return datetime.fromisoformat(r["ts"]).timestamp()


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


class History:
    def __init__(self, rentals, workloads):
        self.rentals = rentals            # [Rental]
        self.workloads = workloads        # job id -> workload
        self._by = defaultdict(list)
        for r in rentals:
            self._by[r.provider].append(r)
            if r.machine:
                self._by[(r.provider, r.machine)].append(r)

    @classmethod
    def load(cls, days=None, records=None):
        days = settings()["history_days"] if days is None else days
        recs = records if records is not None else ledger.read(since=time.time() - days * 86400)
        rentals, workloads, current, billed = {}, {}, {}, {}
        for r in recs:
            p, kind, job = r["payload"], r["kind"], r.get("job")
            if (r.get("refs") or {}).get("workload") and job:
                workloads[job] = r["refs"]["workload"]
            key = (p.get("provider") or "runpod", str(p.get("pod"))) if p.get("pod") is not None else None
            if kind == "pod.create" and key:
                rentals[key] = Rental(key[0], key[1], job, _ts(r), machine=_s(p.get("machine")), gpu=p.get("gpu"))
                current[job] = key
                continue
            if kind == "error" and job in current:
                rent, err = rentals.get(current[job]), p.get("error", "")
                if rent and not rent.ended and LOST.search(err) and (rent.outcome is None or rent.started):
                    rent.outcome, rent.why = "bad", ("lost: " if rent.started else "") + err[:200]
                continue
            if kind == "run.end" and job in current:
                rent = rentals.get(current[job])
                if rent and rent.started:
                    rent.ended = rent.ended or _ts(r)
                    rent.succeeded = p.get("outcome") == "succeeded"
                    rent.compute_s = p.get("compute_s") or rent.compute_s
                continue
            if kind == "spend.billed" and key:
                billed[key] = p.get("usd", 0.0)   # the latest bill for the machine counts
            rent = rentals.get(key) if key else None
            if rent is None:
                continue
            if kind == "pod.ready":
                rent.ready_s = p.get("after_s")
                rent.machine = rent.machine or _s(p.get("machine"))
                rent.gpu = p.get("gpu") or rent.gpu
            elif kind == "run.start":
                rent.started, rent.outcome = _ts(r), "good"
            elif kind == "host.reject":
                why = p.get("why", "")
                broken = p.get("kind") == "broken" or (p.get("kind") is None and BROKEN.search(why))
                rent.outcome, rent.why = ("bad" if broken else None), why[:200]
                rent.machine = rent.machine or _s(p.get("machine"))
            elif kind == "transfer" and p.get("seconds") and p.get("bytes", 0) >= MIN_TRANSFER_BYTES:
                mbps = p["bytes"] * 8 / 1e6 / p["seconds"]
                (rent.up_mbps if p.get("direction") == "up" else rent.down_mbps).append(mbps)
            elif kind == "collect" and rent.started and not rent.ended:
                rent.ended = _ts(r)    # older receipts: compute ~ run.start .. collect
            elif kind == "spend":
                rent.computed_usd += p.get("usd", 0.0)
                rent.computed_s += p.get("seconds", 0)
            elif kind == "pod.termination_pending":
                rent.cleanup_pending = True
            elif kind == "pod.closure_verified":
                rent.cleanup_pending = False
        for key, usd in billed.items():
            if key in rentals:
                rentals[key].billed_usd = usd
        for rent in rentals.values():
            if not rent.succeeded:
                rent.compute_s = None   # a capped, failed or interrupted run says nothing about how long it takes
            elif rent.compute_s is None and rent.started and rent.ended:
                rent.compute_s = rent.ended - rent.started
        return cls(list(rentals.values()), workloads)

    # ---------------------------------------------------------------- what it knows
    def rentals_of(self, provider, machine=None):
        return self._by[(provider, str(machine))] if machine is not None else self._by[provider]

    def failure(self, provider, machine=None):
        """Chance a fresh rental turns out bad: the machine's record if it has one,
        pulled towards the provider's, pulled towards PRIOR_FAIL."""
        rs = [r for r in self.rentals_of(provider) if r.outcome]
        bad = sum(r.outcome == "bad" for r in rs)
        p = (bad + PRIOR_FAIL * PROVIDER_WEIGHT) / (len(rs) + PROVIDER_WEIGHT)
        if machine is None:
            return p
        ms = [r for r in self.rentals_of(provider, machine) if r.outcome]
        return (sum(r.outcome == "bad" for r in ms) + p * MACHINE_WEIGHT) / (len(ms) + MACHINE_WEIGHT)

    def ready_s(self, provider, machine=None):
        return (_median([r.ready_s for r in self.rentals_of(provider, machine)]) if machine is not None else None) \
            or _median([r.ready_s for r in self.rentals_of(provider)])

    def mbps(self, provider, machine, direction):
        """Measured transfer speed from here (real transfers), the machine before its
        provider; None if gipper hasn't timed one."""
        pick = (lambda r: r.up_mbps) if direction == "up" else (lambda r: r.down_mbps)
        scopes = ((self.rentals_of(provider, machine),) if machine is not None else ()) + (self.rentals_of(provider),)
        for rs in scopes:
            v = _median([x for r in rs for x in pick(r)])
            if v:
                return v
        return None

    def compute_s(self, workload, gpu):
        """Median compute seconds this workload took on this GPU before."""
        from gipper.market import matches
        return _median([r.compute_s for r in self.rentals if r.compute_s and r.gpu and matches(gpu, r.gpu)
                        and self.workloads.get(r.job) == workload])

    def billing_factor(self, provider):
        """Billed / computed over rentals of MIN_BILLED_S or more, as one ratio of
        totals: how much a provider's bill exceeds rate x lifetime for real work.
        (Short rentals mostly measure rounding: a 47 s Vast run billed 5x.)"""
        rs = [r for r in self.rentals_of(provider)
              if r.billed_usd is not None and r.computed_usd > 0 and r.computed_s >= MIN_BILLED_S]
        computed = sum(r.computed_usd for r in rs)
        return sum(r.billed_usd for r in rs) / computed if computed > 0.05 else None

    def summary(self, provider):
        rs = self.rentals_of(provider)
        judged = [r for r in rs if r.outcome]
        return {"rentals": len(rs), "good": sum(r.outcome == "good" for r in judged),
                "bad": sum(r.outcome == "bad" for r in judged), "failure": self.failure(provider),
                "ready_s": self.ready_s(provider), "up_mbps": self.mbps(provider, None, "up"),
                "down_mbps": self.mbps(provider, None, "down"), "billing_factor": self.billing_factor(provider),
                "cleanup_pending": sum(r.cleanup_pending for r in rs),
                "reasons": [r.why for r in judged if r.outcome == "bad"][-5:]}


def _s(v):
    return str(v) if v is not None else None
