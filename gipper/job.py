"""The job spec: one TOML file, shared by every executor (Runpod now; local and
toronto in phase 2).

    name = "carla-cadence"
    workload = "voxhaul-compare"          # what this is, for throughput and Somm
    units = { name = "takes", count = 200 }
    sensitivity = "cloud-ok"              # "owned-only" refuses rented GPUs

    [gpu]
    min_mem_gb = 16
    prefer = ["NVIDIA RTX A5000", "NVIDIA GeForce RTX 4090"]   # optional order
    cloud = "COMMUNITY"                    # or SECURE
    max_usd_per_h = 0.6                    # optional ceiling; default 2x the cheapest that fits

    [pod]                                  # optional overrides
    image = "runpod/base:1.4.1-cuda1300-ubuntu2404"
    container_disk_gb = 40
    volume = "gipper-models"               # network volume name (pins the data centre)

    [env]                                  # non-secret environment for the job
    HF_HOME = "/root/hf"

    [[inputs]]                             # local -> pod, under /root/job/<dst>
    src = "~/Projects/voxhaul"
    dst = "voxhaul"
    exclude = ["runs", ".git", "__pycache__"]

    [[outputs]]                            # pod /root/job/<src> -> local <dst>
    src = "out"
    dst = "~/Projects/voxhaul/runs/x/out"

    [run]
    setup = "bash voxhaul/scripts/pod-setup.sh"
    command = "python -m voxhaul.workers.compare ..."
    checkpoint = { src = "out", every_s = 300 }   # synced down while running

    [budget]
    max_usd = 1.0
    max_hours = 2.0
"""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from gipper.config import settings


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
    cloud: str = "COMMUNITY"
    max_usd_per_h: float | None = None
    image: str = ""
    container_disk_gb: int = 40
    volume: str | None = None
    env: dict = field(default_factory=dict)
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    checkpoint_src: str | None = None
    checkpoint_every_s: int = 300
    max_usd: float = 2.0
    max_hours: float = 3.0
    path: Path | None = None


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
        cloud=gpu.get("cloud", s["cloud"]), max_usd_per_h=gpu.get("max_usd_per_h"), image=pod.get("image", s["image"]),
        container_disk_gb=int(pod.get("container_disk_gb", s["container_disk_gb"])), volume=pod.get("volume"),
        env={k: str(v) for k, v in d.get("env", {}).items()},
        inputs=tr(d.get("inputs", []), "src"), outputs=tr(d.get("outputs", []), "dst"),
        checkpoint_src=ck.get("src"), checkpoint_every_s=int(ck.get("every_s", s["checkpoint_every_s"])),
        max_usd=float(budget.get("max_usd", s["default_job_cap_usd"])),
        max_hours=float(budget.get("max_hours", s["default_max_hours"])), path=path)
    for t in job.inputs:
        if not Path(t.src).exists():
            raise JobError(f"{path.name}: input {t.src} does not exist")
    if any(k.upper().endswith(("KEY", "TOKEN", "SECRET")) for k in job.env):
        raise JobError(f"{path.name}: secrets do not belong in [env]")
    return job
