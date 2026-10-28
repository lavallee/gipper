"""`gipper pod run JOB`: rent a GPU from whichever market serves the job best, run
the job, bring the results home, and always give the GPU back.

Lifecycle (every step receipted):
  shop (every provider with a key; regions, ports, memory, [placement] floors)
  -> rank by whole-run score (gipper.costs, using gipper.history) -> caps
  -> rent the best offer (next one if it's gone) -> wait for ssh -> preflight the GPU
  and CUDA, optionally probe the network (replace a bad host, up to 3)
  -> upload inputs (timed: memory learns real transfer speeds)
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
from gipper.placement import input_gb
from gipper.http import ProviderError
from gipper.providers import BadHost, Unavailable, load
from gipper.ssh import ensure_key

BOOTSTRAP = Path(__file__).with_name("bootstrap.sh")


class CleanupPending(RuntimeError):
    pass


class Unsuitable(BadHost):
    """The host works but doesn't suit this job (compute capability, a runtime the
    job pins). Rejected and replaced like any bad host, but not held against it in
    memory. Client preflight wrappers should raise this for job-specific checks."""


class Capped(Exception):
    pass


def _now():
    return time.time()


def plan(job, *, provider=None, gpu=None, throughput=None):
    """(offers worth trying best first, hourly ceiling, notes). Offers above the job's
    own max_usd_per_h, or whose expected cost exceeds its budget, are left out."""
    offers, notes = market.shop(job, names=[provider] if provider else None, throughput=throughput)
    if gpu:
        offers = [o for o in offers if market.matches(gpu, o.gpu)]
    if job.max_usd_per_h:
        offers = [o for o in offers if o.usd_h <= job.max_usd_per_h]
    over = [o for o in offers if o.estimate.get("usd", 0) > job.max_usd]
    if over and len(over) < len(offers):
        notes.append(f"{len(over)} offer(s) left out: expected cost above the job's ${job.max_usd} budget")
        offers = [o for o in offers if o not in over]
    if not offers:
        where = f" in {', '.join(job.regions)}" if job.regions else ""
        udp = " with UDP" if market.wants_udp(job.ports) else ""
        cap = f" at <= ${job.max_usd_per_h}/h" if job.max_usd_per_h else ""
        raise SystemExit(f"no GPU{' ' + gpu if gpu else ''} with >= {job.min_mem_gb} GB{where}{udp}{cap} is on offer right now"
                         + "".join(f"\n  {n}" for n in notes))
    return offers, job.max_usd_per_h or max(o.usd_h for o in offers), notes


def estimate(job, gpu_price=None, throughput=None, setup_s=420, *, offer=None):
    """The whole-run estimate for an offer (gipper.costs); without one, 0.1's rough
    guess from the hourly price."""
    if offer is not None:
        if not offer.estimate or throughput:
            from gipper.costs import estimate as whole_run
            from gipper.history import History
            offer.estimate = whole_run(job, offer, History.load(), throughput=throughput)
        return {**offer.estimate, "basis_summary": _basis(offer.estimate)}
    wall = (setup_s + job.units_count / throughput) if throughput and job.units_count else min(job.max_hours * 3600, setup_s + 1800)
    return {"wall_s": round(wall), "usd": round(wall / 3600 * gpu_price, 3), "basis": "guess", "basis_summary": "guess"}


def _basis(e):
    return ", ".join(f"{k} {v}" for k, v in e.get("basis", {}).items() if v not in ("default", "rate"))  or "defaults"


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


def requirements(remote, job):
    """The job's [placement] floors, checked on the machine itself (a provider may
    not advertise them, or advertise them wrong)."""
    want = job.placement.min_compute_capability
    if not want:
        return {}
    r = remote.run("nvidia-smi --query-gpu=compute_cap --format=csv,noheader", check=False, timeout=60)
    try:
        have = float(r.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        raise BadHost(f"could not read the GPU's compute capability: {r.stderr.strip()[-200:]}") from None
    if have < want:
        raise Unsuitable(f"GPU compute capability {have} is below the job's {want}")
    return {"compute_capability": have}


def _bytes(path):
    p = Path(path)
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink()) if p.exists() else 0


def run(job, *, dry_run=False, log_dir=None, gpu_override=None, provider=None, throughput=None):
    s = settings()
    run_id = ledger.new_id("job")
    if job.sensitivity == "owned-only":
        raise SystemExit(f"{job.name}: sensitivity = owned-only; not running on a rented GPU")
    offers, ceiling, notes = plan(job, provider=provider, gpu=gpu_override, throughput=throughput)
    for n in notes:
        print(f"note: {n}")
    best = offers[0]
    est = estimate(job, best.usd_h, throughput, offer=best)
    est.update(provider=best.provider, gpu=best.gpu, where=best.where, usd_per_h=best.usd_h, ceiling=ceiling,
               machine=best.ref.get("machine"), cap_usd=job.max_usd, max_hours=job.max_hours)
    ledger.write("estimate", est, job=run_id, refs={"name": job.name, "workload": job.workload})
    spent_today = ledger.spend_since(ledger.today_start())
    print(f"{job.name}: {best.label} ({best.where}) at ${best.usd_h:.2f}/h; ~{est['wall_s'] / 60:.0f} min, ~${est['usd']:.3f}, "
          f"{est.get('p_fail', 0):.0%} host risk ({est.get('basis_summary') or est.get('basis', '')}); cap ${job.max_usd}, {job.max_hours} h; "
          f"today so far ${spent_today:.2f} of ${s['daily_cap_usd']}")
    if spent_today + min(est["usd"], job.max_usd) > s["daily_cap_usd"]:
        raise SystemExit(f"refusing: today's spend ${spent_today:.2f} + this job would exceed the daily cap ${s['daily_cap_usd']}")
    if dry_run:
        for o in offers[1:6]:
            e = o.estimate
            print(f"  then {o.label} ({o.where}) ${o.usd_h:.2f}/h; ~{e.get('wall_s', 0) / 60:.0f} min, "
                  f"~${e.get('usd', 0):.3f}, {e.get('p_fail', 0):.0%} risk")
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
                                        "machine": offer.ref.get("machine"), "country": offer.country,
                                        "usd_per_h": inst["usd_h"],
                                        "deadline": inst["t"] + job.max_hours * 3600 + 600},
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
                from gipper.latency import HERE, connect_ms
                ledger.write("host.rtt", {"provider": prov.name, "pod": made["id"], "machine": info.get("machine"),
                                          "origin": HERE, "ms": connect_ms(remote.host, remote.port),
                                          "basis": "fastest of 5 TCP connects to the ssh port"}, job=run_id)
                health = preflight(remote)
                ledger.write("host.ok", {"pod": made["id"], **health}, job=run_id)
                from gipper.cuda_probe import check as cuda_check, CudaProbeError
                try:
                    cuda_health = cuda_check(remote, job.env)
                except CudaProbeError as error:
                    ledger.write("host.cuda_probe", {"provider": prov.name, "pod": made["id"], **error.receipt}, job=run_id)
                    raise
                ledger.write("host.cuda_probe", {"provider": prov.name, "pod": made["id"], **cuda_health}, job=run_id)
                met = requirements(remote, job)
                if met:
                    ledger.write("host.requirements", {"provider": prov.name, "pod": made["id"], **met}, job=run_id)
                if job.placement.probe_transfer_mb:
                    from gipper.network_probe import probe
                    network = probe(remote, job.placement)
                    ledger.write("host.network_probe", {"provider": prov.name, "pod": made["id"],
                                 "machine": info.get("machine"), **network}, job=run_id)
                    print(f"network probe from {network['origin_hostname']}: upload {network['upload_mbps']} Mbps, "
                          f"download {network['download_mbps']} Mbps ({network['status']}; includes SSH overhead)", flush=True)
                    if network['status'] != 'ok':
                        raise BadHost(network.get('error', 'network probe failed'))
                break
            except BadHost as e:
                print(f"rejecting host {info.get('machine') or made['id']}: {e}")
                ledger.write("host.reject", {"provider": prov.name, "pod": made["id"], "machine": info.get("machine"),
                                             "why": str(e), "kind": "unsuitable" if isinstance(e, Unsuitable) else None},
                             job=run_id)
                if not _terminate(inst, run_id, job):
                    raise CleanupPending("prior instance closure unverified; refusing replacement rental")
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
        t_up, up_bytes = _now(), round(input_gb(job) * 1e9)
        for t in job.inputs:
            remote.put(t.src, f"{jobdir}/{t.dst}", t.exclude)
        _transfer(run_id, prov.name, inst["id"], info, "up", up_bytes, _now() - t_up)
        # where the outside world reaches the job's ports (e.g. to advertise in ICE candidates)
        env = {"GIPPER_PUBLIC_IP": remote.host, **{f"GIPPER_PORT_{k.replace('/', '_').upper()}": str(v["port"])
                                                     for k, v in ports.items()}, **job.env, **cuda_health["environment_override"]}
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
        t_run = _now()
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
        compute_s = round(_now() - t_run)
        print("-" * 60 + f"\nexit {code}; collecting outputs")
        t_down, before = _now(), sum(_bytes(t.dst) for t in job.outputs)
        for t in job.outputs:
            remote.get(f"{jobdir}/{t.src}", t.dst)
        if not job.checkpoint_src:   # with checkpoints, the final pull re-sends files already counted
            _transfer(run_id, prov.name, inst["id"], info, "down", sum(_bytes(t.dst) for t in job.outputs) - before,
                      _now() - t_down)
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
        try:
            signal.signal(signal.SIGINT, old)
            if inst:
                try:
                    if inst.get("termination_pending") or not _terminate(inst, run_id, job):
                        outcome = "termination_pending"
                except Exception as cleanup_error:
                    outcome = "termination_pending"
                    ledger.write("pod.termination_pending", {"provider": inst["provider"].name, "pod": inst["id"],
                                 "reason": str(cleanup_error)[:500]}, job=run_id)
        finally:
            try:
                ledger.write("run.end", {"outcome": outcome, "units": job.units_count, "units_name": job.units_name,
                                         "compute_s": locals().get("compute_s")},
                             job=run_id, refs={"name": job.name, "workload": job.workload})
            finally:
                log.close()
    return run_id, outcome


def _transfer(run_id, provider, pod, info, direction, nbytes, seconds):
    """Timed real transfer: what gipper.history learns link speeds from."""
    if nbytes > 0 and seconds > 0:
        ledger.write("transfer", {"provider": provider, "pod": pod, "machine": info.get("machine"),
                                  "direction": direction, "bytes": nbytes, "seconds": round(seconds, 2),
                                  "mbps": round(nbytes * 8 / 1e6 / seconds, 1)}, job=run_id)


def _terminate(inst, run_id, job):
    from gipper.lifecycle import terminate
    if inst.get("closure_verified"):
        return True
    result = terminate(inst["provider"], inst["id"], job_id=run_id, started=inst["t"], usd_h=inst["usd_h"],
                       refs={"name": job.name, "workload": job.workload})
    inst["closure_verified"] = result
    inst["termination_pending"] = not result
    return result


def _salvage(remote, job, jobdir):
    """Bring back whatever outputs exist before the machine goes."""
    if not remote or not jobdir:
        return
    for t in job.outputs:
        try:
            remote.get(f"{jobdir}/{t.src}", t.dst)
        except Exception as e:  # salvage is best effort; teardown must still run
            print(f"salvage of {t.src} failed: {e}")
