# Itemized Plan: gipper

> Orient: [orientation.md](./orientation.md) | Architect: [architecture.md](./architecture.md)
> Generated: 2026-10-20

## Context Summary

GPU capacity (lisbon RTX 3060, toronto M6) is shared by workloads that claim it
instead of asking; urgent work can't make room, and there are no numbers for
when to rent. gipper turns GPU use into requests answered on a most-important-
wins basis (grants, or denials with reasons and alternatives, escalating to Marc
by email when work can't get done), runs jobs on owned machines or Runpod, and
records everything for Somm and Milton. Phase 0 (Runpod core) comes first to
unblock the Scares voice work.

**Mindset:** mvp | **Scale:** personal

---

## Epic: gipper-r0d — gipper #0: Runpod core (no daemon)

Priority: P0
Labels: phase-0, slice:runpod

Run a GPU job on a rented Runpod GPU safely from the CLI: price it, run it,
collect it, tear it down, record it.

### Task: gipper-r0d.1 — Package scaffold, secrets and ledger

Priority: P0
Labels: phase-0, model:sonnet, complexity:low, setup
Blocks: gipper-r0d.2, gipper-r0d.5

**Context**: Everything later writes receipts and reads the key; get both right once.

**Implementation Steps**:
1. `pyproject.toml` (Python 3.12, stdlib only, `gipper` entry point), `gipper/` package, argparse CLI with subcommand groups.
2. `gipper/secrets.py`: read `RUNPOD_API_KEY` from the environment or `~/Projects/gipper/.env`; never log, print or pass it on a command line.
3. `gipper/ledger.py`: append `gipper.receipt/v1` JSON lines to `~/.local/share/gipper/receipts/YYYY-MM.jsonl` (id, kind, ts, refs, payload).

**Acceptance Criteria**:
- [ ] `gipper --help` works from a `uv tool install -e .`
- [ ] A test proves the key never appears in ledger lines, logs or exception text
- [ ] `gipper ledger --since today` prints receipts written by a test command

**Files**: pyproject.toml, gipper/__init__.py, gipper/cli.py, gipper/secrets.py, gipper/ledger.py, tests/

---

### Task: gipper-r0d.2 — Runpod API client and fake

Priority: P0
Labels: phase-0, model:sonnet, complexity:medium, api
Blocks: gipper-r0d.3, gipper-r0d.6, gipper-r0d.8

**Context**: One small client over Runpod's GraphQL (prices, account) and REST (pods, volumes), testable without spending.

**Implementation Steps**:
1. `gipper/runpod/api.py`: `account()`, `gpu_types()` (id, memory, secure/community and spot prices), `create_pod(spec)`, `get_pod(id)`, `list_pods()`, `terminate_pod(id)`, `volumes()`.
2. `gipper/runpod/fake.py`: an in-memory fake with the same interface for tests.
3. Retries on transient HTTP errors; clear errors otherwise.

**Acceptance Criteria**:
- [ ] Live `account()` returns balance and spend limit with the real key
- [ ] `gpu_types()` returns at least the 4090, L40S and A100 with prices
- [ ] Fake-backed tests cover create, poll, terminate

**Files**: gipper/runpod/api.py, gipper/runpod/fake.py, tests/test_runpod_api.py

---

### Task: gipper-r0d.3 — `gipper pod gpus` and feasibility

Priority: P0
Labels: phase-0, model:haiku, complexity:low, api, quick-win
Blocks: gipper-r0d.6

**Context**: The first question is always "what fits and what does it cost".

**Implementation Steps**:
1. Table of GPU types: memory, on-demand and spot $/h, availability.
2. `--min-mem N` filter; mark the cheapest that fits.

**Acceptance Criteria**:
- [ ] `gipper pod gpus --min-mem 20` lists real types with live prices
- [ ] Output is used by `pod run` to pick a type (verified in r0d.6)

**Files**: gipper/cli.py, gipper/runpod/select.py

---

### Task: gipper-r0d.4 — Job spec format (shared with later executors)

Priority: P0
Labels: phase-0, model:opus, complexity:medium, model
Blocks: gipper-r0d.6

**Context**: Phase 2 wraps phase 0, so the job spec must already be the shared one.

**Implementation Steps**:
1. `gipper/job.py`: TOML job spec — name, workload, gpu {min_mem_gb, prefer}, image, volume, setup, command, inputs [{src, dst}], outputs [{src, dst}], checkpoint {dir, every_s}, budget {max_usd, max_hours}, sensitivity (owned-only blocks cloud), env.
2. Validation with clear messages; a `units` field for throughput.

**Acceptance Criteria**:
- [ ] Example specs for a voxhaul compare job and a library slice validate
- [ ] `sensitivity = "owned-only"` makes `pod run` refuse

**Files**: gipper/job.py, examples/*.toml, tests/test_job.py

---

### Task: gipper-r0d.5 — Image and network volume

Priority: P0
Labels: phase-0, model:sonnet, complexity:medium, setup, risk:medium
Blocks: gipper-r0d.6

**Context**: Without cached weights and environments every run pays a 5 GB download and a PyTorch install.

**Implementation Steps**:
1. Use a stock Runpod PyTorch image; a bootstrap script on the volume builds named venvs (e.g. `voxcpm`) once and reuses them.
2. `gipper pod volume init NAME --gb N --dc DC`; `gipper pod volume seed` runs a pod that fills the HF cache (VoxCPM2) and venvs, then terminates.

**Acceptance Criteria**:
- [ ] A second job on the same volume starts its command within ~3 minutes of pod creation
- [ ] Volume contents listed by `gipper pod volume ls`

**Files**: gipper/runpod/volume.py, gipper/runpod/bootstrap.sh

---

### Task: gipper-r0d.6 — `gipper pod run`

Priority: P0
Labels: phase-0, model:opus, complexity:high, logic, risk:high
Blocks: gipper-r0d.7, gipper-r0d.9

**Context**: The core lifecycle; money is at stake, so teardown is unconditional.

**Implementation Steps**:
1. Pick the GPU type, create the pod (name `gipper-<job>-<id>`, volume, ssh), wait for ssh.
2. rsync inputs; run setup and command over ssh with live logs to the terminal and a log file.
3. Periodically rsync the checkpoint dir; on finish rsync outputs.
4. `try/finally` terminate; receipts for start, spend (seconds x price), end, outcome.

**Acceptance Criteria**:
- [ ] A tiny job (nvidia-smi + write a file) round-trips and the pod is gone afterwards
- [ ] Ctrl-C mid-run still terminates the pod and writes an `interrupted` receipt
- [ ] Spend receipt within 10% of Runpod's own billing for the run

**Files**: gipper/runpod/run.py, gipper/ssh.py, gipper/cli.py

---

### Task: gipper-r0d.7 — Caps and watchdog

Priority: P0
Labels: phase-0, model:opus, complexity:medium, logic, risk:high
Blocks: gipper-r0d.9

**Context**: Runaway spend is the top risk.

**Implementation Steps**:
1. Per-job `max_usd`/`max_hours`: terminate when reached, receipt `capped`.
2. Daily cap from `~/.config/gipper/config.toml`, summed from the ledger before creating a pod.
3. In-pod deadline: the pod's command runs under `timeout` so a lost client can't leave it running past `max_hours`.

**Acceptance Criteria**:
- [ ] A job with `max_usd = 0.01` is stopped and receipted
- [ ] A new pod is refused when today's spend + estimate exceeds the daily cap

**Files**: gipper/runpod/run.py, gipper/config.py

---

### Task: gipper-r0d.8 — `ls`, `reap`, `cost`

Priority: P0
Labels: phase-0, model:sonnet, complexity:low, logic
Blocks: gipper-r0d.9

**Context**: Find and kill anything gipper started and forgot; know what it cost.

**Implementation Steps**:
1. `pod ls`: gipper pods with age and running cost.
2. `pod reap`: terminate `gipper-` pods with no live run in the ledger (or older than their max_hours); `--dry-run`.
3. `pod cost [--since]`: spend per job and per day from receipts.
4. A user timer (every 15 min) runs `reap`.

**Acceptance Criteria**:
- [ ] A pod created and abandoned on purpose is terminated by `reap`
- [ ] `cost` matches the spend receipts

**Files**: gipper/runpod/reap.py, deploy/gipper-reap.{service,timer}

---

### Task: gipper-r0d.9 — E2E: finish the Carla cadence experiment on a 4090 (checkpoint)

Priority: P0
Labels: phase-0, model:sonnet, complexity:medium, test, checkpoint
Blocks: gipper-r0d.10

**Context**: First real job; proves the path and gives real 4090 throughput.

**Implementation Steps**:
1. Job spec for voxhaul `workers.compare` over the remaining held-out and Scares scripts (sensitivity noted: show-derived reference clip and adapter, acceptable per Marc for private research).
2. Run; collect into `voxhaul/runs/carla-20261020/cadence/`; run words/prosody/report locally.

**Acceptance Criteria**:
- [ ] All remaining takes produced; report regenerated with full sets
- [ ] Receipts show seconds per take and dollars; `pod ls` empty afterwards

**Files**: examples/carla-cadence.toml

---

### Task: gipper-r0d.10 — Library slice calibration and docs

Priority: P1
Labels: phase-0, model:haiku, complexity:low, docs, test
Blocks: none

**Context**: Seed throughput for estimates; write down how to use phase 0.

**Implementation Steps**:
1. Run a 100-take voxhaul library slice on a 4090 (and an L40S if cheap); record units/s.
2. README: setup, specs, run, caps, reap, cost.

**Acceptance Criteria**:
- [ ] `gipper pod estimate` uses the recorded throughput for that workload
- [ ] README walk-through reproduces a run

**Files**: README.md, examples/voxhaul-library-slice.toml

---

## Epic: gipper-l1s — gipper #1: Local requests and leases

Priority: P0
Labels: phase-1, slice:requests

Work asks for GPU time on lisbon and gets a grant or a denial with reasons; urgent work pre-empts; everything paused comes back; stuck work escalates.

### Task: gipper-l1s.1 — Workload registry and declarations
Priority: P0 · Labels: phase-1, model:sonnet, complexity:medium, model · Blocks: gipper-l1s.3
Declarations (`workloads/*.toml`) for keel-research, gabe, ollama models, voxhaul-library, voxhaul-train. **AC:** `gipper workloads` lists them with importance, staleness, interruptibility; invalid declarations rejected.

### Task: gipper-l1s.2 — State DB (requests, leases, obligations, jobs, escalations)
Priority: P0 · Labels: phase-1, model:sonnet, complexity:medium, model · Blocks: gipper-l1s.3
SQLite WAL schema and repository; migrations. **AC:** round-trip tests; receipts written for every state change.

### Task: gipper-l1s.3 — Scheduler: effective priority, admission, backfill, pre-emption
Priority: P0 · Labels: phase-1, model:opus, complexity:high, logic, risk:high · Blocks: gipper-l1s.5
Most-important-wins with urgency aging and staleness ceilings; no reservations. **AC:** simulated scenarios (urgent vs Keel, staleness rescue, backfill) pass; decisions receipted with their terms.

### Task: gipper-l1s.4 — Grant/denial contract
Priority: P0 · Labels: phase-1, model:sonnet, complexity:medium, api · Blocks: gipper-l1s.7
Denials carry reason, holder, retry_after, queue_position, alternatives. **AC:** `gipper request` prints a grant or a denial with all fields; JSON output for scripts.

### Task: gipper-l1s.5 — gipperd and the local socket API
Priority: P0 · Labels: phase-1, model:sonnet, complexity:medium, api · Blocks: gipper-l1s.6
Daemon (systemd user service with watchdog), Unix-socket HTTP API per the architecture. **AC:** `gipper status` shows devices, leases, queue, open obligations.

### Task: gipper-l1s.6 — Managed consumers with restore obligations
Priority: P0 · Labels: phase-1, model:opus, complexity:high, logic, risk:high · Blocks: gipper-l1s.8
`systemd-timer` (Keel) and `ollama` (unload); obligations written before acting, replayed on start, verified. **AC:** pause drill on Keel restores the timer, including after killing gipperd mid-pause.

### Task: gipper-l1s.7 — gabe asks; escalation through roger
Priority: P0 · Labels: phase-1, model:sonnet, complexity:medium, logic · Blocks: gipper-l1s.8
`gipper request` at the top of `gabe/deploy/gabe-edition.sh`, adapt on denial; escalation email via rogerd plus an Open Loops entry, rate-limited. **AC:** a forced denial past gabe's limit sends one email and opens one loop.

### Task: gipper-l1s.8 — `gipper run` local executor; wire voxhaul (checkpoint)
Priority: P0 · Labels: phase-1, model:sonnet, complexity:medium, logic, checkpoint · Blocks: gipper-l1s.9
`gipper run --workload W -- CMD` via `systemd-run --user`; voxhaul GPU steps take a lease when gipperd is up (fallback `gpu.clear`). **AC (E2E):** a high-priority voxhaul job pauses Keel and unloads Ollama, runs, and Keel is verified restored.

### Task: gipper-l1s.9 — Docs and phase-1 retro
Priority: P1 · Labels: phase-1, model:haiku, complexity:low, docs · Blocks: none
README sections for requests, workloads, escalation. **AC:** a new workload can be declared from the README alone.

---

## Epic: gipper-c2x — gipper #2: Placement, estimates and executors

Priority: P1
Labels: phase-2, slice:placement

### Task: gipper-c2x.1 — ssh executor for toronto
Priority: P1 · Labels: phase-2, model:sonnet, complexity:medium · Blocks: gipper-c2x.3
rsync + nohup/pidfile, per-machine environments. **AC:** the phase-0 example job runs on toronto via `gipper submit --placement toronto`.

### Task: gipper-c2x.2 — Runpod executor under the scheduler
Priority: P1 · Labels: phase-2, model:sonnet, complexity:medium · Blocks: gipper-c2x.3
Wrap phase-0 `pod run` as an executor; leases on rented devices. **AC:** `gipper submit --placement runpod` goes through gipperd and is receipted identically to phase 0.

### Task: gipper-c2x.3 — Estimator: feasibility, wall, wait, dollars
Priority: P1 · Labels: phase-2, model:opus, complexity:high · Blocks: gipper-c2x.4
Throughput from receipts, queue-wait model, notional owned cost, bands from estimate-vs-actual. **AC:** `gipper estimate JOB` prints all placements; "no history" marked.

### Task: gipper-c2x.4 — `--placement auto` and calibration (checkpoint)
Priority: P1 · Labels: phase-2, model:sonnet, complexity:medium, checkpoint · Blocks: gipper-c2x.5
Auto placement from estimates and workload policy; calibration jobs on lisbon, toronto, 4090, L40S. **AC (E2E):** a voxhaul LoRA job placed automatically; estimate vs actual within ±25%.

### Task: gipper-c2x.5 — Retire hand-run GPU practices
Priority: P2 · Labels: phase-2, model:haiku, complexity:low · Blocks: none
Replace voxhaul `runs/*` systemd-run launches with `gipper submit`. **AC:** voxhaul README points to gipper; no new hand-run units.

---

## Epic: gipper-t3m — gipper #3: Somm and Milton

Priority: P1
Labels: phase-3, slice:telemetry

### Task: gipper-t3m.1 — Milton `gipper` adapter
Priority: P1 · Labels: phase-3, model:sonnet, complexity:medium · Blocks: gipper-t3m.4
Modelled on `chip.py`; cost (marginal for cloud, notional for owned), outcome, duration in attributes; added to `BUILTIN_ADAPTERS`. **AC:** `milton ingest` shows gipper runs with cost.

### Task: gipper-t3m.2 — Runs recorded in Somm
Priority: P1 · Labels: phase-3, model:sonnet, complexity:medium · Blocks: gipper-t3m.4
`calls` rows with `origin="gipper"`, real duration and cost basis. **AC:** runs appear in Somm stats for their workloads.

### Task: gipper-t3m.3 — Somm: scheduling metadata and admission phase (Somm change)
Priority: P1 · Labels: phase-3, model:opus, complexity:high, risk:medium · Blocks: gipper-t3m.4
`scheduling_json` on workloads; a deferral-capable admission step before Ollama dispatch that asks gipperd, fails open. Reviewed in Somm. **AC:** a Keel call during a high-priority lease defers or reroutes per policy.

### Task: gipper-t3m.4 — Gate Keel and gabe through Somm; retire Keel's timer steering (checkpoint)
Priority: P1 · Labels: phase-3, model:sonnet, complexity:medium, checkpoint · Blocks: gipper-t3m.5
**AC (E2E):** Keel resumes on its own after a lease ends; its cost visible in Milton.

### Task: gipper-t3m.5 — Remove voxhaul `gpu.py`
Priority: P2 · Labels: phase-3, model:haiku, complexity:low · Blocks: none
**AC:** voxhaul GPU steps use only gipper; `gpu.py` deleted.

---

## Epic: gipper-s4v — gipper #4: Divisible work, spot and replay

Priority: P2
Labels: phase-4, slice:efficiency

### Task: gipper-s4v.1 — Sharded jobs; farm.py as a client
Priority: P2 · Labels: phase-4, model:opus, complexity:high · Blocks: gipper-s4v.4
Split by estimated speed across lisbon, toronto and pods. **AC (E2E):** a farm render split three ways matches a local render.

### Task: gipper-s4v.2 — Spot pods with checkpoint resume
Priority: P2 · Labels: phase-4, model:sonnet, complexity:medium · Blocks: none
**AC:** a deliberately interrupted spot LoRA run resumes and finishes.

### Task: gipper-s4v.3 — Packing and idle teardown
Priority: P3 · Labels: phase-4, model:sonnet, complexity:medium · Blocks: none
**AC:** two queued jobs share one pod; the pod ends when idle.

### Task: gipper-s4v.4 — Policy replay
Priority: P3 · Labels: phase-4, model:opus, complexity:high, experiment · Blocks: none
Replay recorded requests under alternative policies. **AC:** report of missed deadlines, idle hours and cost per policy.

---

## Summary

| Epic | Tasks | Priority | Description |
|------|-------|----------|-------------|
| gipper-r0d | 10 | P0 | Runpod core, no daemon (runs first) |
| gipper-l1s | 9 | P0 | Requests, leases, pre-emption, restore, escalation |
| gipper-c2x | 5 | P1 | Placement, estimates, toronto and Runpod executors |
| gipper-t3m | 5 | P1 | Somm and Milton integration |
| gipper-s4v | 4 | P2 | Sharding, spot, packing, replay |

**Total:** 5 epics, 33 tasks
