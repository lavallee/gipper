"""What reached each rented machine: the trail you read when a provider reports an
incident, or a machine turns out to have been a bad place to put something.

Every `Remote.put` first takes a manifest of exactly what tar would send (path, size,
sha256, mode), scans it for material that looks sensitive, stores the manifest under
the data directory and writes a `custody.upload` receipt. Only then do bytes leave.
No receipt, no upload.

Material that goes around `Remote.put` can't be seen, so it has to be declared:
`Remote.put_secrets` records the names it places, and `gipper custody declare` (or
`declare()`) records a scp, a tunnel or a machine gipper didn't rent. The report says
plainly what it can't vouch for, including machines a provider billed that the ledger
has never heard of.

Secret values are never recorded. A fingerprint is a keyed hash (the key stays beside
the ledger), so `gipper custody check` can tell whether a value is the one that was
exposed, and a stolen ledger can't be used to guess values.

Receipts: custody.machine, custody.upload, custody.uploaded, custody.secret, custody.note.
"""
import fnmatch
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets as _secrets
import subprocess
import tarfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from gipper import config, ledger

SCAN_MAX_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_FINDINGS = 200
ARCHIVES = (".tar", ".tgz", ".tar.gz", ".tar.zst", ".tar.xz", ".tar.bz2", ".zip", ".7z", ".rar", ".gz", ".zst")
PLACEHOLDER_SUFFIXES = (".example", ".sample", ".template", ".dist", ".pub")
NAME_RULES = [re.compile(p) for p in (
    r"\.env(\..+)?", r".+\.env", r"id_(rsa|dsa|ecdsa|ed25519)", r".+\.(pem|key|p12|pfx|tfstate)",
    r"\.netrc", r"\.pgpass", r"\.npmrc", r"\.pypirc", r"\.git-credentials", r"\.dockercfg",
    r"credentials(\.json)?", r"token\.json", r"service[-_]?account.*\.json")]
CONTENT_RULES = {name: re.compile(p) for name, p in {
    "private-key": rb"-----BEGIN [A-Z ]*PRIVATE KEY",
    "aws-key": rb"\bAKIA[0-9A-Z]{16}\b",
    "github-token": rb"\bgh[pousr]_[A-Za-z0-9]{30,}",
    "huggingface-token": rb"\bhf_[A-Za-z0-9]{30,}",
    "llm-api-key": rb"\bsk-[A-Za-z0-9_-]{20,}",
    "google-api-key": rb"\bAIza[0-9A-Za-z_-]{30,}",
    "slack-token": rb"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    "stripe-key": rb"\b[rs]k_live_[A-Za-z0-9]{16,}",
    "twilio-api-key": rb"\bSK[0-9a-f]{32}\b",
    "secret-assignment": rb"(?i)\b[A-Z0-9_]*(API_KEY|AUTH_TOKEN|ACCESS_TOKEN|SECRET|PASSWORD)\b\s*[=:]\s*[\"']?[A-Za-z0-9_\-/+=.]{16,}",
}.items()}


class CustodyError(RuntimeError):
    """The manifest couldn't be taken, so the upload doesn't happen."""


class Refused(RuntimeError):
    """The upload holds a known secret, or policy refuses what it found. Not a bad host."""


def _home():
    d = config.DATA / "custody"
    (d / "manifests").mkdir(parents=True, exist_ok=True)
    for p in (d, d / "manifests"):
        os.chmod(p, 0o700)
    return d


def _salt():
    path = _home() / "salt"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    value = _secrets.token_bytes(32)
    with os.fdopen(fd, "wb") as f:
        f.write(value)
    return value


def fingerprint(value):
    """Keyed, so a low-entropy value can't be guessed from the ledger. Stable per installation."""
    return hmac.new(_salt(), value.encode(), hashlib.sha256).hexdigest()[:32]


def read_values(path):
    """{name: value} from a dotenv or flat JSON file."""
    path = Path(path).expanduser()
    if path.suffix == ".json":
        data = json.loads(path.read_text())
        return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}
    return {k.removeprefix("export ").strip(): v for k, v in config._dotenv(path).items()}


SECRET_NAME = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|AUTH|CREDENTIAL|PRIVATE|COOKIE|SESSION)")


def _secret_sources():
    """custody_secret_files entries: a path (values whose names look secret), or
    {path = "...", names = [...]} for exactly those names."""
    for entry in config.settings().get("custody_secret_files", []):
        if isinstance(entry, str):
            yield entry, None
        elif isinstance(entry, dict) and entry.get("path"):
            yield entry["path"], set(entry.get("names") or [])
        else:
            raise SystemExit(f"custody_secret_files: expected a path or {{path, names}}, got {entry!r}")


def known_secrets():
    """{value: name} for what must never travel: provider keys and custody_secret_files.
    Only names that look secret count, so a path or URL beside them never blocks an upload."""
    known = {}
    for name in config.KEYS:
        value = config.secret(name, required=False)
        if value:
            known[value] = name
    for path, names in _secret_sources():
        try:
            values = read_values(path)
        except FileNotFoundError:
            print(f"custody: secret file {path} is missing; its values can't be checked", flush=True)
            continue
        for name, value in values.items():
            if len(value) >= 8 and (name in names if names is not None else bool(SECRET_NAME.search(name))):
                known[value] = name
    return known


def _name_is_sensitive(path):
    base = path.rsplit("/", 1)[-1]
    return not base.endswith(PLACEHOLDER_SUFFIXES) and any(r.fullmatch(base) for r in NAME_RULES)


def _scan_content(data, known):
    """Rule names only: what matched is never kept."""
    out = [f"known-secret:{name}" for value, name in known.items() if value.encode() in data]
    if b"\0" not in data[:8192]:
        out += [name for name, rule in CONTENT_RULES.items() if rule.search(data)]
    return out


def take_manifest(tar_cmd, known=None):
    """Read the same tar stream an upload would send: exclude rules and links behave
    exactly as they will on the wire. Returns (manifest, findings)."""
    known = known_secrets() if known is None else known
    entries, findings = [], []
    summary = {"files": 0, "bytes": 0, "content_scanned": 0, "content_not_scanned": 0, "opaque_archives": 0}
    proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as stream:
            for m in stream:
                path = m.name.removeprefix("./")
                if not path or path == ".":
                    continue
                entry = {"path": path, "type": "file" if m.isreg() else "dir" if m.isdir() else "link" if m.issym() or m.islnk() else "other",
                         "size": m.size if m.isreg() else 0, "mode": oct(m.mode & 0o7777)}
                if m.issym() or m.islnk():
                    entry["target"] = m.linkname
                hits = []
                if m.isreg():
                    summary["files"] += 1
                    summary["bytes"] += m.size
                    digest, head, source = hashlib.sha256(), [], stream.extractfile(m)
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        if m.size <= SCAN_MAX_BYTES:
                            head.append(chunk)
                    entry["sha256"] = digest.hexdigest()
                    if m.size <= SCAN_MAX_BYTES:
                        summary["content_scanned"] += 1
                        hits += _scan_content(b"".join(head), known)
                    else:
                        summary["content_not_scanned"] += 1
                    if path.lower().endswith(ARCHIVES):
                        summary["opaque_archives"] += 1
                if m.type != tarfile.DIRTYPE and _name_is_sensitive(path):
                    hits.append("sensitive-name")
                findings += [{"path": path, "rule": rule} for rule in hits]
                entries.append(entry)
        # tarfile stops at the end-of-archive marker; tar may still be writing padding,
        # and closing the pipe under it would kill it and look like a failed manifest.
        while proc.stdout.read(65536):
            pass
    except tarfile.TarError as e:
        unreadable = str(e)
    else:
        unreadable = None
    finally:
        proc.stdout.close()  # first: on an error path tar may be blocked writing to it
        err = proc.stderr.read().decode(errors="replace")
        proc.stderr.close()
        code = proc.wait()
    if unreadable:
        raise CustodyError(f"could not read the upload stream: {unreadable}; {err[-300:]}")
    if code:
        raise CustodyError(f"tar exited {code} while taking the manifest: {err[-300:]}")
    entries.sort(key=lambda e: e["path"])
    body = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return {"id": hashlib.sha256(body.encode()).hexdigest()[:32], "entries": entries, **summary}, findings


def _store(manifest):
    path = _home() / "manifests" / f"{manifest['id']}.json.gz"
    if not path.exists():
        fd = os.open(path.with_suffix(f".tmp{os.getpid()}"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as f:
            f.write(json.dumps(manifest).encode())
        os.replace(path.with_suffix(f".tmp{os.getpid()}"), path)
    return path


def load_manifest(manifest_id):
    with gzip.open(_home() / "manifests" / f"{manifest_id}.json.gz") as f:
        return json.loads(f.read())


def policy_for(job=None):
    s = config.settings()
    own = getattr(job, "custody", None) or {}
    return {"on_sensitive": own.get("on_sensitive", s.get("custody_on_sensitive", "warn")), "allow": list(own.get("allow", []))}


def judge(findings, policy):
    """(findings marked allowed or not, why refused or None). A known secret is always
    refused: to place one deliberately, use Remote.put_secrets, which records it."""
    marked = [{**f, **({"allowed": True} if any(fnmatch.fnmatch(f["path"], a) for a in policy["allow"]) else {})} for f in findings]
    known = [f for f in marked if f["rule"].startswith("known-secret:")]
    if known:
        return marked, "holds a known secret: " + ", ".join(sorted({f"{f['path']} ({f['rule'].split(':', 1)[1]})" for f in known})[:5])
    live = [f for f in marked if not f.get("allowed")]
    if live and policy["on_sensitive"] == "refuse":
        return marked, "looks sensitive: " + ", ".join(sorted({f"{f['path']} ({f['rule']})" for f in live})[:5])
    return marked, None


def _say(src, marked):
    live = [f for f in marked if not f.get("allowed")]
    if live:
        print(f"custody: {src}: {len(live)} sensitive finding(s), recorded: "
              + ", ".join(sorted({f"{f['path']} ({f['rule']})" for f in live})[:5]), flush=True)


def _where(remote):
    return {"host": remote.host, "port": remote.port, **getattr(remote, "custody", {}).get("ids", {})}


@contextmanager
def upload(remote, src, dst, exclude=(), tar_cmd=None):
    """Around an upload: manifest and receipt before any byte moves, outcome after."""
    context = getattr(remote, "custody", {})
    manifest, findings = take_manifest(tar_cmd)
    marked, refused = judge(findings, context.get("policy") or policy_for())
    _store(manifest)
    ids = _where(remote)
    job = context.get("job")
    upload_id = ledger.new_id("u")
    payload = {"upload": upload_id, **ids, "src": str(src), "dst": dst, "exclude": list(exclude), "manifest": manifest["id"],
               **{k: manifest[k] for k in ("files", "bytes", "content_scanned", "content_not_scanned", "opaque_archives")},
               "findings": marked[:MAX_RECEIPT_FINDINGS], "findings_total": len(marked)}
    if refused:
        ledger.write("custody.upload", {**payload, "refused": refused}, job=job)
        raise Refused(f"upload of {src} refused, nothing sent: {refused}")
    ledger.write("custody.upload", payload, job=job)
    _say(src, marked)
    try:
        yield manifest
    except BaseException as e:
        ledger.write("custody.uploaded", {"upload": upload_id, **ids, "ok": False, "error": type(e).__name__}, job=job)
        raise
    ledger.write("custody.uploaded", {"upload": upload_id, **ids, "ok": True}, job=job)


def check_job(job):
    """Before renting: a refusal shouldn't cost a machine."""
    from gipper.ssh import tar_command
    policy, known = policy_for(job), known_secrets()
    for t in job.inputs:
        manifest, findings = take_manifest(tar_command(t.src, t.exclude), known)
        marked, refused = judge(findings, policy)
        if refused:
            raise Refused(f"input {t.src} refused before renting: {refused}")
        _say(t.src, marked)
        if manifest["opaque_archives"]:
            print(f"custody: {t.src}: {manifest['opaque_archives']} archive(s) sent without looking inside", flush=True)


def attach(remote, provider, pod, job_id, job, offer=None, info=None):
    """Tie a Remote to its rental and record what the machine is."""
    info = info or {}
    machine = info.get("machine") or (offer.ref.get("machine") if offer else None)
    ids = {"provider": provider, "pod": str(pod), "machine": machine}
    remote.custody = {"ids": ids, "job": job_id, "policy": policy_for(job)}
    text = (job.setup + "\n" + job.command).encode()
    pub = Path(str(config.KEY_DIR / "id_ed25519.pub"))
    ledger.write("custody.machine", {
        **ids, "host": remote.host, "port": remote.port, "user": remote.user, "name": job.name, "workload": job.workload,
        "sensitivity": job.sensitivity, "gpu": info.get("gpu"), "where": offer.where if offer else None,
        "country": offer.country if offer else None, "image": job.image or "(provider default)", "ports": info.get("ports") or job.ports,
        "volume": job.volume, "env_names": sorted(job.env), "job_file": str(job.path) if job.path else None,
        "job_sha256": hashlib.sha256(job.path.read_bytes()).hexdigest() if job.path and job.path.exists() else None,
        "command_sha256": hashlib.sha256(text).hexdigest(),
        "ssh_public_key_sha256": hashlib.sha256(pub.read_bytes()).hexdigest() if pub.exists() else None,
    }, job=job_id, refs={"name": job.name})


def record_secrets(names_and_values, *, ids, path=None, how="", confidence="direct", note="", at=None, job=None):
    """Names and fingerprints only. A value of None means the name is known but the value isn't to hand."""
    return ledger.write("custody.secret", {
        **ids, "secrets": [{"name": n, "fingerprint": fingerprint(v) if v else None} for n, v in sorted(names_and_values.items())],
        "path": path, "how": how, "confidence": confidence, "note": note, "occurred_at": at}, job=job)


def declare(*, provider, pod, secrets=(), from_env=None, path=None, files=(), tunnel=None, note="", how="", confidence="direct",
            machine=None, host=None, port=None, at=None, job=None):
    """Record what reached a machine by a route gipper didn't see."""
    from gipper.ssh import tar_command
    ids = {"provider": provider, "pod": str(pod), "machine": machine, **({"host": host} if host else {}), **({"port": int(port)} if port else {})}
    values = read_values(from_env) if from_env else {}
    missing = [n for n in secrets if from_env and n not in values]
    if missing:
        raise SystemExit(f"{from_env}: no {', '.join(missing)}")
    written = []
    if secrets:
        written.append(record_secrets({n: values.get(n) for n in secrets}, ids=ids, path=path, how=how, confidence=confidence,
                                      note=note, at=at, job=job))
    for local in files:
        manifest, findings = take_manifest(tar_command(local))
        _store(manifest)
        marked, _ = judge(findings, {"on_sensitive": "warn", "allow": []})
        written.append(ledger.write("custody.upload", {
            "upload": ledger.new_id("u"), **ids, "src": str(local), "dst": path, "exclude": [], "manifest": manifest["id"],
            **{k: manifest[k] for k in ("files", "bytes", "content_scanned", "content_not_scanned", "opaque_archives")},
            "findings": marked[:MAX_RECEIPT_FINDINGS], "findings_total": len(marked), "declared": True, "how": how,
            "confidence": confidence, "occurred_at": at}, job=job))
    if tunnel or (note and not secrets and not files):
        written.append(ledger.write("custody.note", {**ids, "tunnel": tunnel, "note": note, "how": how, "confidence": confidence,
                                                     "occurred_at": at}, job=job))
    return written


def _epoch(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()


def _new_machine(provider, pod):
    return {"provider": provider, "pod": pod, "machine": None, "name": None, "job": None, "host": None, "port": None, "image": None,
            "env_names": [], "first": None, "last": None, "closed": None, "described": False, "uploads": [], "secrets": [], "notes": []}


def collect():
    """{(provider, pod): machine}, including rentals from before the trail existed."""
    machines, by_addr, uploads = {}, {}, {}
    def get(provider, pod):
        return machines.setdefault((provider or "runpod", str(pod)), _new_machine(provider or "runpod", str(pod)))
    def touch(m, rec, payload):
        t = _epoch(payload.get("occurred_at") or rec["ts"])
        m["first"] = min(m["first"], t) if m["first"] else t
        m["last"] = max(m["last"], t) if m["last"] else t
    kinds = {"pod.create", "pod.terminate", "pod.closure_verified", "custody.machine", "custody.upload", "custody.uploaded", "custody.secret", "custody.note"}
    for r in ledger.read(kinds=kinds):
        p, kind = r["payload"], r["kind"]
        if kind == "custody.uploaded":
            if p.get("upload") in uploads:
                uploads[p["upload"]].update(ok=p.get("ok"), error=p.get("error"))
            continue
        addr = (p.get("host"), p.get("port"))
        if not p.get("pod"):
            if kind in ("custody.upload", "custody.secret", "custody.note"):
                m = by_addr.get(addr)
                if m is None:
                    m = get("unknown", f"{addr[0]}:{addr[1]}")
            else:
                continue
        else:
            m = get(p.get("provider"), p["pod"])
        touch(m, r, p)
        if p.get("machine"):
            m["machine"] = p["machine"]
        m["job"] = m["job"] or r.get("job")
        if p.get("host"):
            m["host"], m["port"] = p["host"], p.get("port")
            by_addr[addr] = m
        if kind == "pod.create":
            m["name"] = m["name"] or r["refs"].get("name")
        elif kind == "custody.machine":
            m.update(described=True, name=p.get("name") or m["name"], image=p.get("image"), env_names=p.get("env_names", []),
                     workload=p.get("workload"), job_file=p.get("job_file"), where=p.get("where"), gpu=p.get("gpu"))
        elif kind in ("pod.terminate", "pod.closure_verified"):
            m["closed"] = r["ts"]
        elif kind == "custody.upload":
            entry = {k: p.get(k) for k in ("upload", "src", "dst", "manifest", "files", "bytes", "content_not_scanned", "opaque_archives",
                                           "findings", "findings_total", "refused", "declared", "how", "confidence")}
            entry.update(at=p.get("occurred_at") or r["ts"], ok=None)
            uploads[p["upload"]] = entry
            m["uploads"].append(entry)
        elif kind == "custody.secret":
            m["secrets"].append({"at": p.get("occurred_at") or r["ts"], "secrets": p.get("secrets", []), "path": p.get("path"), "how": p.get("how"),
                                 "confidence": p.get("confidence"), "note": p.get("note")})
        else:
            m["notes"].append({"at": p.get("occurred_at") or r["ts"], "tunnel": p.get("tunnel"), "note": p.get("note"), "confidence": p.get("confidence")})
    return machines


def report(*, provider=None, pod=None, machine=None, since=None, until=None, check_providers=False):
    since_t, until_t = _epoch(since), _epoch(until)
    rows = []
    for m in collect().values():
        if (provider and m["provider"] != provider) or (pod and m["pod"] != pod) or (machine and m["machine"] != machine):
            continue
        if (since_t and (m["last"] or 0) < since_t) or (until_t and (m["first"] or 0) > until_t):
            continue
        rows.append(m)
    rows.sort(key=lambda m: m["first"] or 0)
    rotate, review, gaps = {}, [], []
    for m in rows:
        label = f"{m['provider']}:{m['pod']}"
        for placed in m["secrets"]:   # a write that failed may still have arrived: it counts
            for s in placed["secrets"]:
                r = rotate.setdefault(s["name"], {"name": s["name"], "machines": [], "values": set(), "unknown_values": 0, "confidence": set()})
                if label not in r["machines"]:
                    r["machines"].append(label)
                r["confidence"].add(placed.get("confidence") or "direct")
                if s.get("fingerprint"):
                    r["values"].add(s["fingerprint"])
                else:
                    r["unknown_values"] += 1
        tracked = False
        for u in m["uploads"]:
            tracked = True
            if u.get("refused"):
                continue
            for f in u.get("findings") or []:
                if not f.get("allowed"):
                    review.append({"machine": label, "src": u["src"], "path": f["path"], "rule": f["rule"], "manifest": u["manifest"]})
            if u.get("ok") is None and not u.get("declared"):
                gaps.append(f"{label}: upload of {u['src']} has no recorded outcome; assume it arrived")
            if u.get("content_not_scanned"):
                gaps.append(f"{label}: {u['src']}: {u['content_not_scanned']} file(s) too large to scan, hashed only")
            if u.get("opaque_archives"):
                gaps.append(f"{label}: {u['src']}: {u['opaque_archives']} archive(s) not looked inside")
        if not m["described"] and not tracked and not m["secrets"] and not m["notes"]:
            gaps.append(f"{label}: rented with no custody trail (before 0.4, or outside gipper): contents unknown")
        if m["provider"] == "unknown":
            gaps.append(f"{label}: material sent to an address no rental in the ledger matches")
    if check_providers:
        gaps += _unknown_to_ledger(since_t, {(m["provider"], m["pod"]) for m in collect().values()}, provider)
    for r in rotate.values():
        r["values"], r["confidence"] = sorted(r["values"]), sorted(r["confidence"])
    return {"machines": rows, "rotate": sorted(rotate.values(), key=lambda r: r["name"]), "review": review, "gaps": gaps,
            "limits": "Records what passed through gipper or was declared. Commands run on a machine, and anything a job fetched itself, are not recorded."}


def _unknown_to_ledger(since, known, provider=None):
    """Machines the provider billed that the ledger has never heard of: the trail can't cover them."""
    from gipper.providers import load
    out = []
    start = since or (datetime.now(timezone.utc).timestamp() - 90 * 86400)
    for name in [provider] if provider else config.settings()["providers"]:
        try:
            p = load(name)
            if not p.has_key():
                out.append(f"{name}: no key, so its bills weren't checked")
                continue
            for iid in sorted((p.billed(start) or {}), key=str):
                if (name, str(iid)) not in known:
                    out.append(f"{name}:{iid}: billed by the provider but unknown to gipper: declare what reached it")
        except Exception as e:
            out.append(f"{name}: could not read bills: {config.redact(str(e))[:200]}")
    return out


def find(target):
    """Which machines received this? target: a local file, a sha256, or part of a path."""
    path = Path(target).expanduser()
    digest = None
    if path.is_file():
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        digest = h.hexdigest()
    elif re.fullmatch(r"[0-9a-f]{64}", target):
        digest = target
    hits = []
    for m in collect().values():
        for u in m["uploads"]:
            if u.get("refused"):
                continue
            try:
                entries = load_manifest(u["manifest"])["entries"]
            except FileNotFoundError:
                continue
            for e in entries:
                if (digest and e.get("sha256") == digest) or (not digest and target in e["path"]):
                    hits.append({"machine": f"{m['provider']}:{m['pod']}", "at": u["at"], "src": u["src"], "dst": u["dst"], "path": e["path"],
                                 "sha256": e.get("sha256"), "arrived": u.get("ok")})
    return hits


def check(env_file):
    """For each value in a file: has this exact value been placed on a machine? After
    rotating, every line should say no."""
    placed = {}
    for m in collect().values():
        for p in m["secrets"]:
            for s in p["secrets"]:
                if s.get("fingerprint"):
                    placed.setdefault(s["fingerprint"], []).append(f"{m['provider']}:{m['pod']}")
    return [{"name": n, "exposed_on": sorted(set(placed.get(fingerprint(v), [])))} for n, v in sorted(read_values(env_file).items())]


def _short(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M") if t else "?"


def show(rep):
    """The report as text. Names, paths and hashes; never values."""
    rows = rep["machines"]
    print(f"{len(rows)} machine(s)" + (f", {_short(rows[0]['first'])} to {_short(max(m['last'] or 0 for m in rows))} UTC" if rows else ""))
    print("\nROTATE (placed on these machines)")
    for r in rep["rotate"]:
        extra = f", {len(r['values'])} distinct value(s)" if r["values"] else ""
        unknown = " (some values not fingerprinted)" if r["unknown_values"] else ""
        print(f"  {r['name']:28s} {len(r['machines'])} machine(s){extra}{unknown} [{'/'.join(r['confidence'])}]: {', '.join(r['machines'][:8])}")
    if not rep["rotate"]:
        print("  none declared")
    print("\nREVIEW (uploaded files that look sensitive)")
    for f in rep["review"][:200]:
        print(f"  {f['machine']:32s} {f['rule']:22s} {f['path']}  (from {f['src']})")
    if not rep["review"]:
        print("  none found")
    print("\nMACHINES")
    for m in rows:
        files, nbytes = sum(u.get("files") or 0 for u in m["uploads"]), sum(u.get("bytes") or 0 for u in m["uploads"])
        print(f"  {m['provider']}:{m['pod']}  machine {m['machine'] or '?'}  {m['name'] or '?'}  {_short(m['first'])}  "
              f"{len(m['uploads'])} upload(s), {files} files, {nbytes / 1e9:.2f} GB"
              + (f"  env: {','.join(m['env_names'])}" if m["env_names"] else ""))
        for n in m["notes"]:
            print(f"    note: {n.get('tunnel') or ''} {n.get('note') or ''}".rstrip())
    print("\nGAPS (what this report can't vouch for)")
    for g in rep["gaps"][:200]:
        print(f"  {g}")
    if not rep["gaps"]:
        print("  none known")
    print(f"\n{rep['limits']}")
