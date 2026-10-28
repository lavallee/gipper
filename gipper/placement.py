"""What a job tells gipper about itself so offers can be judged on the whole run,
not the hourly price: `[placement]` in the job file. Everything is optional;
whatever is missing comes from memory (gipper.history) or a stated default.

    [placement]
    objective = "auto"            # auto | cost | time (see docs/placement.md)
    compute_s = 550               # expected compute seconds once inputs are up
    compute_s_by_gpu = { A40 = 250 }   # per GPU, where known
    setup_s = 120                 # the job's own setup, excluding the downloads below
    setup_download_gb = 8.5       # what the job fetches itself (weights, wheels)
    upload_gb = 0.1               # default: the size of [[inputs]]
    download_gb = 0.2             # outputs coming back
    transfer_efficiency = 0.5     # share of a host's advertised speed to expect
    min_host_download_mbps = 500  # hard filters on what the provider advertises
    min_host_upload_mbps = 100
    min_cuda = 12.8
    min_compute_capability = 8.0
    probe_transfer_mb = 2         # measure the ssh path before uploading (MiB each way)
    probe_timeout_s = 120
    min_measured_upload_mbps = 1
    min_measured_download_mbps = 1

`profile = "batch"` is accepted for job files written for 0.1's placement profiles.
"""
import fnmatch
import math
from dataclasses import dataclass, field, fields
from pathlib import Path

OBJECTIVES = ("auto", "cost", "time")


@dataclass
class Placement:
    objective: str = "auto"
    profile: str = "batch"               # 0.1 compatibility; only "batch" exists
    compute_s: float | None = None
    compute_s_by_gpu: dict = field(default_factory=dict)
    setup_s: float | None = None
    setup_download_gb: float = 0.0
    upload_gb: float | None = None
    download_gb: float = 0.0
    transfer_efficiency: float = 0.5
    min_host_download_mbps: float | None = None
    min_host_upload_mbps: float | None = None
    min_cuda: float | None = None
    min_compute_capability: float | None = None
    probe_transfer_mb: float = 0.0
    probe_timeout_s: float = 120.0
    min_measured_upload_mbps: float | None = None
    min_measured_download_mbps: float | None = None


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def load_placement(data, *, error=ValueError):
    """The job's [placement] table (absent: all defaults), validated."""
    data = dict(data or {})
    unknown = set(data) - {f.name for f in fields(Placement)}
    if unknown:
        raise error(f"[placement] has unknown fields: {', '.join(sorted(unknown))} (see gipper/placement.py)")
    p = Placement(**data)
    if p.objective not in OBJECTIVES:
        raise error(f"placement.objective must be one of {', '.join(OBJECTIVES)}")
    if p.profile != "batch":
        raise error('placement.profile: only "batch" remains; latency work is chosen with [gpu] regions')
    for name in ("compute_s", "setup_s", "setup_download_gb", "upload_gb", "download_gb", "min_host_download_mbps",
                 "min_host_upload_mbps", "min_cuda", "min_compute_capability", "probe_transfer_mb", "probe_timeout_s",
                 "min_measured_upload_mbps", "min_measured_download_mbps"):
        v = getattr(p, name)
        if v is not None and (not _number(v) or v < 0):
            raise error(f"placement.{name} must be a non-negative number")
    if not (_number(p.transfer_efficiency) and 0 < p.transfer_efficiency <= 1):
        raise error("placement.transfer_efficiency must be in (0, 1]")
    if not isinstance(p.compute_s_by_gpu, dict) or not all(isinstance(k, str) and _number(v) and v > 0
                                                           for k, v in p.compute_s_by_gpu.items()):
        raise error("placement.compute_s_by_gpu maps GPU names to seconds")
    if p.probe_transfer_mb and not 1 <= p.probe_transfer_mb <= 64 or p.probe_timeout_s > 300:
        raise error("placement.probe_transfer_mb is 0 (off) or 1-64 MiB; probe_timeout_s at most 300")
    if (p.min_measured_upload_mbps or p.min_measured_download_mbps) and not p.probe_transfer_mb:
        raise error("placement.min_measured_*_mbps need probe_transfer_mb")
    return p


def input_gb(job):
    """Bytes the [[inputs]] will upload (decimal GB), honouring their excludes."""
    size = 0
    for t in job.inputs:
        root = Path(t.src)
        for path in (root.rglob("*") if root.is_dir() else [root]):
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(root) if root.is_dir() else Path(path.name)
            if any(fnmatch.fnmatch(str(rel), pat) or any(fnmatch.fnmatch(part, pat) for part in rel.parts)
                   for pat in t.exclude):
                continue
            size += path.stat().st_size
    return size / 1e9
