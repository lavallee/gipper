"""The receipt ledger: append-only JSON lines, one file per month.

Every record is `gipper.receipt/v1`: {schema, id, ts, kind, job, refs, payload}.
Kinds so far: estimate, pod.create, pod.ready, run.start, run.end, spend,
checkpoint, collect, pod.terminate, cap, reap, error. Milton, Somm and replay
read these; nothing rewrites them.
"""
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from gipper.config import DATA, redact

SCHEMA = "gipper.receipt/v1"


def _dir():
    d = DATA / "receipts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_id(prefix="r"):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def write(kind, payload=None, *, job=None, refs=None):
    now = datetime.now(timezone.utc)
    rec = {"schema": SCHEMA, "id": new_id(), "ts": now.isoformat(timespec="seconds"), "kind": kind,
           "job": job, "refs": refs or {}, "payload": payload or {}}
    line = redact(json.dumps(rec, default=str))
    path = _dir() / f"{now:%Y-%m}.jsonl"
    with open(path, "a") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    return rec


def read(since=None, kinds=None, job=None):
    """Yield receipts (oldest first); since = epoch seconds or ISO date."""
    if isinstance(since, str):
        since = datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp()
    for path in sorted(_dir().glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if since and datetime.fromisoformat(rec["ts"]).timestamp() < since:
                continue
            if kinds and rec["kind"] not in kinds:
                continue
            if job and rec.get("job") != job:
                continue
            yield rec


def spend_since(since):
    return sum(r["payload"].get("usd", 0.0) for r in read(since=since, kinds={"spend"}))


def today_start():
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
