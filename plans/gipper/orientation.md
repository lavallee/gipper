# Orient Report: gipper

**Date:** 2026-10-20
**Depth:** standard
**Status:** Approved (Marc, 2026-10-20)

---

## Executive Summary

The estate has two GPUs (lisbon's RTX 3060 12 GB, toronto's Apple M6 16 GB
unified) shared by scheduled workloads that don't coordinate, and no way to
make room for urgent ad-hoc work or to know when renting a cloud GPU is the
better trade. gipper schedules GPU work across owned machines with priorities
and pre-emption, treats rented GPUs as a first-class placement (for work the
3060 can't do at all, or does far slower or at a far higher total cost),
and leaves an accounting trail that Somm and Milton can read: what ran where,
for how long, at what cost, against what was estimated.

## Problem Statement

GPU capacity is the constraint. Scheduled and regular workloads (Keel's
research worker and gabe calling Ollama models; Blender farm renders; voxhaul
voicing and LoRA training) take lisbon and toronto without regard to each
other, and some aren't smart about it (Keel retries every two minutes, reloading
a 6 GB model the moment it is unloaded). Concretely, on 2026-10-19/20:

- TTS and Whisper jobs crashed out of memory under Ollama models loaded by gabe
  and Keel; Keel's own calls timed out in turn.
- Making room meant stopping Keel by hand; it is *still* stopped, because
  nothing records that it should come back.
- An urgent experiment waited ~3 hours behind a LoRA batch, or ran 3-4x slower
  on toronto (55 s vs ~15 s per VoxCPM take), when a rented 4090 for well
  under a dollar might have finished it in ~15 minutes. Nobody could say with
  confidence which option was better, because there are no estimates.

Who has it: Marc and the agents working for him, whenever ad-hoc GPU work
meets the standing load.

## Refined Vision

gipper is the estate's GPU job runner. A job is declared once (what to run,
what it needs, what it produces, how much work it is, whether it can be paused
and resumed). gipper then:

1. **Estimates** it per placement (lisbon, toronto, rented GPU types): wall
   time, queue wait, dollars, and the risk notes, from measured throughput.
2. **Schedules** it on owned GPUs through a **lease**: every GPU consumer,
   gipper's own jobs and the existing scheduled workloads, holds a lease with a
   priority. A higher-priority job can **pause or defer** lower-priority work
   (stop a timer, let a job checkpoint and stop, unload an Ollama model) and
   gipper **always restores** what it paused.
3. **Places work on rented GPUs** (Runpod first) as a first-class choice, not
   only as overflow: directly, when a job doesn't fit owned hardware (memory,
   compute) or when the estimate shows a large overall saving in time or
   resources; or as overflow when local capacity is busy and the estimate
   clears a policy (e.g. "finishes before local could start, under $X").
   gipper handles provisioning, sync, run, collection and guaranteed teardown.
   Mixed placement (some shards local, some rented) for divisible work.
4. **Accounts** for everything: leases, pauses, runs, spend and estimate-vs-
   actual go to an append-only ledger; Somm meters GPU work as a workload kind
   (and learns placement and throughput); Milton ingests the ledger as runner
   receipts for cost and outcome accounting.

Division of labour: **Somm** = entrypoint, placement policy, metering and
memory (via a `gpu` workload handler, the same mechanism Fab uses for `chip`);
**gipper** = lifecycle and scheduling; **Milton** = after-the-fact accounting
and findings. Somm's scope does not grow beyond the handler.

## Requests, not claims

Today a scheduled job owns the GPU because a timer fired. gipper inverts that:
**a workload asks for resources and is admitted, deferred or pre-empted
according to what it declared about itself.** This is a solved problem in
batch computing; we borrow rather than invent:

| Borrowed from | Idea we take |
|---|---|
| Kubernetes **Kueue** | *Admission is separate from execution*: work sits in a queue until admitted against capacity; priority classes; pre-emption of lower priorities |
| **Slurm** | Multi-factor priority (importance, age, size), `--begin`/`--deadline`, pre-emption modes (*suspend*, *requeue*, *cancel*), **backfill** (small jobs fill gaps without delaying bigger ones) |
| **HTCondor** | *Matchmaking*: jobs advertise requirements and preferences, machines advertise offers, a negotiator matches; checkpoint-and-vacate; cloud "glide-ins" as just more offers |
| **Leases** (Chubby, etcd) | Grants are time-bounded and renewed by a live holder; a dead holder's lease expires and is reclaimed, so nothing holds the GPU forever |
| Deadline scheduling (**EDF**) and **aging** | Urgency rises as slack to a deadline shrinks, so low-importance work can wait but never starves |
| **Time-of-use / carbon-aware** scheduling | Flexible work shifts to cheap windows (DeepSeek peak 01:00–04:00 and 06:00–10:00 UTC at 2× off-peak; owned GPUs idle overnight) |

### Workload metadata (declared once, per workload)

```toml
[workload.keel-research]          # Keel's research worker (scores sources with qwen3:8b)
needs        = { gpu_mem_gb = 6, backend = "ollama", model = "qwen3:8b" }
importance   = "low"              # critical | high | normal | low | best-effort
flexibility  = "anytime"          # anytime | window | deadline | interactive
max_staleness = "5d"              # may be deferred, but must get service within 5 days
service      = "until-queue-empty"
interruptible = { grace = "2m", resume = "task" }   # finishes or abandons the current task
windows      = { prefer = ["night-local"] }
placement    = "owned-only"       # owned-only | cloud-ok | cloud-preferred
```

From these, gipper computes an **effective priority** = importance + urgency
(slack to deadline or staleness limit) + age, admits work into **leases**
(time-bounded, renewable, revocable with the declared grace), pre-empts lower
effective priorities when something more urgent asks, and backfills small
flexible jobs into gaps. Keel can be off for days; as its staleness limit
nears, its urgency rises until it is admitted ahead of other low work. An
ad-hoc job asks the same way (`importance = high`, `flexibility =
interactive`) instead of someone stopping services by hand.

**Where the metadata lives:** on the workload record that Somm already keeps,
so one declaration drives both GPU scheduling (gipper) and LLM flow control
(Somm deferring a flexible DeepSeek workload out of peak hours, or routing it
to a local model when the GPU is free). gipper owns time and capacity on GPUs;
Somm owns price and model knowledge; both read the same intent.

### How existing jobs move over (no big-bang rewrite)

1. **Wrapped**: a timer's command becomes `gipper run --workload NAME -- CMD`,
   which waits for admission before running. No code change in the job.
2. **Gated at Somm**: GPU-backed LLM calls (Keel's and gabe's Ollama traffic
   already passes through Somm) consult gipper's admission before dispatch;
   when not admitted, Somm defers or falls back per the workload's policy. One
   enforcement point covers every Ollama caller, including in-process workers
   a wrapper can't reach.
3. **Cooperative**: jobs that care (voxhaul, the farm) call the lease API
   themselves, renew, and checkpoint on revocation notice.

### Benchmark for future Somm

Every request, decision (admit, defer, pre-empt), outcome and dollar is in the
ledger, so policies can be **replayed** against real demand: how many deadlines
missed, GPU idle hours, cost per workload, staleness at service. That is the
evidence base for smarter flow control later, on GPUs and on hosted LLMs.

## Requirements

### P0 — Must Have
- **GPU inventory and lease** on lisbon and toronto: capacity, current holders,
  priority. One mechanism all consumers go through (or are wrapped by).
  *Why:* the OOMs come from uncoordinated use.
- **Workload metadata and requests**: every workload declares importance,
  flexibility, deadlines or staleness limits, interruptibility, windows and
  placement (above); runs are admitted by request, not by timer. *Why:* the
  core shift from claims to requests.
- **Effective priority with aging and deadlines**: low work waits but cannot
  starve past its staleness limit. *Why:* Keel can be off for days, not forever.
- **Managed consumers**: a registry of the standing workloads (Keel worker,
  Ollama-loaded models, gabe, farm renders, voxhaul) with how to pause, defer,
  resume and verify each. *Why:* we can't change every consumer at once;
  gipper has to steer the ones that exist.
- **Priorities and pre-emption with guaranteed restore**: an ad-hoc job can
  displace lower-priority work; every pause has a restore that runs on success,
  failure and gipper crash (dead-man), and is visible until done. *Why:* the
  Keel-stays-stopped failure.
- **Job spec and owned-machine runs**: inputs, command, environment (venv or
  image), outputs, resources, units of work, resumability; runs on lisbon
  locally and toronto over ssh, with logs and collected outputs.
- **Estimates before running**: per placement, *feasibility* first (does it
  fit: GPU memory, compute capability, environment), then wall time + wait +
  cost (+ setup overhead), from recorded throughput per (workload, machine) in
  its own units (steps, takes, frames); an explicit "no history" estimate when
  there is none. Owned-machine time is costed too (displaced work, wall time),
  so local vs rented is a real comparison, not free vs paid. *Why:* the
  decision to wait, run locally or rent needs numbers.
- **Rented GPUs, on demand (Runpod)**: choose a GPU type that fits the job,
  provision, sync, run, collect, teardown that cannot be skipped, per-job and
  global budget caps, an orphan reaper. *Why:* some work needs more than the
  3060 (memory or speed), and some is simply a better trade in the cloud.
- **Ledger and telemetry**: every lease, pause, restore, run, estimate and
  dollar recorded with estimate-vs-actual; readable by Somm (metering,
  throughput learning) and Milton (receipts). *Why:* we want to know what
  actually happened and what it cost.

### P1 — Should Have
- **Divisible jobs across placements** (frames, lines, takes): split by
  estimated speed, run local and rented shards together (generalises farm.py).
- **Spot / interruptible rentals with checkpoint sync and resume** (LoRA
  training and library voicing already resume).
- **Efficiency**: cached model weights and images per provider (no 5 GB
  download per job), right-sizing the GPU type from the estimate, idle
  teardown, packing several queued jobs onto one rented machine.
- **Somm admission gate for GPU-backed LLM calls** (Ollama traffic from Keel,
  gabe) and shared workload metadata with Somm's flow control (price windows).
- **Policy replay**: simulate scheduling policies against recorded demand.
- **Somm placement recommendations** from history (e.g. "voxcpm-library: rent
  a 4090 when the local wait exceeds 40 min").
- **Agent-facing surface**: CLI first, then a skill/MCP so agents estimate and
  submit instead of hand-running systemd-run and ssh (today's pattern).
- **Calibration jobs**: a small standard benchmark per workload kind to seed
  throughput on a new machine or GPU type.

### P2 — Nice to Have
- Scheduled/recurring workloads on rented GPUs (not planned, but the job spec
  and placement must not preclude it).
- Additional providers (Modal for serverless inference). Vast.ai: done (0.1–0.2).
- A status page (queue, leases, spend) beyond the CLI.

## Constraints

- **Owned by default for standing work; cloud first-class for jobs that call
  for it.** Somm's sovereignty principle (no commercial dependency on the hot
  path) holds for the estate's standing workloads: nothing scheduled depends on
  a rented GPU to function. Ad-hoc and heavy jobs may go straight to the cloud
  when they don't fit owned hardware or the estimate shows a large saving.
- **Two heterogeneous machines**: CUDA (lisbon, 12 GB) and MPS (toronto, 16 GB
  unified, shared with the desktop); environments differ per machine.
- **Some consumers need root to pause**: gabe and Ollama are system units;
  `sudo` needs a password. Pausing them must use what a user can do (unload a
  model through Ollama's API, stop user timers) or a narrowly scoped
  privilege granted once.
- **Secrets**: the Runpod key lives in `gipper/.env` (mode 600, git-ignored)
  and is read at run time; never logged, printed or written elsewhere.
- **Python, CLI-first**, matching voxhaul and the estate.
- **Somm and Milton keep their scopes**: Somm gets a handler; Milton gets an
  adapter over the ledger. No lifecycle logic in either.

## Assumptions

- GPU work here is batch (minutes to hours), not latency-sensitive serving.
- Workloads can state their work in units (steps, takes, frames), so
  throughput, estimates and splitting are possible.
- One operator (Marc and his agents); no multi-tenant fairness.
- Standing workloads tolerate being deferred for an ad-hoc job's duration
  (hours, not days) if they are reliably restored.
- Rented-GPU data exposure is acceptable for designed voices and code;
  sensitive inputs (e.g. show audio) are flagged per job.

## Open Questions / Experiments

- Which GPU types are worth keeping in the menu (4090, L40S, A100 80 GB, H100)
  for our workloads → calibration below plus price snapshots; feasibility rules
  (e.g. full-batch VoxCPM2 LoRA ~20 GB) recorded per workload.
- Real cloud throughput and setup overhead → **calibration run**: voxhaul LoRA
  (600 steps) and a 100-take library slice on a Runpod 4090 and an L40S,
  measured against lisbon. Budget ~$1.
- How each standing workload behaves when paused (does Keel lose a task mid-way;
  does gabe miss an edition?) → **pause drills** per managed consumer, observed
  in the ledger, before pre-emption is automatic.
- How accurate estimates must be to be trusted → target ±25% on wall time and
  cost for workloads with history; **track estimate-vs-actual** from day one
  and revisit.
- Which effective-priority formula and staleness limits feel right → start
  with simple weights, **replay** a week of recorded demand, tune.
- Whether gabe and Keel should call the lease themselves (cooperative) or be
  steered from outside → start external, measure friction, then ask each
  project's owner.
- Spot interruption rate on Runpod for our job lengths → measure during P1.

## Out of Scope

- Serving or inference endpoints, autoscaling, request routing for live traffic.
- Cluster managers (Kubernetes, Slurm), multi-user scheduling.
- Replacing Somm's LLM routing or Milton's analysis.
- Buying hardware or tuning models; gipper runs what it is given.

## Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Orphaned rented machines run up spend | H | Teardown in `finally` + reaper that lists provider machines against the ledger; per-job and global caps; account-level spend limit |
| Paused workloads never restored (today's Keel) | H | Restore as a ledger obligation with dead-man timer; `gipper status` shows open pauses; drills before automation |
| Estimates wrong, so decisions are wrong | M | "No history" made explicit; calibration jobs; estimate-vs-actual in every record |
| Scope creep into Somm or Milton | M | Handler and adapter only; lifecycle stays in gipper |
| Heterogeneous environments (CUDA/MPS/cloud images) break jobs | M | Environments declared per job and per machine; a smoke step before long runs |
| Sensitive data on third-party GPUs | M | Per-job sensitivity flag; owned-only placement for flagged jobs |
| Root-owned consumers can't be paused | M | Use user-level controls (Ollama API unload); ask for one scoped sudoers rule only if drills show it's needed |

## MVP Definition

A CLI that can:
1. take **workload declarations** (importance, flexibility, staleness or
   deadline, interruptibility, placement) and turn runs into **requests**:
   `gipper run --workload NAME -- CMD` waits for admission;
2. hold **leases** on lisbon's GPU with effective priority (importance +
   urgency + age), pre-empting lower work with its declared grace and always
   restoring it; first managed consumers: Ollama models, Keel's research
   worker (wrapped, `max_staleness` honoured), gipper's own jobs;
3. **run a declared job** on lisbon, toronto or a Runpod on-demand GPU, with
   logs, outputs and guaranteed teardown;
4. **estimate** that job per placement, feasibility first, from recorded
   throughput (or say there is none);
5. write every request, decision, lease, pause, restore, run, estimate and
   dollar to a **ledger** Somm and Milton can read.

First real job: voxhaul LoRA training, run once on each placement as the
calibration that seeds the estimates.

---

**Next:** run `forge-architect` for technical design.
