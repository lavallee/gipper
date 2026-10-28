"""Find and end rented GPUs gipper started and forgot, on every provider with a key.

A machine is reaped when its name starts with `gipper-` and either the ledger has
no live run for it (created but never terminated by a running client) past a grace
period, or it is past the deadline recorded at creation. Machines not named
`gipper-` are never touched.
"""
import time
from datetime import datetime

from gipper import ledger
from gipper.config import settings
from gipper.providers import load

GRACE_S = 20 * 60  # a client may still be uploading or waiting for ssh


def state():
    """(provider, id) -> {job, created, deadline, terminated}; receipts from before
    there was more than one provider are Runpod's."""
    known = {}
    for r in ledger.read(kinds={"pod.create", "pod.terminate", "pod.closure_verified", "pod.termination_pending", "reap"}):
        pid = r["payload"].get("pod")
        if not pid:
            continue
        k = (r["payload"].get("provider") or "runpod", str(pid))
        p = known.setdefault(k, {"job": r["job"], "terminated": False})
        if r["kind"] == "pod.create":
            p["created"] = r["ts"]
            p["deadline"] = r["payload"].get("deadline")
            p["usd_h"] = r["payload"].get("usd_per_h")
            try:
                p["started"] = datetime.fromisoformat(r["ts"]).timestamp()
            except (ValueError, TypeError):
                p["started"] = None
        elif r["kind"] == "pod.termination_pending":
            p["pending"] = True
            if "delete_accepted" in r["payload"]:
                p["delete_accepted"] = r["payload"]["delete_accepted"]
            p["terminated"] = False
        else:
            p["terminated"] = True
            p["pending"] = False
    return known


def verdict(rec, machine, now):
    if rec is None:
        started = machine.get("started")
        return None if started and now - started < GRACE_S else "not in the ledger"
    if rec.get("pending"):
        return "termination pending; closure unverified"
    if rec["terminated"]:
        return "ledger says terminated but it is still listed"
    if rec.get("deadline") and now > rec["deadline"]:
        return "past its deadline"
    return None


def reap(dry_run=False, providers=None):
    """[(provider, id, name, why)] acted on (or that would be, with dry_run)."""
    known = state()
    acted = []
    now = time.time()
    for p in providers or [load(n) for n in settings()["providers"]]:
        if not p.has_key():
            continue
        try:
            machines = p.list_ours()
        except Exception as e:  # one provider being down must not stop the others' sweep
            print(f"{p.name}: could not list machines: {e}")
            continue
        # An accepted but asynchronous DELETE can disappear between sweeps.
        # Keep its ledger claim until a new positive provider read confirms it.
        listed = {str(m["id"]) for m in machines}
        machines = list(machines) + [{"id": iid, "name": "gipper-pending-recovery"}
                   for (provider, iid), rec in known.items()
                   if provider == p.name and rec.get("pending") and rec.get("delete_accepted") and iid not in listed]
        for m in machines:
            why = verdict(known.get((p.name, str(m["id"]))), m, now)
            if not why:
                continue
            acted.append((p.name, m["id"], m["name"], why))
            if not dry_run:
                from gipper.lifecycle import terminate
                rec = known.get((p.name, str(m["id"]))) or {}
                try:
                    closed = terminate(p, m["id"], job_id=rec.get("job"), started=rec.get("started"),
                                       usd_h=rec.get("usd_h"), delete_accepted=bool(rec.get("delete_accepted")))
                except Exception as error:
                    print(f"{p.name}: cleanup pending for {m['id']}: {error}")
                    continue
                if closed:
                    ledger.write("reap", {"provider": p.name, "pod": m["id"], "name": m["name"], "why": why}, job=rec.get("job"))
    return acted
