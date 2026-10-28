# gipper

GPU work for the Lyra Forge estate. You describe a job in one TOML file, and gipper
rents the GPU that will get it done best, runs it, brings the results home, gives
the GPU back, and puts every second and dollar on a ledger. It shops two markets
today, Runpod and Vast.ai (Shadeform is wired in but unused), and it learns from
its own rentals: which providers and machines turn out bad, how fast transfers
really go, how long a workload takes, and what providers actually bill.

**Status: 0.2.1.** Rented GPUs from the CLI or Python, no daemon. Requests, leases
and pre-emption across owned machines (lisbon's 3060, toronto's M6) are the
next phase: [orientation](plans/gipper/orientation.md) ·
[architecture](plans/gipper/architecture.md) · [plan](plans/gipper/itemized-plan.md).

## Install

```
uv tool install -e ~/Projects/gipper
cp deploy/gipper-reap.* ~/.config/systemd/user/ && systemctl --user enable --now gipper-reap.timer
```

Keys go in `gipper/.env` (mode 600, git-ignored) or the environment:
`RUNPOD_API_KEY`, `VAST_API_KEY`, `SHADEFORM_API_KEY`. A provider without a key
is skipped when renting. Keys only travel in request headers, and receipts and
errors are scrubbed of them. Machines get gipper's own ssh key
(`~/.config/gipper/id_ed25519`), never an API key.

## Use

```
gipper pod gpus --min-mem 24 --region US,CA [--udp]   # what's on offer, scored for a 30-minute job
gipper pod estimate job.toml                          # how long and how much, offer by offer
gipper pod run job.toml [--dry-run] [--provider vast] [--gpu "RTX 4090"]
gipper pod hosts                                      # what gipper has learned about providers and machines
gipper pod cost [--since 2026-10-27]                  # per job: computed and billed
gipper pod ls | reap [--dry-run] | reconcile | account
gipper ledger [--since ...] [--job ID]                # raw receipts
```

A job ([`examples/smoke.toml`](examples/smoke.toml); every field in
[`gipper/job.py`](gipper/job.py) and [`gipper/placement.py`](gipper/placement.py)):

```toml
name = "carla-cadence"
workload = "voxhaul-compare"          # jobs with the same workload share a memory of compute times

[gpu]
min_mem_gb = 24
prefer = ["RTX 4090", "RTX A6000"]    # in order; names match across providers
regions = ["US", "CA"]                # ISO country codes; default anywhere
providers = ["vast", "runpod"]        # default: every provider with a key

[pod]
ports = ["5004/udp"]                  # published; UDP rules out Runpod

[[inputs]]
src = "~/Projects/voxhaul"
dst = "voxhaul"
exclude = [".git", "runs"]

[[outputs]]
src = "out"
dst = "~/Projects/voxhaul/runs/x/out"

[run]
command = "python -m voxhaul.compare"
checkpoint = { src = "out", every_s = 300 }

[budget]
max_usd = 1.0
max_hours = 2.0

[placement]                           # optional, and better than memory's guess
compute_s = 900
setup_download_gb = 5                 # weights and wheels the job fetches itself
```

## How gipper chooses

Every offer is judged on the whole run, not its hourly price
([docs/placement.md](docs/placement.md) has the details):

- **Time:** machine ready + setup + upload + the job's own downloads + compute +
  output download. Each figure comes from the job's `[placement]` hints, from
  memory, from what the provider advertises (Vast publishes host link speeds),
  or from a stated default, and `estimate` says which.
- **Dollars:** rental for that time, plus Vast's bandwidth fees, times what this
  provider has really billed compared with rate × lifetime on real work (Vast
  about ×1.1: disk from creation, rentals rounded up; Runpod about ×1.0).
- **Risk:** the chance the host turns out bad, from memory: the machine's own
  record, pulled towards its provider's, pulled towards 10% for a stranger.
  A failure costs `failure_cost_usd` ($1) plus the time and rent it wastes.
- **Score** = dollars + risk + waiting, with an hour of waiting worth
  `hour_value_usd` ($3).

There is no short-or-long switch. On a 15-minute job, a $0.40/h Vast 4090 and a
$0.69/h Runpod 4090 are 8 cents apart, less than the 25 cents a 23% chance of a
bad host costs, so Runpod wins. On a ten-hour job they are $2.57 apart, and Vast
wins. `prefer` still comes first, and an offer whose expected cost
exceeds `max_usd` is left out. `[placement] objective = "cost"` stops valuing
waiting (failures still cost); `"time"` values it at $50/h; real-time work uses
`profile = "interactive"` to rank by measured round trip first.

Memory is the ledger itself. A rental is *good* once its job started, and *bad*
if the host was broken (busy GPU, CUDA that won't initialise, ssh that never came
up, a failed probe) or the connection was lost. A host that works but doesn't
suit the job (`run.Unsuitable`), a job that fails on a working host, and an
error on this side count against nobody.

## What `pod run` guarantees

- **The machine is given back, and gipper checks.** Teardown runs in `finally`
  after success, failure, Ctrl-C or a cap, and counts as done only when the
  provider stops listing the machine. Until then it stays
  `pod.termination_pending`, gipper rents no replacement, and the reaper (every
  15 minutes) retries it first. The reaper also ends any `gipper-*` machine the
  ledger forgot or that passed its deadline, on every provider. The job runs
  detached under `timeout max_hours`, so a lost client can't overrun it.
- **Hosts are checked before anything is uploaded:** the GPU is idle,
  `cuInit` succeeds (with one retry without CUDA forward-compat libraries on
  error 804), the compute capability meets the job's floor, and optionally a
  measured transfer probe passes. A bad host is replaced, up to 3 times.
- **Caps:** per job (`max_usd`, `max_hours`, `[gpu] max_usd_per_h`) and per day
  (`daily_cap_usd` in `~/.config/gipper/config.toml`, default $5). Daily spend
  uses providers' bills where reconciled.
- **Outputs** sync down every `checkpoint.every_s` and are salvaged on failure.
- **Environment:** each provider's default image unless `[pod] image` says
  otherwise. Each job sources a bootstrap providing `uv` and
  `GIPPER_TORCH_INDEX`, the PyTorch wheel index for the host's driver. Published
  ports arrive as `GIPPER_PUBLIC_IP` and `GIPPER_PORT_<n>_<PROTO>` (the outside
  port, which on Vast differs from the inside one).

## Markets

| | Runpod | Vast.ai | Shadeform (unused) |
|---|---|---|---|
| You rent | container, root | container on someone's machine, root | VM on one of 30+ clouds, user `shadeform` |
| Good for | reliability (81 of 81 hosts good so far), secure cloud, network volumes | consumer GPUs everywhere, cheap, UDP | datacenter cards with one bill |
| Ports | TCP/HTTP, **no UDP** | TCP and UDP, random outside port | TCP and UDP (UFW; the cloud may filter) |
| Bills | per pod per day | itemised: GPU, disk, bandwidth | no billing API |

An RTC job that must stay on Runpod can relay media through Cloudflare's TURN
over TCP/TLS on 443, at some latency cost.

## From another repo

The CLI is the simplest surface. From Python (gipper is stdlib-only):

```python
from gipper import run, ledger
from gipper.job import load
from gipper.providers import load as provider

job = load("job.toml")
offers, ceiling, notes = run.plan(job, provider="vast")   # ranked; each has .estimate
run_id, outcome = run.run(job, provider="vast", log_dir="runs/x")
provider("vast").list_ours()                              # for your own guards
```

`run.preflight(remote)` can be wrapped to add host checks of your own: raise
`run.Unsuitable` when the host works but doesn't suit the job, and `run.BadHost`
when it's broken. Both replace the host, but only `BadHost` counts against it in
memory. To move a job between providers, check [docs/clients.md](docs/clients.md).

## More

- [docs/placement.md](docs/placement.md): the cost model, memory and every `[placement]` field
- [docs/clients.md](docs/clients.md): writing jobs that run on any provider; the Python surface
- [docs/lessons.md](docs/lessons.md): what the first weeks of rentals taught
- [AGENTS.md](AGENTS.md): for agents changing gipper
- [CHANGELOG.md](CHANGELOG.md)
