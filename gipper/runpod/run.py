"""`gipper pod run JOB`: rent a GPU, run the job, bring the results home, and
always give the GPU back.

Lifecycle (every step receipted):
  estimate -> caps check -> create pod -> wait for ssh -> upload inputs
  -> start the job detached on the pod (nohup, under `timeout max_hours`)
  -> stream its log, sync checkpoints, enforce max_usd / max_hours
  -> download outputs -> terminate (in `finally`, whatever happened) -> spend

The job runs detached so a dropped ssh connection doesn't kill it; the in-pod
`timeout` bounds it even if this client dies, and the reaper (`gipper pod reap`)
terminates any gipper pod the ledger says should be gone.
"""
import shlex
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from gipper import ledger
from gipper.config import DATA, settings
from gipper.runpod.api import Api, RunpodError
from gipper.runpod.select import candidates
from gipper.ssh import Remote, ensure_key

JOBDIR = "/root/job"
BOOTSTRAP = Path(__file__).with_name("bootstrap.sh")


class Capped(Exception):
    pass


def _now():
    return time.time()


def pick_gpu(api, job):
    c = candidates(api.gpu_types(), job.min_mem_gb, job.cloud, job.prefer)
    if not c:
        raise SystemExit(f"no Runpod GPU type with >= {job.min_mem_gb} GB on {job.cloud} cloud has a price right now")
    return c


def estimate(job, gpu_price, throughput=None, setup_s=420):
    """Wall seconds and dollars; throughput = units per second if known."""
    if throughput and job.units_count:
        wall = setup_s + job.units_count / throughput
        basis = "throughput"
    else:
        wall = min(job.max_hours * 3600, setup_s + 1800)
        basis = "guess"
    return {"wall_s": round(wall), "usd": round(wall / 3600 * gpu_price, 3), "basis": basis}


def _create(api, job, gpu_ids, pubkey, run_id, cloud=None):
    cloud = cloud or job.cloud
    s = settings()
    spec = {
        "name": f"gipper-{job.name}-{run_id[-6:]}"[:60],
        "imageName": job.image, "gpuTypeIds": gpu_ids, "gpuTypePriority": "custom", "gpuCount": 1,
        "cloudType": cloud, "computeType": "GPU", "containerDiskInGb": job.container_disk_gb,
        # direct ssh (tar streams) needs a public IP: community hosts must offer one; secure always does
        "volumeInGb": 0, "ports": ["22/tcp"], **({"supportPublicIp": True} if cloud == "COMMUNITY" else {}),
        **({"allowedCudaVersions": s["cuda_versions"]} if s["cuda_versions"] else {}), "env": {"PUBLIC_KEY": pubkey, **job.env},
    }
    if job.volume:
        vol = next((v for v in api.volumes() if v.get("name") == job.volume), None)
        if not vol:
            raise SystemExit(f"network volume {job.volume!r} not found (gipper pod volume ls)")
        spec.update(networkVolumeId=vol["id"], volumeMountPath="/workspace", dataCenterIds=[vol["dataCenterId"]])
    return api.create_pod(spec)


def _wait_ready(api, pod_id, timeout_s=900):
    t0 = _now()
    while _now() - t0 < timeout_s:
        p = api.get_pod(pod_id)
        port = (p.get("portMappings") or {}).get("22")
        if p.get("publicIp") and port:
            return p, Remote(p["publicIp"], port)
        time.sleep(5)
    raise RuntimeError(f"pod {pod_id} had no ssh endpoint after {timeout_s}s")


class BadHost(RuntimeError):
    pass


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


def run(job, *, dry_run=False, log_dir=None, gpu_override=None, throughput=None):
    api = Api()
    s = settings()
    run_id = ledger.new_id("job")
    if job.sensitivity == "owned-only":
        raise SystemExit(f"{job.name}: sensitivity = owned-only; not running on a rented GPU")
    cands = pick_gpu(api, job)
    if gpu_override:
        cands = [c for c in cands if c[0]["id"] == gpu_override] or cands
    # Runpod takes the first available type in the list; don't let a busy cheap type
    # silently become one several times the price (the smoke test got a $0.69 5090
    # for a $0.34 4090 ask). Ceiling: the job's own, else 2x the cheapest that fits.
    stocked = [c[1] for c in cands if c[2]] or [c[1] for c in cands]
    ceiling = job.max_usd_per_h or 2 * min(stocked)
    cands = [c for c in cands if c[1] <= ceiling] or cands[:1]
    gpu, price, _ = cands[0]
    est = estimate(job, price, throughput)
    est.update(gpu=gpu["id"], usd_per_h=price, cap_usd=job.max_usd, max_hours=job.max_hours)
    ledger.write("estimate", est, job=run_id, refs={"name": job.name, "workload": job.workload})
    spent_today = ledger.spend_since(ledger.today_start())
    print(f"{job.name}: {gpu['id']} at ${price}/h; estimate {est['wall_s'] / 60:.0f} min, ${est['usd']} ({est['basis']}); "
          f"cap ${job.max_usd}, {job.max_hours} h; today so far ${spent_today:.2f} of ${s['daily_cap_usd']}")
    if spent_today + min(est["usd"], job.max_usd) > s["daily_cap_usd"]:
        raise SystemExit(f"refusing: today's spend ${spent_today:.2f} + this job would exceed the daily cap ${s['daily_cap_usd']}")
    if dry_run:
        return run_id, None

    log_dir = Path(log_dir or DATA / "jobs" / run_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "run.log", "ab")
    pubkey = ensure_key()
    pod_id, t_create, cost_h, outcome = None, None, price, "error"
    stop = {"why": None}
    old = signal.signal(signal.SIGINT, lambda *a: stop.update(why="interrupted"))
    signal.signal(signal.SIGTERM, lambda *a: stop.update(why="terminated"))
    try:
        for attempt in range(3):
            pod = None
            tries = [(job.cloud, [c[0]["id"] for c in cands[:6]])]
            if job.cloud == "COMMUNITY":  # nothing free in community: same types, secure cloud, same ceiling
                sec = [c for c in candidates(api.gpu_types(), job.min_mem_gb, "SECURE", job.prefer) if c[1] <= ceiling]
                if sec:
                    tries.append(("SECURE", [c[0]["id"] for c in sec[:6]]))
            for cloud, ids in tries:
                try:
                    pod = _create(api, job, ids, pubkey, run_id, cloud)
                    break
                except RunpodError as e:
                    if "no instances" not in str(e).lower():
                        raise
                    print(f"no {cloud.lower()} instances free for {', '.join(i.replace('NVIDIA ', '') for i in ids)}")
            if pod is None:
                raise RunpodError("no instances available at or under the price ceiling")
            pod_id, t_create = pod["id"], _now()
            cost_h = pod.get("costPerHr") or price
            ledger.write("pod.create", {"pod": pod_id, "gpu": (pod.get("machine") or {}).get("gpuTypeId") or gpu["id"],
                                        "usd_per_h": cost_h, "deadline": t_create + job.max_hours * 3600 + 600},
                         job=run_id, refs={"name": job.name})
            print(f"pod {pod_id} created (${cost_h}/h); waiting for ssh")
            pod, remote = _wait_ready(api, pod_id)
            if not remote.wait(600):
                raise RuntimeError("ssh never came up")
            ledger.write("pod.ready", {"pod": pod_id, "after_s": round(_now() - t_create), "machine": pod.get("machineId"),
                                       "gpu": (pod.get("machine") or {}).get("gpuTypeId")}, job=run_id)
            try:
                health = preflight(remote)
                ledger.write("host.ok", {"pod": pod_id, **health}, job=run_id)
                break
            except BadHost as e:
                print(f"rejecting host {pod.get('machineId')}: {e}")
                ledger.write("host.reject", {"pod": pod_id, "machine": pod.get("machineId"), "why": str(e)}, job=run_id)
                _terminate(api, pod_id, t_create, cost_h, run_id, job)
                pod_id = None
        else:
            raise RuntimeError("three hosts in a row were busy or broken")
        print(f"ssh ready after {_now() - t_create:.0f}s; uploading inputs")

        remote.run(f"mkdir -p {JOBDIR}")
        remote.put(BOOTSTRAP, f"{JOBDIR}/.gipper/bootstrap.sh")
        for t in job.inputs:
            remote.put(t.src, f"{JOBDIR}/{t.dst}", t.exclude)
        envs = " ".join(f"export {k}={shlex.quote(v)};" for k, v in job.env.items())
        body = f"source {JOBDIR}/.gipper/bootstrap.sh; {envs} cd {JOBDIR}; " + \
               (f"{job.setup} && " if job.setup else "") + f"{job.command}"
        limit = int(job.max_hours * 3600)
        # Only the job is backgrounded, with every stream redirected, so this ssh returns at
        # once (backgrounding the whole `cd && nohup` list kept ssh's stdout open until the
        # job ended). setsid detaches it from the ssh session entirely.
        job_cmd = f"timeout {limit} bash -c {shlex.quote(body)}; echo $? > {JOBDIR}/.gipper/exit"
        remote.run(f"cd {JOBDIR}; setsid nohup bash -c {shlex.quote(job_cmd)} > {JOBDIR}/.gipper/run.log 2>&1 < /dev/null &",
                   timeout=60)
        ledger.write("run.start", {"pod": pod_id}, job=run_id)
        print("running; log follows\n" + "-" * 60, flush=True)

        offset, last_ck, code = 0, _now(), None
        while code is None:
            if stop["why"]:
                raise KeyboardInterrupt(stop["why"])
            spent = (_now() - t_create) / 3600 * cost_h
            if spent >= job.max_usd or _now() - t_create >= job.max_hours * 3600:
                raise Capped(f"${spent:.2f} / {(_now() - t_create) / 60:.0f} min reached the job's cap")
            st = remote.run(f"cat {JOBDIR}/.gipper/exit 2>/dev/null || echo running", check=False, timeout=60)
            chunk = remote.run(f"tail -c +{offset + 1} {JOBDIR}/.gipper/run.log", check=False, timeout=60)
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
                        remote.get(f"{JOBDIR}/{t.src}", t.dst)
                ledger.write("checkpoint", {"src": job.checkpoint_src}, job=run_id)
                last_ck = _now()
            time.sleep(10)
        print("-" * 60 + f"\nexit {code}; collecting outputs")
        for t in job.outputs:
            remote.get(f"{JOBDIR}/{t.src}", t.dst)
        remote.get(f"{JOBDIR}/.gipper/run.log", str(log_dir / "pod"))
        ledger.write("collect", {"outputs": [t.dst for t in job.outputs]}, job=run_id)
        outcome = "succeeded" if code == 0 else ("timeout" if code == 124 else "failed")
    except Capped as e:
        outcome = "capped"
        ledger.write("cap", {"reason": str(e)}, job=run_id)
        print(f"\nstopping: {e}")
        _salvage(locals().get("remote"), job)
    except KeyboardInterrupt as e:
        outcome = "interrupted"
        print(f"\nstopping: {e or 'interrupted'}")
        _salvage(locals().get("remote"), job)
    except (RunpodError, RuntimeError, OSError) as e:
        ledger.write("error", {"error": str(e)[:500]}, job=run_id)
        print(f"\nerror: {e}")
        _salvage(locals().get("remote"), job)
    finally:
        signal.signal(signal.SIGINT, old)
        if pod_id:
            _terminate(api, pod_id, t_create, cost_h, run_id, job)
        ledger.write("run.end", {"outcome": outcome, "units": job.units_count, "units_name": job.units_name},
                     job=run_id, refs={"name": job.name, "workload": job.workload})
        log.close()
    return run_id, outcome


def _terminate(api, pod_id, t_create, cost_h, run_id, job):
    for attempt in range(5):
        try:
            api.terminate_pod(pod_id)
            break
        except RunpodError as e:
            print(f"terminate failed ({e}); retrying")
            time.sleep(5)
    secs = _now() - t_create
    ledger.write("pod.terminate", {"pod": pod_id}, job=run_id)
    ledger.write("spend", {"provider": "runpod", "pod": pod_id, "seconds": round(secs), "usd_per_h": cost_h,
                           "usd": round(secs / 3600 * cost_h, 4), "basis": "computed", "accuracy": "estimated"},
                 job=run_id, refs={"name": job.name, "workload": job.workload})
    print(f"pod {pod_id} terminated after {secs / 60:.1f} min: ~${secs / 3600 * cost_h:.3f}")


def _salvage(remote, job):
    """Bring back whatever outputs exist before the pod goes."""
    if not remote:
        return
    for t in job.outputs:
        try:
            remote.get(f"{JOBDIR}/{t.src}", t.dst)
        except Exception as e:  # salvage is best effort; teardown must still run
            print(f"salvage of {t.src} failed: {e}")
