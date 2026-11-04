# gipper: notes for agents

gipper rents GPUs (Runpod, Vast.ai; Shadeform wired in but unused), runs a job,
brings results home, gives the machine back, and records everything in an
append-only ledger. Read [README.md](README.md) for what it does and
[docs/placement.md](docs/placement.md) for how it chooses. This file covers
working *on* gipper.

## Before you change anything

- **Protect running clients.** Editable installs import directly from their
  checkout. Check for active runs before editing a shared installation:
  `pgrep -af "gipper pod run|cloud.py|runner.run"`. Use a git worktree on a
  branch for non-trivial changes, and integrate when no clients are running.
- **Keep the client surface stable** ([docs/clients.md](docs/clients.md)):
  `run.run`, `run.plan`, `run.estimate`, `run.preflight`, `run.settings`,
  `run.BadHost`, `job.load`, `providers.load(...)` with its methods, `ledger`,
  `ssh.Remote`, and the `[placement]` fields client jobs use. Clients can wrap
  `run.preflight`, `run.plan`, and `run.settings`; `run.py` must keep looking
  these up by module-global name at call time.
- **Run the tests:** `uv run --extra test pytest -q` (no network or rentals).
  Use `uv run` to select the project's Python environment.

## Invariants

1. **A rented machine is always given back, and only counted given back when
   the provider stops listing it** (`lifecycle.terminate`). Teardown is in
   `finally`. Never rent a replacement while a previous machine's closure is
   unconfirmed.
2. **Keys never leave a request header.** Not in argv, logs, receipts or errors;
   `config.redact()` scrubs every provider key, and `http.request` applies it to
   error bodies. Jobs' `[env]` refuses names ending in KEY, TOKEN or SECRET.
3. **The ledger is append-only and is the memory.** Never rewrite receipts. New
   facts are new receipt kinds or new payload fields, and readers must tolerate
   old receipts without them: receipts before 2026-10-26 have no `provider` and
   are Runpod's; before 0.2 there's no `machine` on `pod.create`, no `compute_s`
   on `run.end` and no `kind` on `host.reject`.
4. **Memory must not blame a host for what isn't its fault.** Job-specific
   rejects raise `run.Unsuitable`; only broken hosts and lost connections count
   as bad; compute time comes only from successful runs; link speed comes only
   from real transfers of 20 MB or more; the billing factor only from rentals of
   10 minutes or more. A wrong memory steers every later run.
5. **Never touch a machine not named `gipper-*`**, and no provider resource
   gipper didn't create.
6. **One provider failing never stops the others** (shopping, reaping, reconciling).
7. **Stdlib only.** No runtime dependencies.

## Map

| File | Job |
|---|---|
| `job.py`, `placement.py` | the job file and its `[placement]` hints |
| `market.py` | `Offer`, GPU-name matching, filters (`admit`), order (`rank`), `shop` |
| `costs.py` | the whole-run estimate and score for one offer |
| `history.py` | memory, read from the ledger: failure rates, ready times, link speeds, compute times, billing factors |
| `run.py` | the run: plan, rent, preflight, upload, detached job, log, checkpoints, collect, teardown |
| `lifecycle.py` | verified teardown |
| `reap.py` | the 15-minute sweep for forgotten machines |
| `billing.py` | providers' bills → `spend.billed`; spend for the daily cap |
| `cuda_probe.py`, `network_probe.py` | host checks before upload |
| `providers/{runpod,vast,shadeform}.py` | one module per market behind the interface in `providers/__init__.py` |
| `runpod/` | import paths kept for pre-0.2 scripts only |
| `http.py`, `ssh.py`, `ledger.py`, `config.py` | plumbing |

## Adding a provider

Implement the interface documented in `providers/__init__.py`. Map locations to
ISO country codes, memory to decimal GB, and advertised link speeds (Mb/s) and
fees ($/GB) into `Offer.metrics` under the names `costs.py` reads. Set `udp`
honestly. Implement `billed()` if the provider has a billing API. Add it to
`providers.NAMES` and `config.DEFAULTS["providers"]`. Test the parsing with
response shapes captured from the real API. Then do a real smoke run
(`examples/smoke.toml --provider <name>`) and, if it claims UDP,
`examples/udp-echo.toml` plus a packet sent from here, and write what you learn
in `docs/lessons.md`.

## Tests

Test decisions and parsing, not plumbing: what gets chosen, what counts as a bad
host, what gets billed, what a job file accepts, what the reaper ends. Build
histories from receipts (`tests/conftest.py: receipt`), and providers' answers
from real response shapes. Don't build tests that mock `run.run`'s internals
end to end: an explicitly authorized live smoke run qualifies that path separately.

## Spending money

A real rental needs the user's go-ahead unless the task already asked for one.
Use `--dry-run` and `gipper pod estimate` first, keep `[budget]` tight, and check
`gipper pod ls` is empty afterwards.
