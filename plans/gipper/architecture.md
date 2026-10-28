# Architecture: gipper

**Date:** 2026-10-20
**Mindset:** mvp (clean modules, tests on the paths that touch money and other people's jobs)
**Scale:** personal (one operator, two owned GPUs, two cloud markets: Runpod and Vast.ai)
**Status:** Approved (Marc, 2026-10-20)

---

## Technical Summary

gipper is a small coordinator daemon (`gipperd`) on lisbon plus a CLI and a
client library. Work arrives as **requests** tagged with a declared
**workload** (importance, flexibility, staleness or deadline, interruptibility,
placement, resource needs). The scheduler keeps a model of each **device**
(lisbon CUDA 12 GB, toronto MPS 16 GB, rented GPU types), admits requests into
time-bounded **leases** on a **most-important-wins** basis, and pre-empts lower
effective priority with the declared grace. Nothing is set aside for scheduled
work: every consumer, scheduled or ad hoc, **asks** and receives either a
grant or a **denial with an explanation** it can act on (why, who holds the
GPU, when to ask again, what alternatives exist). A workload that cannot get
its work done before its deadline or staleness limit **escalates** (email to
Marc through roger, the fleet-mail gateway, plus an Open Loops entry).
Consumers not yet asking for themselves are steered through **managed
consumers** (stop and restore a systemd timer, unload an Ollama model), and
every pause carries a **restore obligation** the daemon keeps across crashes.

Jobs run through **executors**: local (transient `systemd-run --user` units, so
a job survives a daemon restart and can be killed as a cgroup), ssh (toronto),
and Runpod (pods from a prepared image, a network volume for cached weights,
rsync over the pod's ssh, teardown in a `finally` plus a reaper). An
**estimator** answers feasibility, wall time, wait and dollars per placement
from throughput recorded in earlier runs (or says there is no history).

Everything that happens is written to an append-only **receipt ledger**
(JSONL). Milton ingests it through a new adapter; Somm receives completed runs
as `calls` rows (`origin="gipper"`) and, in a later phase, consults gipper's
admission before dispatching Ollama calls, so GPU-backed LLM traffic from Keel
and gabe becomes request-driven without changing either project.

## Technology Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Language | Python 3.12, stdlib-first (`sqlite3`, `urllib`, `tomllib`, `subprocess`) | Matches voxhaul and Somm; few dependencies on a tool that babysits others |
| CLI | `argparse`, one `gipper` entry point | As in voxhaul; no framework needed |
| State | SQLite (WAL) at `~/.local/share/gipper/gipper.db` | Queue, leases, obligations, throughput; same choice as Somm; transactional |
| Ledger | Append-only JSONL receipts, `~/.local/share/gipper/receipts/YYYY-MM.jsonl`, schema `gipper.receipt/v1` | Durable, greppable, the Milton/chip pattern; source of truth for accounting |
| Daemon IPC | HTTP over a Unix socket (`$XDG_RUNTIME_DIR/gipper.sock`), JSON | Local-only, no auth surface; cheap enough to call from Somm's dispatch path |
| Local execution | `systemd-run --user` transient units | Jobs outlive the daemon; cgroup stop/kill; journald logs |
| Remote owned | ssh + rsync to toronto; `nohup` + pidfile | toronto is macOS (no systemd); already used by farm.py |
| Cloud | Runpod REST/GraphQL via `urllib`; custom image; network volume | Key from `.env`; no SDK dependency; volume avoids 5 GB downloads per job |
| Tests | pytest; fake executors and a fake Runpod API | Money and pre-emption paths tested without spending or stopping real jobs |

## System Architecture

```
  ad-hoc agent / Marc                standing workloads
  gipper submit | estimate | run      keel-worker.timer   gabe          Ollama
          |                                 ^ stop/start      | request   ^ unload/observe
          |                                 |                 v grant/deny+why
          v                                 |                 |           |
  +---------------------------- gipperd (lisbon) --------------------------------+
  | API (unix socket) -> Scheduler --- leases / queue / obligations (SQLite)     |
  |                        |  effective priority = importance + urgency + age    |
  |                        |  admission vs Device capacity, backfill, pre-empt   |
  |   Workload registry  --+--> Managed consumers (timer, ollama)                |
  |   Estimator <-- throughput (from receipts)    Escalation -> roger (email)    |
  |                        v                                                     |
  |                    Executors: local(systemd-run) | ssh(toronto) | runpod     |
  |                        |                                   reaper, caps      |
  +------------------------|-----------------------------------------------------+
                           v
                 receipts/*.jsonl  ---> Milton adapter (gipper.py)
                           |       ---> Somm calls rows (origin="gipper")
  Somm (in Keel/gabe) --admission?--> gipperd   (phase 3: GPU-backed Ollama calls)
```

## Components

### Workload registry
- **Purpose:** the declared intent behind every GPU consumer.
- **Responsibilities:** load `workloads/*.toml` (in the gipper repo now; each
  project may later ship its own and register the path); validate; expose
  importance, flexibility, `max_staleness`/deadline, interruptibility (grace,
  resume), windows, placement, needs (`gpu_mem_gb`, backends, model), units.
- **Interface:** `Registry.get(name) -> Workload`; `gipper workloads`.
- **Note:** the orient report puts this metadata on Somm's workload record.
  Somm's `policy_json` rejects unknown keys today, so gipper owns the
  declarations first and Somm gains a `scheduling_json` field in phase 3
  (a Somm change, reviewed in Somm). Names match Somm workload names.

### Device model
- **Purpose:** what each GPU can take right now.
- **Responsibilities:** static capacity (lisbon 12 GB CUDA with ~0.8 GB
  headroom for the desktop; toronto 16 GB unified MPS with a configurable
  share; Runpod types from a price/spec snapshot); live usage (`nvidia-smi`,
  Ollama `/api/ps`, toronto `vm_stat`/`ps`); declared usage from leases.
- **Interface:** `Device.free_mem()`, `Device.fits(needs)`.

### Scheduler
- **Purpose:** turn requests into leases, fairly and predictably.
- **Responsibilities:** effective priority `p = w_imp[importance] + urgency +
  w_age*wait_h`, where urgency rises as slack (deadline or staleness limit
  minus now minus estimated duration) shrinks; **most important wins**: admit
  the highest `p` that fits; backfill a lower request only if its estimate
  ends before the next queued higher request could start; pre-empt holders
  with lower `p` that are interruptible, giving their grace; never pre-empt
  `importance=critical`. No capacity is reserved for scheduled work.
- **Answers:** every request gets a grant or a denial. A denial carries
  `reason` (busy, does not fit, placement forbidden, budget), `holder`
  (workload and priority holding the device), `retry_after` (from the
  holder's estimate), `queue_position`, and `alternatives` (another device, a
  cloud placement with its estimate, or, for LLM work, a hosted model through
  Somm). The asker adapts: wait, take an alternative, or skip and report.
  Leases have a TTL renewed by the holder's heartbeat; expiry reclaims.
- **Interface:** `request()`, `grant()`, `renew()`, `release()`, `revoke()`;
  ticks every few seconds.

### Managed consumers
- **Purpose:** steer standing workloads gipper can't rewrite yet.
- **Kinds (MVP):** `systemd-timer` (Keel worker: stop the timer, let the
  in-flight service finish within grace or stop it, later start the timer);
  `ollama` (unload resident models through the API; observe loads).
- **Not a managed consumer:** gabe. It asks (`gipper request` at the top of
  `gabe-edition.sh`, its own repo, no root needed) and adapts to the answer;
  the same path Keel moves to once its Ollama calls are gated in Somm.
- **Restore obligations:** every pause writes an obligation row (what to
  restore, when at the latest, verification) before acting. A daemon start
  replays open obligations; each restore is verified (timer active, unit
  state) and receipted. `gipper status` lists open pauses.
- **Staleness:** a paused consumer's `max_staleness` is a hard ceiling; the
  scheduler restores it before the limit even if higher work waits.

### Escalation
- **Purpose:** a workload that cannot get its work done says so to a person.
- **Trigger:** a request whose deadline or `max_staleness` will be missed at
  the current queue (estimated), or repeated denials past a declared limit.
- **Channel:** email to Marc through **roger** (`rogerd`, the fleet-mail
  gateway) with the workload, what it needed, who held the GPU, the
  alternatives it was offered, and one-line actions; plus an Open Loops
  `needs_you` entry. Rate-limited per workload; receipted.

### Executors
- **Interface:** `prepare(job, placement)`, `start()`, `status()`,
  `logs()`, `sync_checkpoint()`, `stop(grace)`, `collect()`, `teardown()`.
- **local:** `systemd-run --user --unit gipper-JOB ... --property=...`;
  logs from journald; stop sends SIGTERM then kill after grace.
- **ssh (toronto):** rsync inputs to `~/gipper/jobs/JOB`, `nohup` with a
  pidfile, rsync outputs back; environment per machine declared in the job.
- **runpod:** pick a GPU type that fits; create an on-demand pod from the
  gipper image with the network volume attached; rsync inputs over the pod's
  ssh; run; sync checkpoints every N minutes; collect; terminate in `finally`.
  **Reaper:** lists pods with the `gipper-` prefix against the ledger and
  terminates unknowns. **Caps:** per job, per day, and the account's own spend
  limit; a job is killed at its cap with a receipt.

### Estimator
- **Purpose:** numbers before commitment.
- **Responsibilities:** feasibility (memory, backend, sensitivity vs
  placement); wall = setup + units / throughput(workload, device); wait =
  queue model on owned devices; dollars = hourly price x wall (cloud) and a
  notional owned cost plus "displaces: X" for owned devices; "no history"
  marked explicitly; ±band from estimate-vs-actual so far.
- **Throughput source:** receipts (units done / run seconds), plus calibration
  jobs per workload.

### Ledger
- **Purpose:** the record Milton, Somm and replay read.
- **Records (`gipper.receipt/v1`, one JSON object per line):** `request`,
  `decision` (admit / defer / pre-empt / deny, with the priority terms),
  `lease`, `pause`, `restore`, `run` (start, end, units, outcome), `estimate`,
  `spend` (provider, GPU type, seconds, dollars, basis), each with ids that
  link them.

### CLI and client library
- `gipper workloads | status | estimate JOB | submit JOB [--placement
  auto|lisbon|toronto|runpod[:gpu]] | run --workload W -- CMD | logs ID |
  cancel ID | reap | ledger [--since]`.
- `gipper.client.lease(workload, gpu_mem_gb)`: a context manager for
  cooperative jobs; heartbeats; raises/calls back on revocation notice so the
  job can checkpoint (voxhaul training uses this).

## Data Model

### Workload
```
name: str — matches the Somm workload name where one exists
project: str
importance: critical|high|normal|low|best-effort
flexibility: interactive|deadline|window|anytime
max_staleness: duration|None; deadline: datetime|None
interruptible: {grace_s, resume: none|task|checkpoint}
windows: {prefer: [...], avoid: [...]}
placement: owned-only|cloud-ok|cloud-preferred
needs: {gpu_mem_gb, backends: [cuda, mps], model?}
units: str — "steps", "takes", "frames"
consumer: {kind: systemd-timer|ollama|asks, ref} — how gipper reaches it; "asks" = requests for itself
```

### Request / Lease / Obligation (SQLite)
```
request(id, workload, job_id?, placement_pref, units, created_at, state, effective_priority)
lease(id, request_id, device, gpu_mem_gb, granted_at, expires_at, heartbeat_at, state)
obligation(id, consumer, action, due_by, created_at, state, verified_at)
job(id, workload, spec_json, placement, executor_ref, state, started_at, ended_at, cost_usd)
throughput(workload, device, units_per_s, setup_s, n, updated_at)
escalation(id, workload, request_id, reason, sent_at, channel, state)
```

### Relationships
- Workload 1→n Request; Request 1→0..1 Lease; Request 0..1→1 Job.
- A pre-emption creates Obligations on the paused consumer or job.
- A denied Request records its answer; a missed deadline creates an Escalation.
- Receipts reference request, lease, job and obligation ids.

## APIs / Interfaces

### gipperd local API
- **Type:** HTTP/JSON over a Unix socket (internal).
- **Key endpoints:**
  - `POST /requests` — ask for resources (workload, needs, units); returns id and state
  - `GET /requests/{id}` — poll; `admitted` carries the lease
  - `POST /leases/{id}/heartbeat`, `DELETE /leases/{id}` — renew, release
  - `POST /jobs` — submit a job spec (executor-run work)
  - `GET /status` — devices, leases, queue, open obligations
  - `GET /admission?workload=W&device=lisbon` — fast yes/no/defer-until for
    Somm's gate (phase 3)

### Runpod
- **Type:** REST (`rest.runpod.io/v1`) for pods and volumes; GraphQL for
  prices and account limits. Verified so far: GraphQL `myself` (balance,
  spend limit, pods, volumes) works with the key.

## Implementation Phases

### Phase 0: Runpod core (side track, unblocks the skeleton work now)
**Goal:** run a voxhaul or skeleton GPU job on a rented GPU today, safely,
without waiting for the scheduler.
- `gipper pod` commands that need no daemon: `gpus` (types, memory, live
  prices), `estimate` (price x known throughput, or a stated guess), `run JOB`
  (create pod, sync inputs, run with live logs, periodic checkpoint sync,
  collect outputs, terminate in `finally`), `ls`, `reap`, `cost`.
- A prepared image (CUDA + PyTorch + the voxhaul engine venvs) and a network
  volume holding model weights (VoxCPM2 now), so a run starts in minutes.
- Caps per job and per day; the account spend limit as the backstop; the
  receipt ledger from day one (the same `gipper.receipt/v1` records phase 1
  builds on).
- **E2E:** finish the Carla cadence experiment's remaining takes and a voxhaul
  library slice on a 4090; receipts show seconds and dollars; `reap` finds
  nothing left running.

### Phase 1: Local requests and leases (the Keel problem)
**Goal:** urgent local GPU work makes room through requests, and everything
paused comes back.
- Workload registry + declarations for keel-research, gabe, Ollama
  models, voxhaul-library, voxhaul-train.
- SQLite state, receipt ledger, gipperd with the scheduler and lisbon device.
- Managed consumers: systemd-timer (Keel), ollama (unload); restore
  obligations with replay on start.
- Grant/denial answers with reasons and alternatives; gabe asks at the top of
  `gabe-edition.sh` and adapts; escalation through roger.
- `gipper run --workload W -- CMD` (local executor) and `gipper status`.
- **E2E:** a voxhaul job at `importance=high` via `gipper run` stops Keel's
  timer and unloads Ollama, runs, and Keel's timer is verified restored (also
  after killing gipperd mid-run). Pause drill on Keel recorded in the ledger.

### Phase 2: Placement, estimates and the cloud
**Goal:** pick lisbon, toronto or a rented GPU with numbers.
- Job spec shared by all executors; ssh executor (toronto); the phase 0 Runpod
  code becomes the `runpod` executor under the scheduler.
- Estimator with feasibility; throughput from receipts.
- Calibration: voxhaul LoRA 600 steps and a 100-take library slice on
  lisbon, toronto, Runpod 4090 and L40S (~$1).
- **E2E:** `gipper estimate` then `gipper submit --placement auto` on a voxhaul
  job; estimate-vs-actual in the ledger; reaper finds nothing orphaned.

### Phase 3: Telemetry and Somm/Milton integration
**Goal:** the accounting lands where the estate already looks.
- Milton adapter `gipper` (chip as template) added to `BUILTIN_ADAPTERS`.
- Somm: completed runs as `calls` rows (`origin="gipper"`, real duration,
  cost basis/kind); `scheduling_json` on workloads; an **admission check**
  in the Ollama dispatch path that can defer or reroute per workload (needs a
  Somm change: pre-call hooks cannot defer today).
- Keel and gabe Ollama traffic gated; Keel's timer wrapper retired.
- **E2E:** a Keel source-scoring call during a high-priority lease defers (or
  reroutes per its policy) and resumes afterwards; Milton shows the run's cost.

### Phase 4: Divisible work, spot and replay
**Goal:** efficiency.
- Sharded jobs across placements (generalise farm.py); spot pods with
  checkpoint sync and resume; packing several jobs onto one pod; idle teardown.
- Policy replay over recorded requests.
- **E2E:** a farm render split across lisbon, toronto and a pod, finished
  frames identical to a local render.

## Technical Risks

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Orphaned pods keep billing | H | M | Teardown in `finally`, reaper on a timer, per-job and daily caps, account spend limit |
| gipperd dies holding pauses | H | M | Obligations written before acting; replay on start; a systemd watchdog; `status` shows open pauses |
| Somm gate adds latency or fails closed | M | M | Local socket, cached answers (seconds), fail-open to today's behaviour if gipperd is down |
| Scheduling a job that doesn't actually fit (MPS shares memory with the desktop) | M | M | Conservative headroom; live usage checks; a short smoke step before long runs |
| Runpod API or image drift breaks runs | M | M | Pin image and API calls; fake-API tests; a nightly no-op smoke pod later |
| Root units (Ollama) cannot be paused | M | H | Unload via API; gate callers (gabe asks; Keel via Somm); a scoped sudoers rule only if drills show it's needed |
| Low-priority work starved for too long | M | M | Urgency aging toward staleness; escalation by email before a limit is missed |
| Phase 0 Runpod code diverges from the later executor | L | M | Same job spec and receipt schema from the start; phase 2 wraps it rather than rewriting |
| Somm or Milton changes stall in their own review | M | M | Phases 1-2 need neither; phase 3 changes are small and separately reviewable |

## Dependencies

### External
- Runpod (pods, network volumes, prices): key in `gipper/.env`.
- Ollama HTTP API on lisbon (`/api/ps`, unload via `keep_alive: 0`).
- systemd user manager on lisbon; ssh and rsync to toronto.

### Internal
- Somm (`somm`, `somm-core`): Repository for `calls` rows; dispatch path for
  the admission check (phase 3).
- Milton: adapter protocol and `BUILTIN_ADAPTERS` (phase 3).
- voxhaul: first client (jobs, `gpu.clear`).
- skeleton `render/farm.py`: phase 4 client.

## Integration Impact

### New Components → Existing Consumers

| New Component | Existing Consumer | Integration Action |
|---|---|---|
| `gipper run` / lease client | voxhaul `cli.py` GPU steps (`run(..., gpu_step=True)`), `voxhaul.batch` LoRA runs | Replace `gpu.clear()` with a gipper lease when gipperd is reachable; keep the old path as fallback during phase 1 |
| systemd-timer consumer | `keel-worker.timer` (user unit) | No change in Keel; gipper stops and restores the timer |
| request + answer contract | gabe `deploy/gabe-edition.sh` (repo-owned script behind the root unit) | Phase 1: ask at the top, adapt to a denial (wait, hosted model via Somm, or skip and report); no root change |
| Escalation | roger (`rogerd` fleet-mail gateway), Open Loops | Phase 1: send through roger's outbound path; open a `needs_you` loop |
| ollama consumer | Ollama system service; Somm `keep_alive=30m` default | Unload via API in phase 1; phase 3 Somm asks gipper before dispatch |
| Somm admission check | Somm `SommLLM.generate` Ollama dispatch (`client.py` dispatch, `routing.py`) | Phase 3 Somm change: a deferral-capable admission phase per workload, fail-open |
| Somm run recording | Somm `calls` table, `harnesses/ledger.py` precedent | gipper writes `origin="gipper"` rows with real duration |
| Milton `gipper` adapter | Milton `adapters/__init__.py` `BUILTIN_ADAPTERS` | Add adapter modelled on `chip.py`; root via `MILTON_GIPPER_RECEIPTS` |
| Executors (sharding) | skeleton `render/farm.py` | Phase 4: farm submits frame shards to gipper |

### Deprecated / Replaced Code

| Old Code | Replaced By | Files That Import It | Action |
|---|---|---|---|
| voxhaul `voxhaul/gpu.py` (`clear`, Ollama quiet-wait) | gipper lease | voxhaul `cli.py` | Fallback in phase 1-2, remove in phase 3 |
| Hand-run `systemd-run` units and toronto copies (voxhaul `runs/*`, cadence run.sh) | `gipper submit` | n/a (operational) | Stop the practice once phase 2 lands |
| farm.py's own split/ssh logic | gipper sharded jobs | skeleton `render/farm.py` | Phase 4; keep farm.py as a thin client |

### Integration Verification
- Phase 1: real Keel pause and restore around a real voxhaul job, including a gipperd crash mid-run.
- Phase 2: a real voxhaul LoRA job placed by `--placement auto`, estimate vs actual recorded, no orphan pods.
- Phase 3: a real Keel Ollama call deferred during a lease; the run visible in Milton and Somm.
- Phase 4: a real farm render split three ways.

## Security Considerations

- The Runpod key stays in `gipper/.env` (600, git-ignored), read at run time,
  never logged; pods receive no key.
- Per-job `sensitivity = "owned-only"` blocks cloud placement (e.g. show audio).
- The daemon socket is user-only (0600, `$XDG_RUNTIME_DIR`).
- Cloud artefacts are deleted at teardown; the network volume holds only model
  weights and images.

## Future Considerations

- Scheduled workloads on rented GPUs (the model already allows it).
- More providers (Modal for serverless inference). Vast.ai and Shadeform: done in 0.1–0.2, see plans/gipper/providers.md.
- Projects declaring their own workloads; Somm as the single home of metadata.
- A status page; carbon- or price-aware windows from external feeds.

---

**Next:** run `forge-itemize` to break this into executable tasks.
