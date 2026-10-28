"""Ending a rental, and knowing it ended: a provider accepting the delete is not
the machine being gone. `terminate()` asks for the delete (retrying), then reads
the provider's list until the machine is absent. Only then does it write
`pod.closure_verified`, `pod.terminate` and the computed `spend`; until then the
machine stays `pod.termination_pending`, which the reaper picks up first, and
gipper rents no replacement while a previous machine may still be billing.
"""
import time

from gipper import ledger

DELETE_TRIES, LIST_TRIES, PAUSE_S = 5, 4, 2


def terminate(provider, iid, *, job_id=None, started=None, usd_h=None, refs=None, delete_accepted=False):
    """True once the provider no longer lists the machine."""
    iid = str(iid)
    base = {"provider": provider.name, "pod": iid}
    ledger.write("pod.termination_pending", {**base, "delete_accepted": delete_accepted,
                                             "reason": "termination requested; not yet confirmed gone"}, job=job_id)
    accepted, error = delete_accepted, None
    for attempt in range(0 if accepted else DELETE_TRIES):
        try:
            provider.terminate(iid)
            accepted = True
            break
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            if attempt < DELETE_TRIES - 1:
                time.sleep(PAUSE_S)
    gone = False
    if accepted:
        for attempt in range(LIST_TRIES):
            try:
                listed = provider.list_ours()
                if not isinstance(listed, list) or not all(isinstance(m, dict) and "id" in m for m in listed):
                    raise ValueError("provider returned an unreadable machine list")
                gone = not any(str(m["id"]) == iid for m in listed)
                error = None if gone else "provider still lists the machine"
                if gone:
                    break
            except Exception as e:   # an unreadable list is not proof of absence
                error = f"{type(e).__name__}: {e}"
            if attempt < LIST_TRIES - 1:
                time.sleep(PAUSE_S)
    if not gone:
        ledger.write("pod.termination_pending", {**base, "delete_accepted": accepted,
                                                 "reason": error or "not confirmed gone"}, job=job_id)
        print(f"{provider.name} {iid}: termination pending ({error or 'not confirmed gone'})", flush=True)
        return False
    ledger.write("pod.closure_verified", {**base, "basis": "delete accepted, then absent from the provider's list"},
                 job=job_id)
    ledger.write("pod.terminate", base, job=job_id)
    if started is not None and usd_h is not None:
        secs = max(0, time.time() - started)
        ledger.write("spend", {**base, "seconds": round(secs), "usd_per_h": usd_h, "usd": round(secs * usd_h / 3600, 4),
                               "basis": "rate x lifetime to confirmed closure", "accuracy": "estimated"},
                     job=job_id, refs=refs)
        print(f"{provider.name} {iid} terminated after {secs / 60:.1f} min: ~${secs * usd_h / 3600:.3f}", flush=True)
    return True
