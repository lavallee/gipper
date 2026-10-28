# Driving gipper from another repo

## Writing a job that runs on any provider

- **Paths:** use paths relative to the job directory. The job runs in
  `~/job` on the machine, which is `/root/job` in containers (Runpod, Vast) and
  `/home/shadeform/job` on Shadeform. Don't hardcode `/root` or `/workspace`;
  `/workspace` exists only with a Runpod network volume.
- **GPU names:** plain names in `prefer` ("RTX 4090", "A40") match every
  provider's spelling. Variants are separate cards: "RTX 4090" does not match
  "RTX 4090D".
- **Runpod-only fields:** `[gpu] cloud` only affects Runpod, and `[pod] volume`
  pins a job to Runpod.
- **CUDA and the driver:** hosts vary. On Vast a 535 driver showed up in Quebec,
  and some images carry CUDA forward-compat libraries that break `cuInit` (error
  804). gipper checks `cuInit` on every host and works around 804. If the job
  needs a specific runtime, set `[placement] min_cuda` (filters Vast offers and
  becomes Runpod's `allowedCudaVersions`) and `min_compute_capability` (checked
  on the machine). Install torch from `$GIPPER_TORCH_INDEX`.
- **UDP:** `[pod] ports = ["5004/udp"]`, then advertise
  `$GIPPER_PUBLIC_IP:$GIPPER_PORT_5004_UDP`. The outside port is not 5004 on Vast.
- **Telling gipper about the job:** `[placement] compute_s`, `setup_download_gb`
  and `download_gb` make the first estimates honest; after a few runs, memory
  takes over for compute time. Give runs of the same work the same `workload` so
  they share that memory.

## The Python surface

These names are stable within 0.x:

| Name | What |
|---|---|
| `gipper.job.load(path)` | a `Job` |
| `gipper.run.plan(job, provider=, gpu=, throughput=)` | `(offers, hourly ceiling, notes)`; each offer carries `.estimate` |
| `gipper.run.run(job, provider=, dry_run=, log_dir=, gpu_override=, throughput=)` | `(run_id, outcome)`: `succeeded`, `failed`, `timeout`, `capped`, `interrupted`, `error`, `termination_pending` |
| `gipper.run.estimate(job, offer=o)` | the whole-run estimate for one offer |
| `gipper.run.preflight(remote)` | GPU idle check; wrap it to add your own. Raise `gipper.run.Unsuitable` when the host works but doesn't suit the job (wrong architecture for a pinned runtime), and `gipper.run.BadHost` when it's broken. Both replace the host; only `BadHost` counts against it in memory |
| `gipper.run.settings` | looked up at call time, so a wrapper can change caps for one run |
| `gipper.providers.load(name)` | `.offers(job)`, `.list_ours()`, `.get(id)` (Vast), `.terminate(id)`, `.account()`, `.billed(since)` |
| `gipper.providers.runpod.Runpod().api()` | the raw Runpod client |
| `gipper.ledger.read(since=, kinds=, job=)` / `.write(kind, payload, job=)` | receipts |
| `gipper.history.History.load()` | memory: `.failure(p, machine)`, `.ready_s`, `.mbps`, `.compute_s(workload, gpu)`, `.billing_factor(p)` |
| `gipper.ssh.Remote(host, port, user)` | `.run`, `.put`, `.get` over gipper's key |

Kept for scripts written before 0.2: `gipper.runpod.api.Api` and
`gipper.runpod.run._wait_ready`. Any other `gipper.runpod.run` name raises an
error pointing at `gipper.run`, so a script that patches the old `pick_gpu` fails
instead of silently patching nothing.

## Receipts a guard can rely on

- `pod.create`: `provider`, `pod`, `machine`, `country`, `gpu`, `usd_per_h`, `deadline`
- `host.rtt`: round trip from `origin` (this machine's hostname) to the rented machine
- `pod.closure_verified`: the provider no longer lists the machine. Until it
  appears there may be a `pod.termination_pending`; treat that machine as still
  billing.
- `spend` (computed at teardown) and `spend.billed` (the provider's bill, from
  `reconcile`; the latest one per machine counts). `gipper.billing.spend_since(t)`
  combines them by machine.
- Receipts without `provider` predate 2026-10-26 and are Runpod's.

gipper runs from this checkout for every client (an editable install), so a
change here reaches running clients' next import. Agents changing gipper should
read [AGENTS.md](../AGENTS.md).
