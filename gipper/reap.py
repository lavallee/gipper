"""Find and end rented GPUs gipper started and forgot, on every provider with a key.

A machine is reaped when its name starts with `gipper-` and either the ledger has
no live run for it (created but never terminated by a running client) past a grace
period, or it is past the deadline recorded at creation. Machines not named
`gipper-` are never touched.
"""
import time

from gipper import ledger
from gipper.config import settings
from gipper.providers import load

GRACE_S = 20 * 60  # a client may still be uploading or waiting for ssh


def state():
    """(provider, id) -> {job, created, deadline, terminated}; receipts from before
    there was more than one provider are Runpod's."""
    known = {}
    for r in ledger.read(kinds={"pod.create", "pod.terminate", "reap"}):
        pid = r["payload"].get("pod")
        if not pid:
            continue
        k = (r["payload"].get("provider") or "runpod", str(pid))
        p = known.setdefault(k, {"job": r["job"], "terminated": False})
        if r["kind"] == "pod.create":
            p["created"] = r["ts"]
            p["deadline"] = r["payload"].get("deadline")
        else:
            p["terminated"] = True
    return known


def verdict(rec, machine, now):
    if rec is None:
        started = machine.get("started")
        return None if started and now - started < GRACE_S else "not in the ledger"
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
        for m in machines:
            why = verdict(known.get((p.name, str(m["id"]))), m, now)
            if not why:
                continue
            acted.append((p.name, m["id"], m["name"], why))
            if not dry_run:
                p.terminate(m["id"])
                rec = known.get((p.name, str(m["id"]))) or {}
                ledger.write("reap", {"provider": p.name, "pod": m["id"], "name": m["name"], "why": why}, job=rec.get("job"))
    return acted
