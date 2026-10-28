# gipper

GPU work for the Lyra Forge estate. Workloads **ask** for GPU time instead of
claiming it; the most important request wins; owned machines (lisbon's RTX 3060,
toronto's M6) and rented GPUs (Runpod) are both placements; and every second and
dollar lands on a ledger Somm and Milton can read.

Design: [orientation](plans/gipper/orientation.md) ·
[architecture](plans/gipper/architecture.md) ·
[plan](plans/gipper/itemized-plan.md).

**Status:** phase 0 (rented GPUs from the CLI, no daemon) across three markets:
Runpod, Vast.ai and Shadeform. Requests, leases and pre-emption on owned
machines are phase 1.

## Install

```
uv tool install -e ~/Projects/gipper
```

Provider keys live in `gipper/.env` (mode 600, git-ignored) or the environment:
`RUNPOD_API_KEY`, `VAST_API_KEY`, `SHADEFORM_API_KEY`. A provider without a key
is skipped (Vast's and Shadeform's offers are still listed by `gpus`). Keys only
ever travel in a request header; receipts and errors are scrubbed of all of them.
Machines get gipper's own ssh key (`~/.config/gipper/id_ed25519`), never an API key.

## Rented GPUs

```
gipper pod gpus --min-mem 20 --region US,CA [--udp]   # offers across every market, ranked
gipper pod account                    # balance per provider
gipper pod estimate job.toml          # likely wall time and dollars per offer
gipper pod run job.toml [--dry-run] [--provider vast] [--gpu "RTX 4090"]
gipper pod ls | reap [--dry-run] | cost [--since YYYY-MM-DD]
gipper ledger [--since ...] [--job ID]
```

A job is one TOML file (full reference in `gipper/job.py`; examples in
`examples/`): what to upload, the command, what to bring back, how much GPU
memory it needs, where it may run, which ports it publishes, and its budget.

### Markets

| | Runpod | Vast.ai | Shadeform |
|---|---|---|---|
| What you rent | container (root) | container on one host's machine (root) | whole VM on one of 30+ clouds (`shadeform` user) |
| Strength | secure cloud, network volumes | consumer GPUs (3090/4090/5090) in many places, cheap | datacenter cards (A6000, L40S, H100) with one bill |
| Ports | TCP/HTTP only, no UDP | TCP and UDP, random external port on a shared IP | TCP and UDP (UFW at boot; the cloud may filter too) |
| Region filter | `countryCodes`; stock asked per country | `geolocation` | country from the region's display name |

`[gpu] regions = ["US", "CA"]` keeps a job in those countries on every market
(so a 4090 that only exists in Iceland is not rented for US-facing RTC work).
`[pod] ports = ["5004/udp"]` publishes ports, skips markets that can't, and gives
the job `GIPPER_PUBLIC_IP` and `GIPPER_PORT_5004_UDP` (the outside port to
advertise, e.g. as an ICE candidate). `[gpu] providers` narrows the markets;
`prefer` names match across them ("RTX 4090" is Runpod's "NVIDIA GeForce RTX 4090").

What `pod run` guarantees:
- **Cleanup is attempted and verified.** Termination runs in `finally` after
  success, failure, Ctrl-C or a cap. Accepted DELETE is followed by bounded
  provider-list checks. Only verified absence emits `pod.closure_verified`,
  `pod.terminate` and final estimated rental spend. API errors or continued
  visibility retain `pod.termination_pending`; `run.end` reports that outcome,
  and the reaper prioritizes the owned ID without waiting for its deadline.
  No replacement host is rented while prior closure is unverified. A detached
  job's `timeout` bounds its computation, not the rental bill: unreachable cloud
  control planes can keep charging until removal is actually confirmed.
  External guards and reaper sweeps remain necessary. Shadeform also offers
  provider-side auto-delete deadlines/spend ceilings.
- **Caps:** per job (`[budget] max_usd`, `max_hours`) and per day
  (`daily_cap_usd` in `~/.config/gipper/config.toml`, default $5); a price
  ceiling (`[gpu] max_usd_per_h`, default 2x the cheapest stocked offer of the
  GPUs the job prefers, or of all if it prefers none).
- **Placement:** one ranking across markets: preferred GPUs first, then offers
  with known stock, then price. If an offer is gone, the next is tried; a host
  that is busy, broken or never comes up is replaced (up to 3). Runpod: community
  cloud with a public IP first, then secure. Vast: verified machines, reliability
  >= 0.97, >= 100 Mb/s down, enough direct ports.
- **Environment:** each provider's default image (`runpod/base`, Vast's
  `vastai/base-image`, Shadeform's CUDA OS) unless `[pod] image` says otherwise;
  each job sources a bootstrap that provides `uv` and `GIPPER_TORCH_INDEX`, the
  PyTorch wheel index matching the host's driver
  (`uv pip install torch --index-url "$GIPPER_TORCH_INDEX"`).
- **Outputs** are synced down every `checkpoint.every_s` while running and
  salvaged before teardown if anything goes wrong.

When renting UDP isn't worth it, an RTC job on Runpod can relay media through
Cloudflare's TURN over TCP/TLS on 443 (`turn.cloudflare.com`), at some latency cost.

## For clients (other repos driving gipper)

The CLI is the stable surface: `gipper pod run JOB.toml [--provider vast]`. From
Python:

- `from gipper import run`: use `run.plan(job, provider=)` for ranked offers,
  `run.run(job, provider=, dry_run=, log_dir=)` to do the run, and
  `run.preflight` / `run.BadHost` to add host checks.
- `from gipper.providers import load`: `load("vast").list_ours()` and
  `.terminate(id)` for guards. `load("runpod").api()` is the raw Runpod client.
- `from gipper import ledger`. Machine receipts carry `provider`; a receipt
  without one is Runpod's.

To move a Runpod job to Vast, set `[gpu] providers = ["vast"]` (or drop the line
to shop both). Also check four things:
- Runpod-only fields are ignored elsewhere: `cloud`, and `volume`, which pins a
  job to Runpod.
- Don't hardcode `/root/job` or `/workspace`; Shadeform's home is
  `/home/shadeform`, so use relative paths.
- Use plain GPU names in `prefer`.
- A Vast host's driver can be older, as can its CUDA version (a 535 driver
  showed up in Quebec), so pin that in `preflight` if the job needs it.

`gipper.runpod.api.Api` and `gipper.runpod.run._wait_ready` remain for scripts
written before 2026-10-26; any other `gipper.runpod.run` name now raises an
error that points here.

For opt-in total-cost/time placement, measured transfer checks and early actual
CUDA initialization, see [workload placement](docs/workload-placement.md).

## Ledger

`~/.local/share/gipper/receipts/YYYY-MM.jsonl`, one `gipper.receipt/v1`
record per line: `estimate`, `pod.create`, `pod.ready`, `host.ok`/`host.reject`,
`run.start`, `checkpoint`, `collect`, `cap`, `error`, `pod.terminate`, `spend`,
`run.end`, `reap`, `pod.termination_pending`, `pod.closure_verified`. Machine receipts carry `provider` (absent on receipts from
before 2026-10-26, which are all Runpod's). Spend is computed from the machine's
own hourly price and its lifetime.

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
- Runpod's stock is per country, not just per type: on 2026-10-26 the only 4090
  in stock was in EUR-IS-2 (Iceland). Without a region filter gipper would have
  rented it for US-facing work.
- Vast, first runs (2026-10-27). A US 4090 smoke job: ssh in 38 s, the whole
  round trip in 47 s, about $0.006. On the UDP echo job, the first host (an RTX
  3060 in Quebec) was at 100% GPU use before the job started, and preflight
  replaced it. The second took 214 s to reach ssh (image pull). UDP echoed 5 of
  5 from lisbon in about 22 ms, and replies came from the same public port, so
  `GIPPER_PUBLIC_IP:GIPPER_PORT_<n>_UDP` is the address to advertise. Vast's
  credit fell $0.04 against $0.016 computed; the ledger's Vast spend is an
  undercount until we know what else Vast bills (perhaps disk or a minimum).
- First real job: 227 VoxCPM2 takes on a secure-cloud 4090 in 19.7 minutes
  (about 4 of them setup) for $0.29; the same work was ~3.5 h on toronto.
