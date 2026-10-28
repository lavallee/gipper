"""What providers actually charged, beside what gipper computed.

gipper's own `spend` receipt is rate x lifetime, written at teardown. Bills differ:
Vast charges disk from creation and GPU only while running, rounds up short
rentals (a 47 s smoke run was billed 0.07 h) and charges bandwidth by the byte;
Runpod bills container disk alongside the GPU. `reconcile()` fetches each
provider's itemised charges for gipper's machines and records them as
`spend.billed`, which `spend_since()` (the daily cap) and gipper.history (the
billing factor used in estimates) then prefer.
"""
import time
from collections import defaultdict
from datetime import datetime, timezone

from gipper import ledger
from gipper.config import settings
from gipper.providers import load


def _ours(since):
    """(provider, id) -> job for every machine gipper created since `since`."""
    return {((r["payload"].get("provider") or "runpod"), str(r["payload"]["pod"])): r["job"]
            for r in ledger.read(since=since, kinds={"pod.create"}) if r["payload"].get("pod")}


def reconcile(days=7, providers=None):
    """Write `spend.billed` for gipper's machines whose bill has grown. Returns
    [(provider, id, usd)] written. Providers bill by the day, and a day's charges
    grow until it closes, so the query starts at a UTC midnight with a day to
    spare, and a bill is only recorded if it exceeds the last one: a window that
    clipped a day can't replace a fuller bill. One provider's billing API failing
    doesn't stop the others."""
    since = (int(time.time() // 86400) - days - 1) * 86400
    ours = _ours(since - 86400)
    last = {}
    for r in ledger.read(since=since - 31 * 86400, kinds={"spend.billed"}):
        key = (r["payload"]["provider"], str(r["payload"]["pod"]))
        last[key] = max(last.get(key, 0.0), r["payload"]["usd"])
    written = []
    for p in providers or [load(n) for n in settings()["providers"]]:
        if not p.has_key():
            continue
        try:
            bills = p.billed(since)
        except Exception as e:
            print(f"{p.name}: could not read bills: {e}")
            continue
        for iid, (usd, detail) in (bills or {}).items():
            key = (p.name, str(iid))
            if key not in ours or round(usd, 4) <= round(last.get(key, -1), 4):
                continue
            ledger.write("spend.billed", {"provider": p.name, "pod": str(iid), "usd": round(usd, 6), "detail": detail,
                                          "basis": "provider charges"}, job=ours[key])
            written.append((p.name, str(iid), usd))
    return written


def spend_by_machine(since):
    """(provider, id) -> usd for machines whose spend falls since `since`: the
    largest bill where there is one, else gipper's computed spend. A machine's
    spend is dated by its teardown (`spend` receipt), as the daily cap always
    has been, or by its creation if it has no `spend` yet; a bill recorded days
    later inherits that date."""
    lookback = since - 31 * 86400 if since else None
    created, ended, computed, billed = {}, {}, defaultdict(float), {}
    for r in ledger.read(since=lookback, kinds={"pod.create", "spend", "spend.billed"}):
        key = (r["payload"].get("provider") or "runpod", str(r["payload"].get("pod")))
        if r["kind"] == "pod.create":
            created[key] = r["ts"]
        elif r["kind"] == "spend.billed":
            billed[key] = max(billed.get(key, 0.0), r["payload"]["usd"])
        else:
            computed[key] += r["payload"].get("usd", 0.0)
            ended[key] = r["ts"]
    since_iso = datetime.fromtimestamp(since, timezone.utc).isoformat(timespec="seconds") if since else ""
    out = {}
    for key in set(computed) | set(billed):
        when = ended.get(key) or created.get(key)
        if when is not None and when >= since_iso:
            out[key] = billed.get(key, computed.get(key, 0.0))
    return out


def spend_since(since):
    return sum(spend_by_machine(since).values())
