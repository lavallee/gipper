"""Shopping across providers: every market answers with the same Offer, and one
ranking decides: the job's GPU preference first, then the whole-run score from
gipper.costs (expected dollars, the chance of a bad host, time), then known stock.

GPU names differ by provider ("NVIDIA GeForce RTX 4090", "RTX 4090", "RTX4090"),
so preferences match on a normalised name. Regions are ISO country codes ("US",
"CA"); each provider maps its own locations onto them.
"""
import re
from dataclasses import dataclass, field

from gipper.config import settings


@dataclass
class Offer:
    provider: str
    gpu: str                 # the provider's own name for the GPU
    mem_gb: float
    usd_h: float
    where: str = ""          # the provider's location ("EUR-IS-2", "Michigan, US", "desmoines-usa-1")
    country: str | None = None
    stock: str | None = None  # High/Medium/Low, "yes" for a specific machine, None if unknown
    udp: bool = False        # can expose UDP ports
    ref: dict = field(default_factory=dict)  # what the provider needs to rent it
    metrics: dict = field(default_factory=dict)   # what the provider advertises (speeds, fees, CUDA); missing stays missing
    estimate: dict = field(default_factory=dict)  # gipper.costs.estimate for the job being shopped

    @property
    def label(self):
        return f"{self.provider}:{self.gpu}"

    @property
    def placement_estimate(self):
        """0.1's name for `estimate`."""
        return self.estimate


_NOISE = re.compile(r"nvidia|geforce|generation|pcie|sxm\d?|[^a-z0-9]")


def norm(name):
    """'NVIDIA GeForce RTX 4090' -> 'rtx4090' -> '4090'; 'RTX6000Ada' -> '6000ada'."""
    n = re.sub(r"(\d)gb\b", r"\1g", name.lower())   # "A100 80GB" and "A100_80G" are one card
    n = _NOISE.sub("", n)
    return n[3:] if n.startswith("rtx") and len(n) > 3 else n


def matches(pref, gpu):
    """Same card, or the same card with a memory size ("A100" matches "A100 80GB");
    a variant is another card ("4090" does not match "4090D", "L40" not "L40S")."""
    p, g = norm(pref), norm(gpu)
    return bool(p) and (g == p or (g.startswith(p) and g[len(p)].isdigit()))


def wants_udp(ports):
    return any(p.lower().endswith("/udp") for p in ports)


def admit(offers, *, min_mem_gb=0, regions=(), ports=(), placement=None):
    """Drop what can't serve the job: too little memory, outside its regions, no UDP,
    or below a [placement] floor the provider advertises. A floor on a figure the
    provider doesn't advertise passes here and is checked on the machine itself."""
    regions = {r.upper() for r in regions}
    from gipper.placement import distance_km, rtt_ms
    floors = [] if placement is None else [
        ("host_download_mbps", placement.min_host_download_mbps), ("host_upload_mbps", placement.min_host_upload_mbps),
        ("cuda_max_good", placement.min_cuda), ("compute_capability", placement.min_compute_capability),
        ("dlperf", placement.min_dlperf)]
    out = []
    for o in offers:
        if o.mem_gb < min_mem_gb:
            continue
        if regions and (o.country or "").upper() not in regions:
            continue
        if wants_udp(ports) and not o.udp:
            continue
        if any(floor is not None and o.metrics.get(k) is not None and o.metrics[k] < floor for k, floor in floors):
            continue
        if placement is not None and placement.max_rtt_ms is not None:   # a limit on what wasn't measured fails
            rtt = rtt_ms(o, placement)
            if rtt is None or rtt > placement.max_rtt_ms:
                continue
        if placement is not None and placement.max_distance_km is not None:
            km = distance_km(o, placement)
            if km is None or km > placement.max_distance_km:
                continue
        out.append(o)
    return out


def rank(offers, prefer=(), providers=(), *, job=None, history=None, throughput=None):
    """Preferred GPUs first (in the job's order), then offers known to be in stock
    (Runpod's types without stock mostly fail to rent), then the lowest whole-run
    score (needs the job), then the provider order in settings."""
    def pref_rank(o):
        return next((i for i, p in enumerate(prefer) if matches(p, o.gpu)), len(prefer))
    prov = {p: i for i, p in enumerate(providers)}
    if job is not None:
        from gipper.costs import estimate
        from gipper.history import History
        history = history or History.load()
        for o in offers:
            o.estimate = estimate(job, o, history, throughput=throughput)
    if job is not None and job.placement.objective == "latency":
        from gipper.placement import distance_km, rtt_ms
        p = job.placement

        def near(o):   # measured round trip, then preferred country, then distance
            rtt, km = rtt_ms(o, p), distance_km(o, p)
            country = p.preferred_countries.index(o.country) if o.country in p.preferred_countries else len(p.preferred_countries)
            return (rtt is None, rtt if rtt is not None else 0, country, km if km is not None else float("inf"))
        return sorted(offers, key=lambda o: (pref_rank(o), o.stock is None, *near(o), o.estimate.get("score", o.usd_h),
                                             prov.get(o.provider, len(prov))))
    return sorted(offers, key=lambda o: (pref_rank(o), o.stock is None, o.estimate.get("score", o.usd_h),
                                         prov.get(o.provider, len(prov))))


def ceiling(offers, job_max=None, prefer=()):
    """0.1's hourly ceiling (2x the cheapest stocked preferred offer); kept for callers
    that print it. Ranking no longer needs it: the score already prices the run."""
    if job_max:
        return job_max
    pool = [o for o in offers if any(matches(p, o.gpu) for p in prefer)] or offers
    stocked = [o.usd_h for o in pool if o.stock] or [o.usd_h for o in pool]
    return 2 * min(stocked) if stocked else 0


def shop(job, *, names=None, keyed_only=True, history=None, throughput=None):
    """Offers that can serve the job from every usable provider, ranked, plus notes on
    providers that were skipped or failed (one market being down never stops another)."""
    from gipper.providers import load
    s = settings()
    names = names or job.providers or s["providers"]
    if job.volume:  # network volumes are Runpod's
        names = [n for n in names if n == "runpod"]
    offers, notes = [], []
    for name in names:
        try:
            p = load(name)
        except KeyError as e:
            notes.append(str(e))
            continue
        if keyed_only and not p.has_key():
            notes.append(f"{name}: no {p.key_name}; skipped")
            continue
        if wants_udp(job.ports) and not p.udp:
            notes.append(f"{name}: no UDP; skipped")
            continue
        try:
            offers += p.offers(job)
        except (Exception, SystemExit) as e:  # one market's outage (or missing key) must not stop the others
            notes.append(f"{name}: {e}")
    offers = admit(offers, min_mem_gb=job.min_mem_gb, regions=job.regions, ports=job.ports, placement=job.placement)
    return rank(offers, job.prefer, names, job=job, history=history, throughput=throughput), notes
