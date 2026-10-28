# Changelog

## 0.2.0 (2026-10-28)

Offers are judged on the whole run, and gipper learns from its own rentals.

- **Whole-run estimate and score for every job** (`costs.py`): ready, setup,
  upload, the job's own downloads, compute and output download; rental plus
  Vast's bandwidth fees, times the provider's billed-to-computed factor; plus the
  chance of a bad host and the value of waiting. Short jobs favour reliable,
  fast hosts and long ones favour price. Replaces the opt-in `[placement]`
  profiles from 0.1; the fields client jobs used still load.
- **Memory from the ledger** (`history.py`, `gipper pod hosts`): host failure
  rates per provider and per machine, ready times, measured link speeds, compute
  time per workload and GPU, billing factors.
- **Billed spend** (`billing.py`, `gipper pod reconcile`, run by the reap timer):
  Vast's itemised charges and Runpod's per-pod billing become `spend.billed`;
  the daily cap and `gipper pod cost` use them, attributed to the machine's own day.
- **New receipts:** `transfer` (timed real uploads and downloads),
  `host.requirements`; `pod.create` gains `machine`, `run.end` gains
  `compute_s`, and `host.reject` gains `kind`.
- **`run.Unsuitable`** (a `BadHost`): rejects a host that works but doesn't suit
  the job, without counting it against the host in memory.
- **Runpod:** GPU types its REST API won't create are skipped instead of ending
  the run; `[placement] min_cuda` becomes `allowedCudaVersions`.
- **Vast:** advertised speeds, fees, CUDA and compute capability on every offer;
  memory in decimal GB, so nominal 48 GB cards qualify; more machines per GPU
  model are considered.
- **Kept from fodder's 0.1 additions:** verified teardown, the `cuInit` check
  with its 804 retry, and the optional transfer probe.
- **Removed:** the default 2× hourly ceiling (the score prices the run; an
  explicit `[gpu] max_usd_per_h` still caps it), placement's latency and
  geography fields (unused), and `docs/workload-placement.md` (now
  `docs/placement.md`).
- **Docs:** README, `docs/placement.md`, `docs/clients.md`, `docs/lessons.md`,
  `AGENTS.md`.

## 0.1.0 (2026-10-20 to 2026-10-27)

Rented GPUs from the CLI: Runpod, then Vast.ai and Shadeform behind one
interface; regions, UDP ports, preflight and host replacement, caps, reaper,
receipts.
