"""`gipper pod run JOB`: rent a GPU from whichever market serves the job best, run
the job, bring the results home, and always give the GPU back.

Lifecycle (every step receipted):
  shop (every provider with a key; regions, ports, memory) -> estimate -> caps
  -> rent the best offer (next one if it's gone) -> wait for ssh -> preflight the GPU
  (replace a busy or broken host, up to 3) -> upload inputs
  -> start the job detached (nohup, under `timeout max_hours`)
  -> stream its log, sync checkpoints, enforce max_usd / max_hours
  -> download outputs -> terminate (in `finally`, whatever happened) -> spend

The job runs detached so a dropped ssh connection doesn't kill it; the in-machine
`timeout` bounds it even if this client dies, and the reaper (`gipper pod reap`)
ends any gipper machine the ledger says should be gone, on every provider.
"""
import shlex
import signal
import sys
import time
from pathlib import Path

from gipper import ledger, market
from gipper.config import DATA, settings
from gipper.http import ProviderError
from gipper.providers import BadHost, Unavailable, load
from gipper.ssh import ensure_key

BOOTSTRAP = Path(__file__).with_name("bootstrap.sh")


class Capped(Exception):
    pass


def _now():
    return time.time()


def plan(job, *, provider=None, gpu=None):
    """Offers worth trying, best first, all at or under the price ceiling."""
    offers, notes = market.shop(job, names=[provider] if provider else None)
    if gpu:
        offers = [o for o in offers if market.matches(gpu, o.gpu)]
    if not offers:
        where = f" in {', '.join(job.regions)}" if job.regions else ""
        udp = " with UDP" if market.wants_udp(job.ports) else ""
        raise SystemExit(f"no GPU{' ' + gpu if gpu else ''} with >= {job.min_mem_gb} GB{where}{udp} is on offer right now"
                         + "".join(f"\n  {n}" for n in notes))
    ceiling = market.ceiling(offers, job.max_usd_per_h, job.prefer)
    return [o for o in offers if o.usd_h <= ceiling] or offers[:1], ceiling, notes


def estimate(job, gpu_price, throughput=None, setup_s=420):
    """Wall seconds and dollars; throughput = units per second if known."""
    if throughput and job.units_count:
        wall = setup_s + job.units_count / throughput
        basis = "throughput"
    else:
        wall = min(job.max_hours * 3600, setup_s + 1800)
        basis = "guess"
    return {"wall_s": round(wall), "usd": round(wall / 3600 * gpu_price, 3), "basis": basis}


def _rent(queue, job, pubkey, name):
    """Take offers in order until one is rented: (provider, offer, created)."""
    while queue:
        o = queue.pop(0)
        p = load(o.provider)
        try:
            return p, o, p.create(o, job, pubkey, name)
        except Unavailable as e:
            print(f"{o.label} ({o.where}) not available: {str(e)[:120]}")
    raise Unavailable("nothing at or under the price ceiling could be rented")


def preflight(remote):
    """Reject a host whose GPU is already busy or broken before any upload: the
    community hosts seen on 2026-10-20 included one at 100% use and 1 GB held at
    start, and two whose first CUDA call failed."""
    r = remote.run("nvidia-smi --query-gpu=utilization.gpu,memory.used,compute_mode --format=csv,noheader,nounits; "
                   "nvidia-smi --query-compute-apps=pid --format=csv,noheader | wc -l", check=False, timeout=60)
    if r.returncode:
        raise BadHost(f"nvidia-smi failed: {r.stderr.strip()[-200:]}")
    lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
    util, mem, mode = [x.strip() for x in lines[0].split(",")]
    procs = int(lines[-1].strip() or 0)
    if int(util) > 30 or int(mem) > 800 or procs > 0:
        raise BadHost(f"GPU busy at start: {util}% used, {mem} MiB held, {procs} processes, mode {mode}")
    return {"util": int(util), "mem_mib": int(mem), "mode": mode}


def run(job, *, dry_run=False, log_dir=None, gpu_override=None, provider=None, throughput=None):
    s = settings()
    run_id = ledger.new_id("job")
    if job.sensitivity == "owned-only":
        raise SystemExit(f"{job.name}: sensitivity = owned-only; not running on a rented GPU")
    offers, ceiling, notes = plan(job, provider=provider, gpu=gpu_override)
    for n in notes:
        print(f"note: {n}")
    best = offers[0]
    est = estimate(job, best.usd_h, throughput)
    est.update(provider=best.provider, gpu=best.gpu, where=best.where, usd_per_h=best.usd_h, ceiling=ceiling,
               cap_usd=job.max_usd, max_hours=job.max_hours)
    ledger.write("estimate", est, job=run_id, refs={"name": job.name, "workload": job.workload})
    spent_today = ledger.spend_since(ledger.today_start())
    print(f"{job.name}: {best.label} ({best.where}) at ${best.usd_h:.2f}/h; estimate {est['wall_s'] / 60:.0f} min, "
          f"${est['usd']} ({est['basis']}); cap ${job.max_usd}, {job.max_hours} h; "
          f"today so far ${spent_today:.2f} of ${s['daily_cap_usd']}")
    if spent_today + min(est["usd"], job.max_usd) > s["daily_cap_usd"]:
        raise SystemExit(f"refusing: today's spend ${spent_today:.2f} + this job would exceed the daily cap ${s['daily_cap_usd']}")
    if dry_run:
        for o in offers[1:6]:
            print(f"  then {o.label} ({o.where}) ${o.usd_h:.2f}/h")
        return run_id, None

    log_dir = Path(log_dir or DATA / "jobs" / run_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "run.log", "ab")
    pubkey = ensure_key()
    name = f"gipper-{job.name}-{run_id[-6:]}"[:50]
    inst, outcome = None, "error"
    stop = {"why": None}
    old = signal.signal(signal.SIGINT, lambda *a: stop.update(why="interrupted"))
    signal.signal(signal.SIGTERM, lambda *a: stop.update(why="terminated"))
    try:
        queue = list(offers)
        for attempt in range(3):
            prov, offer, made = _rent(queue, job, pubkey, name)
            inst = {"provider": prov, "id": made["id"], "t": _now(), "usd_h": made["usd_h"] or offer.usd_h}
            ledger.write("pod.create", {"provider": prov.name, "pod": made["id"], "gpu": made["gpu"], "where": offer.where,
                                        "usd_per_h": inst["usd_h"], "deadline": inst["t"] + job.max_hours * 3600 + 600},
                         job=run_id, refs={"name": job.name})
            print(f"{prov.name} {made['id']} created: {made['gpu']}, {offer.where}, ${inst['usd_h']:.3f}/h; waiting for ssh")
            remote, info = None, {}
            try:
                remote, info = prov.wait_ready(made["id"], job)
                if not remote.wait(600):
                    raise BadHost("ssh never came up")
                ledger.write("pod.ready", {"provider": prov.name, "pod": made["id"], "after_s": round(_now() - inst["t"]),
                                           "machine": info.get("machine"), "gpu": info.get("gpu"),
                                           "ports": info.get("ports")}, job=run_id)
                health = preflight(remote)
                ledger.write("host.ok", {"pod": made["id"], **health}, job=run_id)
                break
            except BadHost as e:
                print(f"rejecting host {info.get('machine') or made['id']}: {e}")
                ledger.write("host.reject", {"provider": prov.name, "pod": made["id"], "machine": info.get("machine"),
                                             "why": str(e)}, job=run_id)
                _terminate(inst, run_id, job)
                inst, remote = None, None
        else:
            raise RuntimeError("three hosts in a row were busy or broken")
        jobdir = f"{remote.home}/job"
        ports = info.get("ports") or {}
        for k, v in ports.items():
            print(f"port {k} -> {v['host']}:{v['port']}")
        print(f"ssh ready after {_now() - inst['t']:.0f}s; uploading inputs")

        remote.run(f"mkdir -p {jobdir}")
        remote.put(BOOTSTRAP, f"{jobdir}/.gipper/bootstrap.sh")
        for t in job.inputs:
            remote.put(t.src, f"{jobdir}/{t.dst}", t.exclude)
        # where the outside world reaches the job's ports (e.g. to advertise in ICE candidates)
        env = {"GIPPER_PUBLIC_IP": remote.host, **{f"GIPPER_PORT_{k.replace('/', '_').upper()}": str(v["port"])
                                                     for k, v in ports.items()}, **job.env}
        envs = " ".join(f"export {k}={shlex.quote(v)};" for k, v in env.items())
        body = f"source {jobdir}/.gipper/bootstrap.sh; {envs} cd {jobdir}; " + \
               (f"{job.setup} && " if job.setup else "") + f"{job.command}"
        limit = int(job.max_hours * 3600)
        # Only the job is backgrounded, with every stream redirected, so this ssh returns at
        # once (backgrounding the whole `cd && nohup` list kept ssh's stdout open until the
        # job ended). setsid detaches it from the ssh session entirely.
        job_cmd = f"timeout {limit} bash -c {shlex.quote(body)}; echo $? > {jobdir}/.gipper/exit"
        remote.run(f"cd {jobdir}; setsid nohup bash -c {shlex.quote(job_cmd)} > {jobdir}/.gipper/run.log 2>&1 < /dev/null &",
                   timeout=60)
        ledger.write("run.start", {"provider": prov.name, "pod": inst["id"]}, job=run_id)
        print("running; log follows\n" + "-" * 60, flush=True)

        offset, last_ck, code = 0, _now(), None
        while code is None:
            if stop["why"]:
                raise KeyboardInterrupt(stop["why"])
            spent = (_now() - inst["t"]) / 3600 * inst["usd_h"]
            if spent >= job.max_usd or _now() - inst["t"] >= job.max_hours * 3600:
                raise Capped(f"${spent:.2f} / {(_now() - inst['t']) / 60:.0f} min reached the job's cap")
            st = remote.run(f"cat {jobdir}/.gipper/exit 2>/dev/null || echo running", check=False, timeout=60)
            chunk = remote.run(f"tail -c +{offset + 1} {jobdir}/.gipper/run.log", check=False, timeout=60)
            if chunk.returncode == 0 and chunk.stdout:
                sys.stdout.write(chunk.stdout)
                sys.stdout.flush()
                data = chunk.stdout.encode()
                log.write(data)
                offset += len(data)
            status = st.stdout.strip() if st.returncode == 0 else "running"
            if status.lstrip("-").isdigit():
                code = int(status)
                break
            if job.checkpoint_src and _now() - last_ck >= job.checkpoint_every_s:
                for t in job.outputs:
                    if t.src == job.checkpoint_src:
                        remote.get(f"{jobdir}/{t.src}", t.dst)
                ledger.write("checkpoint", {"src": job.checkpoint_src}, job=run_id)
                last_ck = _now()
            time.sleep(10)
        print("-" * 60 + f"\nexit {code}; collecting outputs")
        for t in job.outputs:
            remote.get(f"{jobdir}/{t.src}", t.dst)
        remote.get(f"{jobdir}/.gipper/run.log", str(log_dir / "pod"))
        ledger.write("collect", {"outputs": [t.dst for t in job.outputs]}, job=run_id)
        outcome = "succeeded" if code == 0 else ("timeout" if code == 124 else "failed")
    except Capped as e:
        outcome = "capped"
        ledger.write("cap", {"reason": str(e)}, job=run_id)
        print(f"\nstopping: {e}")
        _salvage(locals().get("remote"), job, locals().get("jobdir"))
    except KeyboardInterrupt as e:
        outcome = "interrupted"
        print(f"\nstopping: {e or 'interrupted'}")
        _salvage(locals().get("remote"), job, locals().get("jobdir"))
    except (ProviderError, RuntimeError, OSError) as e:
        ledger.write("error", {"error": str(e)[:500]}, job=run_id)
        print(f"\nerror: {e}")
        _salvage(locals().get("remote"), job, locals().get("jobdir"))
    finally:
        signal.signal(signal.SIGINT, old)
        if inst:
            _terminate(inst, run_id, job)
        ledger.write("run.end", {"outcome": outcome, "units": job.units_count, "units_name": job.units_name},
                     job=run_id, refs={"name": job.name, "workload": job.workload})
        log.close()
    return run_id, outcome


def _terminate(inst, run_id, job):
    prov, iid = inst["provider"], inst["id"]
    for attempt in range(5):
        try:
            prov.terminate(iid)
            break
        except ProviderError as e:
            print(f"terminate failed ({e}); retrying")
            time.sleep(5)
    secs = _now() - inst["t"]
    usd = secs / 3600 * inst["usd_h"]
    ledger.write("pod.terminate", {"provider": prov.name, "pod": iid}, job=run_id)
    ledger.write("spend", {"provider": prov.name, "pod": iid, "seconds": round(secs), "usd_per_h": inst["usd_h"],
                           "usd": round(usd, 4), "basis": "computed", "accuracy": "estimated"},
                 job=run_id, refs={"name": job.name, "workload": job.workload})
    print(f"{prov.name} {iid} terminated after {secs / 60:.1f} min: ~${usd:.3f}")


def _salvage(remote, job, jobdir):
    """Bring back whatever outputs exist before the machine goes."""
    if not remote or not jobdir:
        return
    for t in job.outputs:
        try:
            remote.get(f"{jobdir}/{t.src}", t.dst)
        except Exception as e:  # salvage is best effort; teardown must still run
            print(f"salvage of {t.src} failed: {e}")
