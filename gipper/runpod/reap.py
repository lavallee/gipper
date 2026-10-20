"""Find and end rented GPUs gipper started and forgot.

A pod is reaped when its name starts with `gipper-` and either the ledger has no
live run for it (created but never terminated by a running client) past a grace
period, or it is past the deadline recorded at creation. Pods not named
`gipper-` are never touched.
"""
import time

from gipper import ledger
from gipper.runpod.api import Api

GRACE_S = 20 * 60  # a client may still be uploading or waiting for ssh


def state():
    """pod id -> {job, created_ts, deadline, terminated}"""
    pods = {}
    for r in ledger.read(kinds={"pod.create", "pod.terminate", "reap"}):
        pid = r["payload"].get("pod")
        if not pid:
            continue
        p = pods.setdefault(pid, {"job": r["job"], "terminated": False})
        if r["kind"] == "pod.create":
            p["created"] = r["ts"]
            p["deadline"] = r["payload"].get("deadline")
        else:
            p["terminated"] = True
    return pods


def reap(dry_run=False, api=None):
    api = api or Api()
    known = state()
    acted = []
    now = time.time()
    for pod in api.list_pods():
        name, pid = pod.get("name") or "", pod.get("id")
        if not name.startswith("gipper-"):
            continue
        rec = known.get(pid)
        why = None
        if rec is None:
            started = pod.get("lastStartedAt")
            why = "not in the ledger"
            if started:
                from datetime import datetime
                try:
                    age = now - datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
                    if age < GRACE_S:
                        why = None
                except ValueError:
                    pass
        elif rec["terminated"]:
            why = "ledger says terminated but it is still listed"
        elif rec.get("deadline") and now > rec["deadline"]:
            why = "past its deadline"
        if why:
            acted.append((pid, name, why))
            if not dry_run:
                api.terminate_pod(pid)
                ledger.write("reap", {"pod": pid, "name": name, "why": why}, job=(rec or {}).get("job"))
    return acted
