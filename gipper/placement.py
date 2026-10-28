"""What a job tells gipper about itself so offers can be judged on the whole run,
not the hourly price: `[placement]` in the job file. Everything is optional;
whatever is missing comes from memory (gipper.history) or a stated default.

    [placement]
    objective = "auto"            # auto | cost | time (see docs/placement.md)
    compute_s = 550               # expected compute seconds once inputs are up
    compute_s_by_gpu = { A40 = 250 }   # per GPU, where known
    setup_s = 120                 # the job's own setup, excluding the downloads below
    setup_download_gb = 8.5       # what the job fetches itself (weights, wheels)
    upload_gb = 0.1               # default: the size of [[inputs]]
    download_gb = 0.2             # outputs coming back
    transfer_efficiency = 0.5     # share of a host's advertised speed to expect
    min_host_download_mbps = 500  # hard filters on what the provider advertises
    min_host_upload_mbps = 100
    min_cuda = 12.8
    min_compute_capability = 8.0
    probe_transfer_mb = 2         # measure the ssh path before uploading (MiB each way)
    probe_timeout_s = 120
    min_measured_upload_mbps = 1
    min_measured_download_mbps = 1
    min_dlperf = 30               # Vast's generic DLPerf score: a filter, never a speed
    client_upload_mbps = 40       # this end's link, if it's the bottleneck
    client_download_mbps = 300
    fallback_mbps = 100           # a link nobody has advertised or measured

Real-time work (`profile = "interactive"`, objective "latency") ranks by measured
round trip to where the user is, then preferred country, then distance, then the
usual score:

    profile = "interactive"
    latency_target = "toronto-unreal-host"            # names the client the RTTs were measured from;
                                                      # "here" means this machine, so its pings count
    measured_rtt_ms_by_machine = { "vast:machine:12345" = 28 }
    max_rtt_ms = 50                                   # needs a measurement: unmeasured offers fail it
    probe_rtt = true                                  # ping candidates before renting (default for interactive)
    preferred_countries = ["CA", "US"]
    near_latitude = 43.65                             # with max_distance_km: offers without
    near_longitude = -79.38                           # coordinates fail it
    max_distance_km = 1500
"""
import fnmatch
import math
from dataclasses import dataclass, field, fields
from pathlib import Path

OBJECTIVES = ("auto", "cost", "time", "latency")


@dataclass
class Placement:
    objective: str = "auto"
    profile: str = "batch"               # batch | interactive
    compute_s: float | None = None
    compute_s_by_gpu: dict = field(default_factory=dict)
    setup_s: float | None = None
    setup_download_gb: float = 0.0
    upload_gb: float | None = None
    download_gb: float = 0.0
    transfer_efficiency: float = 0.5
    min_host_download_mbps: float | None = None
    min_host_upload_mbps: float | None = None
    min_cuda: float | None = None
    min_compute_capability: float | None = None
    probe_transfer_mb: float = 0.0
    probe_timeout_s: float = 120.0
    min_measured_upload_mbps: float | None = None
    min_measured_download_mbps: float | None = None
    min_dlperf: float | None = None
    client_upload_mbps: float | None = None
    client_download_mbps: float | None = None
    fallback_mbps: float = 100.0
    preferred_countries: list = field(default_factory=list)
    near_latitude: float | None = None
    near_longitude: float | None = None
    max_distance_km: float | None = None
    max_rtt_ms: float | None = None
    latency_target: str = ""
    measured_rtt_ms_by_machine: dict = field(default_factory=dict)
    probe_rtt: bool | None = None        # default: on for interactive jobs


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def load_placement(data, *, error=ValueError):
    """The job's [placement] table (absent: all defaults), validated."""
    data = dict(data or {})
    unknown = set(data) - {f.name for f in fields(Placement)}
    if unknown:
        raise error(f"[placement] has unknown fields: {', '.join(sorted(unknown))} (see gipper/placement.py)")
    if data.get("profile") == "interactive":
        data.setdefault("objective", "latency")
        data.setdefault("probe_rtt", True)
    p = Placement(**data)
    if p.objective not in OBJECTIVES or p.profile not in ("batch", "interactive"):
        raise error(f"placement.profile is batch or interactive; objective one of {', '.join(OBJECTIVES)}")
    if (p.objective == "latency") != (p.profile == "interactive"):
        raise error('placement: objective "latency" goes with profile "interactive", and only with it')
    for name in ("compute_s", "setup_s", "setup_download_gb", "upload_gb", "download_gb", "min_host_download_mbps",
                 "min_host_upload_mbps", "min_cuda", "min_compute_capability", "probe_transfer_mb", "probe_timeout_s",
                 "min_measured_upload_mbps", "min_measured_download_mbps", "min_dlperf", "client_upload_mbps",
                 "client_download_mbps", "fallback_mbps", "max_distance_km", "max_rtt_ms"):
        v = getattr(p, name)
        if v is not None and (not _number(v) or v < 0):
            raise error(f"placement.{name} must be a non-negative number")
    if not (_number(p.transfer_efficiency) and 0 < p.transfer_efficiency <= 1):
        raise error("placement.transfer_efficiency must be in (0, 1]")
    if not isinstance(p.compute_s_by_gpu, dict) or not all(isinstance(k, str) and _number(v) and v > 0
                                                           for k, v in p.compute_s_by_gpu.items()):
        raise error("placement.compute_s_by_gpu maps GPU names to seconds")
    if p.probe_transfer_mb and not 1 <= p.probe_transfer_mb <= 64 or p.probe_timeout_s > 300:
        raise error("placement.probe_transfer_mb is 0 (off) or 1-64 MiB; probe_timeout_s at most 300")
    if (p.min_measured_upload_mbps or p.min_measured_download_mbps) and not p.probe_transfer_mb:
        raise error("placement.min_measured_*_mbps need probe_transfer_mb")
    if not p.fallback_mbps:
        raise error("placement.fallback_mbps must be positive")
    if not isinstance(p.measured_rtt_ms_by_machine, dict) or not all(
            isinstance(k, str) and _number(v) and v > 0 for k, v in p.measured_rtt_ms_by_machine.items()):
        raise error('placement.measured_rtt_ms_by_machine maps "provider:machine:id" to milliseconds')
    if not isinstance(p.preferred_countries, list) or not all(isinstance(c, str) and len(c) == 2
                                                               for c in p.preferred_countries):
        raise error("placement.preferred_countries is a list of ISO country codes")
    p.preferred_countries = [c.upper() for c in p.preferred_countries]
    coords = (p.near_latitude, p.near_longitude)
    if any(v is not None for v in coords) and not (all(_number(v) for v in coords)
                                                   and abs(coords[0]) <= 90 and abs(coords[1]) <= 180):
        raise error("placement needs a valid near_latitude and near_longitude pair")
    if p.max_distance_km is not None and p.near_latitude is None:
        raise error("placement.max_distance_km needs near_latitude and near_longitude")
    if (p.max_rtt_ms is not None or p.measured_rtt_ms_by_machine) and not p.latency_target:
        raise error("placement: RTT measurements need latency_target, naming where they were measured from")
    return p


def distance_km(offer, p):
    """Great-circle distance from the job's near_* point to the offer, if both are known."""
    lat, lon = offer.metrics.get("latitude"), offer.metrics.get("longitude")
    if p.near_latitude is None or not (_number(lat) and _number(lon)):
        return None
    a, b, c, d = map(math.radians, (p.near_latitude, p.near_longitude, lat, lon))
    h = math.sin((c - a) / 2) ** 2 + math.cos(a) * math.cos(c) * math.sin((d - b) / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1, math.sqrt(h)))


def target_is_here(p):
    from gipper.latency import HERE
    return p.latency_target in ("here", HERE)


def rtt_ms(offer, p):
    """A round trip that counts as measured from the job's latency_target: the job's
    own measurement, or a ping from here when the target is here."""
    machine = offer.ref.get("machine")
    own = p.measured_rtt_ms_by_machine.get(f"{offer.provider}:machine:{machine}") if machine is not None else None
    if own is not None:
        return own
    return offer.metrics.get("rtt_ms_here") if target_is_here(p) else None


def best_rtt(offer, p, history=None):
    """(ms, basis): the best round-trip figure there is for ranking. Measured from the
    target first, then pinged from here just now, then memory of this machine, then
    the median for its country, all from here."""
    from gipper.latency import HERE
    own = rtt_ms(offer, p)
    if own is not None:
        return own, "measured" if not target_is_here(p) or offer.metrics.get("rtt_ms_here") != own else "pinged from here"
    if offer.metrics.get("rtt_ms_here") is not None:
        return offer.metrics["rtt_ms_here"], "pinged from here (proxy)"
    if history is not None:
        v = history.rtt(offer.provider, offer.ref.get("machine"), HERE)
        if v is not None:
            return v, "memory of this machine"
        v = history.country_rtt(offer.country, HERE)
        if v is not None:
            return v, f"memory of {offer.country}"
    return None, "unknown"


def estimate_offer(job, offer, *, upload_gb=None):
    """0.1's name for the whole-run estimate (gipper.costs.estimate)."""
    from gipper.costs import estimate
    from gipper.history import History
    if upload_gb is not None:
        job.placement.upload_gb = upload_gb
    return estimate(job, offer, History.load())


def rank_for_job(offers, job, *, tie_key=None):
    """0.1's name for filtering and ranking offers for a job."""
    from gipper.market import admit, rank
    offers = admit(offers, min_mem_gb=job.min_mem_gb, regions=job.regions, ports=job.ports, placement=job.placement)
    return rank(offers, job.prefer, job=job)


def input_gb(job):
    """Bytes the [[inputs]] will upload (decimal GB), honouring their excludes."""
    size = 0
    for t in job.inputs:
        root = Path(t.src)
        for path in (root.rglob("*") if root.is_dir() else [root]):
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(root) if root.is_dir() else Path(path.name)
            if any(fnmatch.fnmatch(str(rel), pat) or any(fnmatch.fnmatch(part, pat) for part in rel.parts)
                   for pat in t.exclude):
                continue
            size += path.stat().st_size
    return size / 1e9
