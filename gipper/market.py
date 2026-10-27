"""Shopping across providers: every market answers with the same Offer, and one
ranking decides (the job's GPU preference, then known stock, then price).

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

    @property
    def label(self):
        return f"{self.provider}:{self.gpu}"


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


def admit(offers, *, min_mem_gb=0, regions=(), ports=()):
    """Drop what can't serve the job: too little memory, outside its regions, no UDP."""
    regions = {r.upper() for r in regions}
    out = []
    for o in offers:
        if o.mem_gb < min_mem_gb:
            continue
        if regions and (o.country or "").upper() not in regions:
            continue
        if wants_udp(ports) and not o.udp:
            continue
        out.append(o)
    return out


def rank(offers, prefer=(), providers=()):
    """Preferred GPUs first (in the job's order), then offers with known stock, then
    cheapest; the provider order in settings breaks ties."""
    def pref_rank(o):
        return next((i for i, p in enumerate(prefer) if matches(p, o.gpu)), len(prefer))
    prov = {p: i for i, p in enumerate(providers)}
    return sorted(offers, key=lambda o: (pref_rank(o), o.stock is None, o.usd_h, prov.get(o.provider, len(prov))))


def ceiling(offers, job_max=None, prefer=()):
    """Don't let a busy cheap type silently become one several times the price (the
    smoke test got a $0.69 5090 for a $0.34 4090 ask): the job's own ceiling, else 2x
    the cheapest offer with stock among the GPUs the job prefers (among all, if it
    prefers none), so a market's cheap long tail doesn't price out the card asked for."""
    if job_max:
        return job_max
    pool = [o for o in offers if any(matches(p, o.gpu) for p in prefer)] or offers
    stocked = [o.usd_h for o in pool if o.stock] or [o.usd_h for o in pool]
    return 2 * min(stocked) if stocked else 0


def shop(job, *, names=None, keyed_only=True):
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
    offers = admit(offers, min_mem_gb=job.min_mem_gb, regions=job.regions, ports=job.ports)
    return rank(offers, job.prefer, names), notes
