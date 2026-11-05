# gipper

[Website](https://lavallee.github.io/gipper/) · [Quickstart](https://lavallee.github.io/gipper/guides/quickstart.html) · [Python API](https://lavallee.github.io/gipper/guides/clients.html)

gipper runs jobs on rented NVIDIA GPUs. Describe the hardware, command, input
files, outputs, and budget in a TOML file; gipper compares cloud offers, rents a
machine, checks it, runs the job, collects results, and verifies teardown.

Use it for model training and fine-tuning, batch inference, rendering, or other
GPU workloads that run from a Linux shell. Your job supplies its own software
and dependencies. The same CLI and Python API work across Runpod and Vast.ai.

Placement considers the whole job: startup, setup, transfers, compute, bandwidth
fees, and host failures. An append-only local ledger records runs and costs;
future estimates learn from **your own** workloads and provider history. A fresh
installation starts with stated defaults, not someone else's rental history.

**Status: 0.4.0, alpha.** Linux client, CLI and Python API, no daemon. Runpod and
Vast have live-run validation; Shadeform is experimental and has not had a live
smoke test. See the [roadmap](ROADMAP.md) for planned features and
[SECURITY.md](SECURITY.md) for trust and billing limits. Licensed under [MIT](LICENSE).

## Install

Requires Linux, Python 3.12+, OpenSSH (`ssh`, `ssh-keygen`), and `tar` with
gzip support. `ping` is optional for interactive placement. The Python runtime
has no third-party dependencies. Remote machines need a Linux shell, `tar`,
`timeout`, `curl`, and NVIDIA tools; the bootstrap installs `uv` if needed.

Install the CLI from [PyPI](https://pypi.org/project/gipper/):

```sh
uv tool install gipper
gipper --help
```

Alternatively, use `python -m pip install gipper` in a virtual environment.
To get the example jobs or install from source:

```sh
git clone https://github.com/lavallee/gipper.git
cd gipper
uv tool install .
gipper --help
```

Use `pip install .` in a virtual environment if you do not use `uv`.
For development, use `uv sync --extra test` (see [CONTRIBUTING.md](CONTRIBUTING.md)).

Configure only the providers you intend to use:

```sh
mkdir -p ~/.config/gipper
(umask 077; cp -n .env.example ~/.config/gipper/.env)
chmod 600 ~/.config/gipper/.env
# Edit ~/.config/gipper/.env to add your provider keys.
```

Keys can also come from environment variables: `RUNPOD_API_KEY`, `VAST_API_KEY`,
`SHADEFORM_API_KEY`. Environment variables take precedence. `GIPPER_ENV` selects
an exclusive dotenv file; otherwise the user-config dotenv overrides the legacy
checkout `.env`. A provider without a key is skipped when renting. Keys travel
in API request headers and are scrubbed from receipts and API error bodies.
Machines get gipper's own SSH key (`~/.config/gipper/id_ed25519`).

Check estimates before paying for a smoke run:

```sh
gipper pod estimate examples/smoke.toml --provider vast
gipper pod run examples/smoke.toml --provider vast --dry-run
# The following command spends money:
gipper pod run examples/smoke.toml --provider vast
gipper pod ls
```

A dry run contacts provider APIs but does not rent. Set provider-side spending
alerts too: estimates and client-side caps cannot guarantee a final bill.

Install the cleanup timer on the machine that runs jobs (recommended):

```sh
mkdir -p ~/.config/systemd/user
cp deploy/gipper-reap.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gipper-reap.timer
systemctl --user list-timers gipper-reap.timer
```

The service expects `~/.local/bin/gipper`; adjust `ExecStart` if installed
elsewhere. A service cannot see keys exported only in an interactive shell; use
the user-config dotenv above. User timers require a running user manager. If
jobs must outlive logout, configure user lingering or an equivalent scheduler.
See [SECURITY.md](SECURITY.md) for host trust and cleanup limitations.

### Local configuration and data

| Path / override | Purpose |
|---|---|
| `~/.config/gipper/config.toml` / `GIPPER_CONFIG` | Settings such as `daily_cap_usd = 5.0`; see [`config.DEFAULTS`](gipper/config.py) |
| `.env` beside the config file / `GIPPER_ENV` | Provider keys (simple `NAME=value`, no shell expansion) |
| `~/.config/gipper/` / `GIPPER_KEYS` | SSH key directory |
| `~/.local/share/gipper/` / `GIPPER_DATA` | Append-only receipts used for history and spend |

Keep the ledger on durable storage and restrict access to it. Separate installs
sharing a provider account should coordinate: the reaper owns the `gipper-*`
namespace, not just the machines in one local ledger.

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
name = "my-gpu-job"
workload = "my-workload"          # jobs with the same workload share a memory of compute times

[gpu]
min_mem_gb = 24
prefer = ["RTX 4090", "RTX A6000"]    # in order; names match across providers
regions = ["US", "CA"]                # ISO country codes; default anywhere
providers = ["vast", "runpod"]        # default: every provider with a key

[pod]
ports = ["5004/udp"]                  # published; UDP rules out Runpod

[[inputs]]
src = "~/gpu-project"
dst = "project"
exclude = [".git", "runs"]

[[outputs]]
src = "out"
dst = "~/gpu-project/runs/x/out"

[run]
command = "python project/main.py"
checkpoint = { src = "out", every_s = 300 }

[budget]
max_usd = 1.0
max_hours = 2.0

[placement]                           # optional, and better than memory's guess
compute_s = 900
setup_download_gb = 5                 # weights and wheels the job fetches itself
```

## After a provider incident

Every upload is recorded before it leaves: a manifest of what was sent, sensitive-looking
files flagged, and provider keys refused outright. When a provider reports a problem, one
command gives you what to rotate, what to review, and what the trail can't vouch for:

```
gipper custody report --provider runpod --since 2026-10-01 --check-providers
```

See [docs/custody.md](docs/custody.md), including how to record what went around gipper.

## How gipper chooses

Every offer is judged on the whole run, not its hourly price
([docs/placement.md](docs/placement.md) has the details):

- **Time:** machine ready + setup + upload + the job's own downloads + compute +
  output download. Each figure comes from the job's `[placement]` hints, from
  memory, from what the provider advertises (Vast publishes host link speeds),
  or from a stated default, and `estimate` says which.
- **Dollars:** rental for that time, plus Vast's bandwidth fees, times what this
  provider has billed compared with rate × lifetime in your ledger. Without
  qualifying history, the billing factor is 1.0; storage and rounding can make
  the final bill differ.
- **Risk:** the chance the host turns out bad, from memory: the machine's own
  record, pulled towards its provider's, pulled towards 10% for a stranger.
  A failure costs `failure_cost_usd` ($1) plus the time and rent it wastes.
- **Score** = dollars + risk + waiting, with an hour of waiting worth
  `hour_value_usd` ($3).

For short jobs, startup time and the cost of a failed host can outweigh a lower
hourly price. For long jobs, the hourly rate has more influence. Provider names
do not determine the winner: the ranking follows the job and available history.
`prefer` comes first, and an offer whose expected cost exceeds `max_usd` is
left out. `[placement] objective = "cost"` removes the value of waiting while
still pricing failures; `"time"` values waiting at $50/h. For latency-sensitive
services, `profile = "interactive"` probes candidate hosts and ranks by round
trip first. Batch jobs account for transfer time without preferring nearby hosts.

Memory is the ledger itself. A rental is *good* once its job started, and *bad*
if the host was broken (busy GPU, CUDA that won't initialise, ssh that never came
up, a failed probe) or the connection was lost. A host that works but doesn't
suit the job (`run.Unsuitable`), a job that fails on a working host, and an
error on this side count against nobody.

## Run lifecycle and safeguards

- **The machine is given back, and gipper checks.** Teardown runs in `finally`
  after success, failure, Ctrl-C or a cap, and counts as done only when the
  provider stops listing the machine. Until then it stays
  `pod.termination_pending`, gipper rents no replacement, and the reaper (every
  15 minutes) retries it first. The reaper also ends any `gipper-*` machine the
  ledger forgot or that passed its deadline, on every provider. The job runs
  detached under `timeout max_hours`. This limits the command, not the rental;
  the reaper must still terminate a machine if the client dies.
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
| Good for | secure cloud, network volumes | consumer GPUs everywhere, cheap, UDP | datacenter cards with one bill |
| Ports | TCP/HTTP, **no UDP** | TCP and UDP, random outside port | TCP and UDP (UFW; the cloud may filter) |
| Bills | per pod per day | itemised: GPU, disk, bandwidth | no billing API |

An RTC job that must stay on Runpod can relay media through Cloudflare's TURN
over TCP/TLS on 443, at some latency cost.

## Python API

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
- [docs/lessons.md](docs/lessons.md): operational behavior and troubleshooting
- [CONTRIBUTING.md](CONTRIBUTING.md): development and tests
- [SECURITY.md](SECURITY.md): trust model and vulnerability reports
- [RELEASING.md](RELEASING.md): release checklist
- [CHANGELOG.md](CHANGELOG.md)
