# gipper

GPU work for the Lyra Forge estate. Workloads **ask** for GPU time instead of
claiming it; the most important request wins; owned machines (lisbon's RTX 3060,
toronto's M6) and rented GPUs (Runpod) are both placements; and every second and
dollar lands on a ledger Somm and Milton can read.

Design: [orientation](plans/gipper/orientation.md) ·
[architecture](plans/gipper/architecture.md) ·
[plan](plans/gipper/itemized-plan.md).

**Status:** phase 0 (rented GPUs from the CLI, no daemon). Requests, leases and
pre-emption on owned machines are phase 1.

## Install

```
uv tool install -e ~/Projects/gipper
```

The Runpod key lives in `gipper/.env` (`RUNPOD_API_KEY=...`, mode 600,
git-ignored) or the environment. It only ever travels in an Authorization
header; receipts and errors are scrubbed of it. Pods get gipper's own ssh key
(`~/.config/gipper/id_ed25519`), never the API key.

## Rented GPUs

```
gipper pod account                    # balance, spend now, spend limit
gipper pod gpus --min-mem 20          # what fits, live $/h and stock
gipper pod estimate job.toml          # likely wall time and dollars per GPU type
gipper pod run job.toml [--dry-run]   # rent, upload, run, stream, collect, terminate
gipper pod ls | reap [--dry-run] | cost [--since YYYY-MM-DD]
gipper ledger [--since ...] [--job ID]
```

A job is one TOML file (full reference in `gipper/job.py`; examples in
`examples/`): what to upload, the command, what to bring back, how much GPU
memory it needs, and its budget.

What `pod run` guarantees:
- **The GPU is always given back.** Terminate runs in `finally`, after
  success, failure, Ctrl-C or a cap. The job runs detached on the pod under
  `timeout max_hours`, so a lost client cannot leave it running past its budget,
  and `gipper pod reap` (meant for a 15-minute timer) ends any `gipper-` pod
  the ledger says should be gone.
- **Caps:** per job (`[budget] max_usd`, `max_hours`) and per day
  (`daily_cap_usd` in `~/.config/gipper/config.toml`, default $5); a GPU
  price ceiling (`[gpu] max_usd_per_h`, default 2x the cheapest type in stock).
- **Placement:** community cloud with a public IP first, then secure cloud,
  same ceiling. (Community hosts without a public IP can't take direct ssh, so
  they are skipped.)
- **Environment:** `runpod/base` image; each job sources a bootstrap that
  provides `uv` and `GIPPER_TORCH_INDEX`, the PyTorch wheel index matching the
  host's driver (`uv pip install torch --index-url "$GIPPER_TORCH_INDEX"`).
- **Outputs** are synced down every `checkpoint.every_s` while running and
  salvaged before teardown if anything goes wrong.

## Ledger

`~/.local/share/gipper/receipts/YYYY-MM.jsonl`, one `gipper.receipt/v1`
record per line: `estimate`, `pod.create`, `pod.ready`, `run.start`,
`checkpoint`, `collect`, `cap`, `error`, `pod.terminate`, `spend`, `run.end`,
`reap`. Spend is computed from the pod's own hourly price and its lifetime.

## Lessons from the first runs (2026-10-20)

- Cloudflare in front of Runpod's API rejects Python's default user agent
  (error 1010): gipper sends its own.
- Filtering hosts by CUDA version or requiring a community public IP can empty
  the market; gipper keeps the public-IP need but falls back to secure cloud and
  picks the torch build per host instead of filtering hosts.
- Stock moves fast: ask for several GPU types in preference order.
- Backgrounding `cd X && nohup job &` over ssh backgrounds the whole list, so
  ssh waits for the job; gipper backgrounds only the job, with `setsid` and
  every stream redirected.
- Host health varies, especially on community cloud: one 5090 was at 100% use
  with 1 GB held before the job started, an L40S host failed its first CUDA call
  twice, an RTX 5000 Ada was "busy or unavailable". gipper now preflights the GPU
  before uploading and replaces a bad host (up to 3), recording `host.reject`
  with the machine id.
- The torch build is chosen from the driver version, not from nvidia-smi's
  "CUDA Version" (CUDA forward-compat libraries in an image inflate it); the
  default image carries no CUDA at all.
- Micro-batch 1 leaves a 4090 mostly idle for LoRA training (3.9 s/step vs the
  3060's 4.5): keep the effective batch, raise the micro-batch (e.g. 4 x 4).
- A unit started with `systemd-run` disappears when stopped; to resume it,
  recreate it with the same command line rather than `systemctl start`.
- First real job: 227 VoxCPM2 takes on a secure-cloud 4090 in 19.7 minutes
  (about 4 of them setup) for $0.29; the same work was ~3.5 h on toronto.
