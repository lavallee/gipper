"""The whole-run view of an offer: how long the job would take there, what it would
cost, how likely the host is to fail, and one number to rank offers by.

    wall     = ready + setup + upload + fetch + compute + download
    expected = (rental for wall + bandwidth fees) x the provider's billing factor
    score    = expected + p_fail x (failure_cost + rental and time lost to a bad host)
               + hour_value x wall

Every figure comes from the best source available, and the estimate says which:
the job's own [placement] hints, the ledger's memory (gipper.history), what the
provider advertises, or a stated default.

The score is what makes short and long jobs come out differently without a
switch: on a 15-minute job, $0.40/h against $0.80/h is ten cents, smaller than a
26% chance of a bad host at $1 a failure, so reliability and speed win; on a
ten-hour job the same prices are four dollars apart, and price wins.

Objectives (placement.objective): "auto" values waiting at settings.hour_value_usd;
"cost" doesn't value waiting at all (failures still cost); "time" values it at
TIME_HOUR_VALUE; "latency" (interactive profile) is ranked by nearness first in
gipper.market, and scored like "auto".

The estimate also carries 0.1's field names (estimated_total_usd, setup_download_s,
assumptions, ...), which clients' reports and the estimate receipt still read.
"""
from gipper.config import settings
from gipper.placement import input_gb

READY_S = {"runpod": 90, "vast": 120, "shadeform": 420}  # until ssh, before memory has a median
SETUP_S = 120               # bootstrap and the job's own setup, if the job doesn't say
COMPUTE_S = 1800            # if neither the job nor memory says
PREFLIGHT_S = 60            # time a bad host costs beyond becoming ready
TIME_HOUR_VALUE = 50.0      # $/h of waiting under objective = "time"


def _mbps(measured, advertised, p, client_cap=None):
    """Link speed and where it came from; a client-side cap limits whichever applies."""
    if measured:
        v, src = measured, "measured"
    elif advertised and advertised > 0:
        v, src = advertised * p.transfer_efficiency, "advertised"
    else:
        v, src = p.fallback_mbps * p.transfer_efficiency, "default"
    if client_cap and client_cap * p.transfer_efficiency < v:
        return client_cap * p.transfer_efficiency, "client cap"
    return v, src


def estimate(job, offer, history, *, throughput=None):
    """Phases (seconds), dollars, risk and score for running `job` on `offer`."""
    from gipper.market import matches
    s, p, m = settings(), job.placement, offer.metrics
    machine = offer.ref.get("machine")
    basis = {}

    ready = history.ready_s(offer.provider, machine)
    basis["ready"] = "memory" if ready else "default"
    ready = ready or READY_S.get(offer.provider, 180)

    setup = p.setup_s if p.setup_s is not None else SETUP_S
    basis["setup"] = "job" if p.setup_s is not None else "default"

    up_gb = p.upload_gb if p.upload_gb is not None else input_gb(job)
    up_mbps, basis["upload"] = _mbps(history.mbps(offer.provider, machine, "up", offer.country),
                                     m.get("host_download_mbps"), p, p.client_upload_mbps)
    down_mbps, basis["download"] = _mbps(history.mbps(offer.provider, machine, "down", offer.country),
                                         m.get("host_upload_mbps"), p, p.client_download_mbps)
    fetch_mbps, basis["fetch"] = _mbps(None, m.get("host_download_mbps"), p)  # host <- internet: no client cap
    upload, download = up_gb * 8000 / up_mbps, p.download_gb * 8000 / down_mbps
    fetch = p.setup_download_gb * 8000 / fetch_mbps

    by_gpu = next((v for g, v in p.compute_s_by_gpu.items() if matches(g, offer.gpu)), None)
    remembered = history.compute_s(job.workload, offer.gpu)
    if by_gpu:
        compute, basis["compute"] = by_gpu, "job (this GPU)"
    elif throughput and job.units_count:
        compute, basis["compute"] = job.units_count / throughput, "throughput"
    elif p.compute_s is not None:
        compute, basis["compute"] = p.compute_s, "job"
    elif remembered:
        compute, basis["compute"] = remembered, "memory"
    else:
        compute, basis["compute"] = min(COMPUTE_S, job.max_hours * 3600), "default"

    wall = ready + setup + upload + fetch + compute + download
    rental = wall / 3600 * offer.usd_h
    fee_in, fee_out = m.get("host_download_usd_per_gb"), m.get("host_upload_usd_per_gb")
    fees_known = offer.provider != "vast" or (fee_in is not None and fee_out is not None)
    bandwidth = (up_gb + p.setup_download_gb) * (fee_in or 0) + p.download_gb * (fee_out or 0)
    factor = history.billing_factor(offer.provider)
    basis["billing"] = "memory" if factor else "rate"
    expected = rental * (factor or 1.0) + bandwidth   # bills already itemise bandwidth: the factor is the rest

    p_fail = history.failure(offer.provider, machine)
    hour_value = {"auto": s["hour_value_usd"], "latency": s["hour_value_usd"], "cost": 0.0,
                  "time": TIME_HOUR_VALUE}[p.objective]
    lost_h = (ready + PREFLIGHT_S) / 3600
    risk = p_fail * (s["failure_cost_usd"] + lost_h * (offer.usd_h + hour_value))
    score = expected + risk + hour_value * wall / 3600
    from gipper.placement import best_rtt, distance_km
    rtt, rtt_basis = best_rtt(offer, p, history)
    phases = {"ready": round(ready), "setup": round(setup), "upload": round(upload), "fetch": round(fetch),
              "compute": round(compute), "download": round(download)}
    legacy = {   # 0.1's names, read by clients' reports
        "profile": p.profile, "estimated_total_usd": round(expected, 4),
        "estimated_bandwidth_usd": round(bandwidth, 4) if fees_known else None, "known_bandwidth_usd": round(bandwidth, 4),
        "bandwidth_fees_known": fees_known, "cost_is_lower_bound": not fees_known,
        "setup_s": phases["setup"], "setup_download_s": phases["fetch"], "upload_s": phases["upload"],
        "download_s": phases["download"], "compute_s": phases["compute"],
        "upload_gb": up_gb, "download_gb": p.download_gb, "setup_download_gb": p.setup_download_gb,
        "host_download_mbps": m.get("host_download_mbps"), "host_upload_mbps": m.get("host_upload_mbps"),
        "host_download_usd_per_gb": fee_in, "host_upload_usd_per_gb": fee_out,
        "effective_upload_mbps": round(up_mbps, 1), "effective_download_mbps": round(down_mbps, 1),
        "dlperf": m.get("dlperf"), "measured_rtt_ms": rtt if rtt_basis == "measured" else None,
        "latency_target": p.latency_target or None,
        "distance_km": round(distance_km(offer, p), 2) if distance_km(offer, p) is not None else None,
        "assumptions": [f"{k} from {v}" for k, v in basis.items()]
                       + ([] if fees_known else ["bandwidth fees not advertised: total is a lower bound"]),
    }
    return {**legacy, "wall_s": round(wall), "usd": round(expected, 4), "score": round(score, 4),
            "rtt_ms": rtt, "rtt_basis": rtt_basis,
            "phases_s": phases,
            "rental_usd": round(rental, 4), "bandwidth_usd": round(bandwidth, 4), "bandwidth_known": fees_known,
            "billing_factor": factor, "p_fail": round(p_fail, 3), "risk_usd": round(risk, 4),
            "objective": p.objective, "hour_value_usd": hour_value, "basis": basis}
