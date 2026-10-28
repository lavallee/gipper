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
TIME_HOUR_VALUE.
"""
from gipper.config import settings
from gipper.placement import input_gb

READY_S = {"runpod": 90, "vast": 120, "shadeform": 420}  # until ssh, before memory has a median
SETUP_S = 120               # bootstrap and the job's own setup, if the job doesn't say
COMPUTE_S = 1800            # if neither the job nor memory says
FALLBACK_MBPS = 100         # a link nobody has advertised or measured
PREFLIGHT_S = 60            # time a bad host costs beyond becoming ready
TIME_HOUR_VALUE = 50.0      # $/h of waiting under objective = "time"


def _mbps(measured, advertised, efficiency):
    if measured:
        return measured, "measured"
    if advertised and advertised > 0:
        return advertised * efficiency, "advertised"
    return FALLBACK_MBPS * efficiency, "default"


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
    up_mbps, basis["upload"] = _mbps(history.mbps(offer.provider, machine, "up"), m.get("host_download_mbps"),
                                     p.transfer_efficiency)
    down_mbps, basis["download"] = _mbps(history.mbps(offer.provider, machine, "down"), m.get("host_upload_mbps"),
                                         p.transfer_efficiency)
    fetch_mbps, basis["fetch"] = _mbps(None, m.get("host_download_mbps"), p.transfer_efficiency)  # host <- internet
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
    hour_value = {"auto": s["hour_value_usd"], "cost": 0.0, "time": TIME_HOUR_VALUE}[p.objective]
    lost_h = (ready + PREFLIGHT_S) / 3600
    risk = p_fail * (s["failure_cost_usd"] + lost_h * (offer.usd_h + hour_value))
    score = expected + risk + hour_value * wall / 3600
    return {"wall_s": round(wall), "usd": round(expected, 4), "score": round(score, 4),
            "phases_s": {"ready": round(ready), "setup": round(setup), "upload": round(upload), "fetch": round(fetch),
                         "compute": round(compute), "download": round(download)},
            "rental_usd": round(rental, 4), "bandwidth_usd": round(bandwidth, 4), "bandwidth_known": fees_known,
            "billing_factor": factor, "p_fail": round(p_fail, 3), "risk_usd": round(risk, 4),
            "objective": p.objective, "hour_value_usd": hour_value, "basis": basis}
