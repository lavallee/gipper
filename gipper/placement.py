"""Opt-in workload placement with explicit units and missing-data assumptions.

No latency probing, provider mutation or implicit profile adoption happens here.
Vast DLPerf is a broad provider score, never a workload throughput conversion.
"""
from dataclasses import dataclass, field, fields
from pathlib import Path
import fnmatch
import math


@dataclass
class Placement:
    profile: str = "batch"
    objective: str = "cost"
    upload_gb: float | None = None       # local -> host; decimal GB (1e9 bytes)
    download_gb: float = 0.0             # host -> local outputs; decimal GB
    setup_download_gb: float = 0.0       # HF/pip/image traffic; does not use client uplink
    setup_s: float = 420.0               # fixed cold setup/SSH/resolver overhead, excludes explicit traffic
    compute_s: float = 1800.0            # assumed runtime when no matching workload measurement exists
    compute_s_by_gpu: dict = field(default_factory=dict)
    min_host_download_mbps: float | None = None
    min_host_upload_mbps: float | None = None
    client_upload_mbps: float | None = None
    client_download_mbps: float | None = None
    transfer_efficiency: float = 0.5     # explicit heuristic; advertised host speed is not path goodput
    fallback_mbps: float = 100.0
    probe_transfer_mb: float = 0.0       # MiB random bytes per direction, 0 disables
    probe_timeout_s: float = 120.0       # total deadline including cleanup
    min_measured_upload_mbps: float | None = None
    min_measured_download_mbps: float | None = None
    min_dlperf: float | None = None
    min_compute_capability: float | None = None
    min_cuda: float | None = None
    preferred_countries: list = field(default_factory=list)
    near_latitude: float | None = None
    near_longitude: float | None = None
    max_distance_km: float | None = None
    max_rtt_ms: float | None = None
    latency_target: str = ""
    measured_rtt_ms_by_machine: dict = field(default_factory=dict)


def finite(value, *, positive=False):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (value > 0 if positive else value >= 0))


def load_placement(data, *, error=ValueError):
    if data is None:
        return None
    if not isinstance(data, dict):
        raise error("[placement] must be a table")
    unknown = set(data) - {f.name for f in fields(Placement)}
    if unknown:
        raise error("Unknown placement fields: " + ", ".join(sorted(unknown)))
    value = dict(data)
    value.setdefault("objective", "latency" if value.get("profile") == "interactive" else "cost")
    p = Placement(**value)
    if p.profile not in {"batch", "interactive"} or p.objective not in {"cost", "time", "latency"}:
        raise error("placement profile must be batch/interactive; objective cost/time/latency")
    if p.objective == "latency" and p.profile != "interactive":
        raise error("Latency placement is an explicit interactive profile")
    nonnegative = ("upload_gb", "download_gb", "setup_download_gb", "setup_s", "compute_s", "probe_transfer_mb")
    positive = ("min_host_download_mbps", "min_host_upload_mbps", "client_upload_mbps",
                "client_download_mbps", "fallback_mbps", "min_dlperf", "min_compute_capability",
                "min_cuda", "max_distance_km", "max_rtt_ms", "probe_timeout_s",
                "min_measured_upload_mbps", "min_measured_download_mbps")
    for name in nonnegative + positive:
        v = getattr(p, name)
        optional = name not in {"download_gb", "setup_download_gb", "setup_s", "compute_s", "fallback_mbps", "probe_timeout_s", "probe_transfer_mb"}
        if (v is None and not optional) or (v is not None and not finite(v, positive=name in positive)):
            raise error(f"placement.{name} must be finite and {'positive' if name in positive else 'nonnegative'}")
    if p.probe_transfer_mb > 64 or (0 < p.probe_transfer_mb < 1) or p.probe_timeout_s > 300:
        raise error("probe_transfer_mb must be 0 or 1–64 MiB; probe_timeout_s must be <=300")
    if (p.min_measured_upload_mbps is not None or p.min_measured_download_mbps is not None) and not p.probe_transfer_mb:
        raise error("measured throughput thresholds require an enabled probe_transfer_mb")
    if not finite(p.transfer_efficiency, positive=True) or p.transfer_efficiency > 1:
        raise error("placement.transfer_efficiency must be in (0,1]")
    for name in ("compute_s_by_gpu", "measured_rtt_ms_by_machine"):
        values = getattr(p, name)
        if not isinstance(values, dict) or any(not isinstance(k, str) or not finite(v, positive=True) for k, v in values.items()):
            raise error(f"placement.{name} must map string identifiers to finite positive numbers")
    if not isinstance(p.preferred_countries, list) or any(not isinstance(c, str) or len(c) != 2 for c in p.preferred_countries):
        raise error("placement.preferred_countries must be a list of ISO country codes")
    p.preferred_countries = [c.upper() for c in p.preferred_countries]
    coords = (p.near_latitude, p.near_longitude)
    if any(v is not None for v in coords):
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in coords) or abs(coords[0]) > 90 or abs(coords[1]) > 180:
            raise error("placement needs a valid near_latitude/near_longitude pair")
    if p.max_distance_km is not None and p.near_latitude is None:
        raise error("max_distance_km needs explicit proximity coordinates")
    if not isinstance(p.latency_target, str):
        raise error("latency_target must name a client origin")
    if (p.max_rtt_ms is not None or p.measured_rtt_ms_by_machine) and not p.latency_target:
        raise error("RTT measurements/filter require a named latency_target")
    return p


def input_gb(job):
    """Conservative uncompressed upload size, honoring common rsync excludes.

    Symlinks are not followed. Rsync compression, cache reuse, sparse-file and
    complex rsync-filter differences remain explicit estimation limitations.
    """
    size = 0
    for transfer in job.inputs:
        root = Path(transfer.src)
        paths = root.rglob("*") if root.is_dir() else [root]
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            rel = str(path.relative_to(root)) if root.is_dir() else path.name
            if any(fnmatch.fnmatch(rel, pattern) or any(fnmatch.fnmatch(part, pattern) for part in Path(rel).parts)
                   for pattern in transfer.exclude):
                continue
            size += path.stat().st_size
    return size / 1e9


def proximity(offer, profile):
    lat, lon = offer.metrics.get("latitude"), offer.metrics.get("longitude")
    if (profile.near_latitude is None or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (lat, lon))
            or abs(lat) > 90 or abs(lon) > 180):
        return None
    a, b, c, d = map(math.radians, [profile.near_latitude, profile.near_longitude, lat, lon])
    hav = math.sin((c-a)/2)**2 + math.cos(a)*math.cos(c)*math.sin((d-b)/2)**2
    return 6371.0088 * 2 * math.asin(min(1, math.sqrt(max(0, hav))))


def measured_rtt(offer, profile):
    machine = offer.ref.get("machine")
    if machine is None:
        return None
    return profile.measured_rtt_ms_by_machine.get(f"{offer.provider}:machine:{machine}")


def estimate_offer(job, offer, *, upload_gb=None):
    from gipper.market import matches
    p = job.placement
    assert p is not None
    assumptions = []
    uploaded = p.upload_gb if p.upload_gb is not None else (input_gb(job) if upload_gb is None else upload_gb)
    if p.upload_gb is None:
        assumptions.append("upload size is a conservative input-file estimate; excludes approximated, compression/cache effects unmeasured")
    down, up = offer.metrics.get("host_download_mbps"), offer.metrics.get("host_upload_mbps")
    if not finite(down, positive=True):
        down = p.fallback_mbps
        assumptions.append(f"host download missing: assumed {down:g} Mbps")
    if not finite(up, positive=True):
        up = p.fallback_mbps
        assumptions.append(f"host upload missing: assumed {up:g} Mbps")
    send = min(down, p.client_upload_mbps) if p.client_upload_mbps is not None else down
    receive = min(up, p.client_download_mbps) if p.client_download_mbps is not None else up
    upload_s = uploaded * 8000 / (send * p.transfer_efficiency)
    download_s = p.download_gb * 8000 / (receive * p.transfer_efficiency)
    setup_download_s = p.setup_download_gb * 8000 / (down * p.transfer_efficiency)
    measured_compute = next((v for gpu, v in p.compute_s_by_gpu.items() if matches(gpu, offer.gpu)), None)
    compute = p.compute_s if measured_compute is None else measured_compute
    assumptions.append("compute is a supplied workload/GPU duration" if measured_compute is not None else "compute uses the explicit fallback duration; GPU workload speed unmeasured")
    assumptions.append(f"network goodput assumed {p.transfer_efficiency:g} × limiting advertised/client Mbps; external HF/pip speed and resolution latency unmeasured")
    probe_gb = p.probe_transfer_mb * 1024**2 / 1e9
    incoming_gb = uploaded + p.setup_download_gb + probe_gb
    outgoing_gb = p.download_gb + probe_gb
    down_fee = offer.metrics.get("host_download_usd_per_gb")
    up_fee = offer.metrics.get("host_upload_usd_per_gb")
    down_known, up_known = finite(down_fee), finite(up_fee)
    fees_known = down_known and up_known
    known_fees = (incoming_gb * down_fee if down_known else 0) + (outgoing_gb * up_fee if up_known else 0)
    assumptions.append("bandwidth uses advertised host $/GB rates and estimated traffic, not an invoice; taxes, retries and unmodeled traffic excluded")
    if not fees_known:
        assumptions.append("one or more bandwidth prices unknown: total is a lower bound using known charges; missing rates are not assumed zero")
    if p.probe_transfer_mb:
        assumptions.append("probe bytes included in fees; probe SSH startup/transfer time is additional unmeasured overhead")
    total = p.setup_s + upload_s + download_s + setup_download_s + compute
    rental_usd = round(total * offer.usd_h / 3600, 6)
    total_usd = round(total * offer.usd_h / 3600 + known_fees, 6)
    distance = proximity(offer, p)
    rtt = measured_rtt(offer, p)
    return {"profile": p.profile, "objective": p.objective, "wall_s": round(total, 3),
            "rental_usd": rental_usd, "usd": total_usd, "estimated_total_usd": total_usd,
            "estimated_bandwidth_usd": round(known_fees, 6) if fees_known else None,
            "known_bandwidth_usd": round(known_fees, 6), "cost_is_lower_bound": not fees_known,
            "incoming_gb": incoming_gb, "outgoing_gb": outgoing_gb, "probe_gb_per_direction": probe_gb,
            "host_download_usd_per_gb": down_fee if down_known else None,
            "host_upload_usd_per_gb": up_fee if up_known else None,
            "setup_s": p.setup_s, "upload_s": round(upload_s, 3), "download_s": round(download_s, 3),
            "setup_download_s": round(setup_download_s, 3), "compute_s": compute,
            "upload_gb": uploaded, "download_gb": p.download_gb, "setup_download_gb": p.setup_download_gb,
            "host_download_mbps": offer.metrics.get("host_download_mbps"), "host_upload_mbps": offer.metrics.get("host_upload_mbps"),
            "effective_upload_mbps": send * p.transfer_efficiency, "effective_download_mbps": receive * p.transfer_efficiency,
            "dlperf": offer.metrics.get("dlperf"), "dlperf_basis": "provider generic score; not a workload benchmark",
            "measured_rtt_ms": rtt, "latency_target": p.latency_target or None,
            "distance_km": round(distance, 2) if distance is not None else None,
            "geo_basis": "geographic proxy only; country/distance are not RTT",
            "bandwidth_fees_known": fees_known, "assumptions": assumptions}


def rank_for_job(offers, job, *, tie_key=lambda offer: ()):
    p = job.placement
    uploaded = p.upload_gb if p.upload_gb is not None else input_gb(job)
    admitted = []
    for offer in offers:
        thresholds = [("host_download_mbps", p.min_host_download_mbps), ("host_upload_mbps", p.min_host_upload_mbps),
                      ("dlperf", p.min_dlperf), ("compute_capability", p.min_compute_capability), ("cuda_max_good", p.min_cuda)]
        if any(limit is not None and (not finite(offer.metrics.get(metric), positive=True) or offer.metrics[metric] < limit)
               for metric, limit in thresholds):
            continue
        rtt, distance = measured_rtt(offer, p), proximity(offer, p)
        if p.max_rtt_ms is not None and (rtt is None or rtt > p.max_rtt_ms):
            continue
        if p.max_distance_km is not None and (distance is None or distance > p.max_distance_km):
            continue
        offer.placement_estimate = estimate_offer(job, offer, upload_gb=uploaded)
        admitted.append(offer)
    def key(offer):
        estimate = offer.placement_estimate
        if p.objective == "time":
            return (estimate["wall_s"], estimate["estimated_total_usd"], *tie_key(offer))
        if p.objective == "latency":
            rtt, distance = estimate["measured_rtt_ms"], estimate["distance_km"]
            country = next((i for i, value in enumerate(p.preferred_countries) if value == offer.country), len(p.preferred_countries))
            return (rtt is None, rtt if rtt is not None else math.inf, country,
                    distance if distance is not None else math.inf, estimate["estimated_total_usd"], *tie_key(offer))
        return (estimate["estimated_total_usd"], estimate["wall_s"], *tie_key(offer))
    return sorted(admitted, key=key)
