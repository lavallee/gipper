"""The job spec: one TOML file, shared by every executor (rented GPUs from Runpod,
Vast and Shadeform now; owned machines planned).

    name = "carla-cadence"
    workload = "example-workload"          # what this is, for throughput history
    units = { name = "takes", count = 200 }
    sensitivity = "cloud-ok"              # "owned-only" refuses rented GPUs

    [gpu]
    min_mem_gb = 16
    prefer = ["RTX A5000", "RTX 4090"]     # optional order; names match across providers
    regions = ["US", "CA"]                 # ISO country codes it may run in; default anywhere
    providers = ["vast", "runpod"]         # markets to shop; default all with a key
    cloud = "COMMUNITY"                    # Runpod: COMMUNITY (then SECURE) or SECURE
    max_usd_per_h = 0.6                    # optional ceiling; default 2x the cheapest that fits

    [pod]                                  # optional overrides
    image = "runpod/base:1.4.1-cuda1300-ubuntu2404"   # default: each provider's own
    container_disk_gb = 40
    volume = "gipper-models"               # Runpod network volume name (pins the data centre)
    ports = ["5004/udp", "8080/tcp"]       # published; the job sees GIPPER_PUBLIC_IP and
                                           # GIPPER_PORT_5004_UDP (the external port). UDP
                                           # rules out Runpod.

    [env]                                  # non-secret environment for the job
    HF_HOME = "/root/hf"

    [[inputs]]                             # local -> pod, under /root/job/<dst>
    src = "~/gpu-project"
    dst = "project"
    exclude = ["runs", ".git", "__pycache__"]

    [[outputs]]                            # pod /root/job/<src> -> local <dst>
    src = "out"
    dst = "~/gpu-project/runs/x/out"

    [run]
    setup = "bash project/setup.sh"
    command = "python project/main.py"
    checkpoint = { src = "out", every_s = 300 }   # synced down while running

    [budget]
    max_usd = 1.0
    max_hours = 2.0

    [placement]                            # optional: how to judge offers on the whole run
    compute_s = 900                        # (all fields in gipper/placement.py)
    setup_download_gb = 5
"""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from gipper.config import settings
from gipper.placement import Placement, load_placement


class JobError(ValueError):
    pass


@dataclass
class Transfer:
    src: str
    dst: str
    exclude: list = field(default_factory=list)


@dataclass
class Job:
    name: str
    workload: str
    command: str
    setup: str = ""
    units_name: str = "units"
    units_count: float | None = None
    sensitivity: str = "cloud-ok"
    min_mem_gb: float = 8
    prefer: list = field(default_factory=list)
    regions: list = field(default_factory=list)
    providers: list = field(default_factory=list)
    cloud: str = "COMMUNITY"
    max_usd_per_h: float | None = None
    image: str = ""
    container_disk_gb: int = 40
    volume: str | None = None
    ports: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    checkpoint_src: str | None = None
    checkpoint_every_s: int = 300
    max_usd: float = 2.0
    max_hours: float = 3.0
    path: Path | None = None
    placement: Placement = field(default_factory=Placement)


def load(path):
    path = Path(path).expanduser().resolve()
    d = tomllib.loads(path.read_text())
    s = settings()
    for k in ("name", "workload"):
        if not d.get(k):
            raise JobError(f"{path.name}: `{k}` is required")
    run = d.get("run", {})
    if not run.get("command"):
        raise JobError(f"{path.name}: [run] command is required")
    gpu, pod, budget = d.get("gpu", {}), d.get("pod", {}), d.get("budget", {})
    units = d.get("units", {})
    ck = run.get("checkpoint") or {}
    sens = d.get("sensitivity", "cloud-ok")
    if sens not in ("cloud-ok", "owned-only"):
        raise JobError(f"{path.name}: sensitivity must be cloud-ok or owned-only")
    base = path.parent

    def tr(items, local_key):
        out = []
        for it in items:
            if not it.get("src") or not it.get("dst"):
                raise JobError(f"{path.name}: inputs/outputs need src and dst")
            t = Transfer(it["src"], it["dst"], list(it.get("exclude", [])))
            loc = Path(getattr(t, local_key)).expanduser()
            setattr(t, local_key, str(loc if loc.is_absolute() else (base / loc).resolve()))
            out.append(t)
        return out

    job = Job(
        name=d["name"], workload=d["workload"], command=run["command"], setup=run.get("setup", ""),
        units_name=units.get("name", "units"), units_count=units.get("count"), sensitivity=sens,
        min_mem_gb=float(gpu.get("min_mem_gb", 8)), prefer=list(gpu.get("prefer", [])),
        regions=[r.upper() for r in gpu.get("regions", s["regions"])], providers=list(gpu.get("providers", [])),
        cloud=gpu.get("cloud", s["cloud"]), max_usd_per_h=gpu.get("max_usd_per_h"), image=pod.get("image", ""),
        container_disk_gb=int(pod.get("container_disk_gb", s["container_disk_gb"])), volume=pod.get("volume"),
        ports=[str(p) for p in pod.get("ports", [])],
        env={k: str(v) for k, v in d.get("env", {}).items()},
        inputs=tr(d.get("inputs", []), "src"), outputs=tr(d.get("outputs", []), "dst"),
        checkpoint_src=ck.get("src"), checkpoint_every_s=int(ck.get("every_s", s["checkpoint_every_s"])),
        max_usd=float(budget.get("max_usd", s["default_job_cap_usd"])),
        max_hours=float(budget.get("max_hours", s["default_max_hours"])), path=path,
        placement=load_placement(d.get("placement"), error=JobError))
    for p in job.ports:
        num, _, proto = p.partition("/")
        if not num.isdigit() or proto.lower() not in ("", "tcp", "udp", "http"):
            raise JobError(f"{path.name}: ports look like \"5004/udp\" or \"8080/tcp\", got {p!r}")
    if job.volume and job.providers and job.providers != ["runpod"]:
        raise JobError(f"{path.name}: a network volume is a Runpod thing; set providers = [\"runpod\"]")
    for t in job.inputs:
        if not Path(t.src).exists():
            raise JobError(f"{path.name}: input {t.src} does not exist")
    if any(k.upper().endswith(("KEY", "TOKEN", "SECRET")) for k in job.env):
        raise JobError(f"{path.name}: secrets do not belong in [env]")
    return job
