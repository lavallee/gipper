# Changelog

## 0.2.2 (2026-10-28)

Round trips are measured and remembered; geography only steers work that needs it.

- **Interactive jobs ping candidate hosts before renting** (`probe_rtt`, on by
  default for `profile = "interactive"`): the 48 cheapest offers' public IPs,
  from this machine, in about 10 seconds. About 60% of Vast hosts answer.
- **Round-trip order of trust:** the job's own measurement from `latency_target`,
  then a ping from here, then memory of that machine, then the median for its
  country, then `preferred_countries`. Pings from here pass `max_rtt_ms` only
  with `latency_target = "here"`; otherwise they rank but don't qualify.
- **Every rental records `host.rtt`** (fastest of 5 TCP connects to its ssh
  port), and `pod.create` records the offer's `country`. Shops for interactive
  jobs record `market.rtt`.
- **Link-speed memory is per country** before per provider, so far hosts' slower
  transfers are priced from experience.
- **Batch jobs ignore geography:** a cheap host on the far side of the world wins
  whenever the whole run costs less.

## 0.2.1 (2026-10-28)

Everything from the 0.1 placement work (the `codex/workload-placement-20261027`
branch) is available again, on top of 0.2's model:

- **`profile = "interactive"` with `objective = "latency"`:** offers rank by
  measured round trip (`measured_rtt_ms_by_machine`, `latency_target`), then
  `preferred_countries`, then distance (`near_latitude`/`near_longitude`), then
  the usual score. `max_rtt_ms` and `max_distance_km` fail closed when nothing
  was measured.
- **Restored fields:** `min_dlperf` (Vast's DLPerf as a filter),
  `client_upload_mbps` and `client_download_mbps` (caps on this end's link),
  `fallback_mbps`.
- **0.1 names in every estimate:** `estimated_total_usd`,
  `estimated_bandwidth_usd`, `known_bandwidth_usd`, `bandwidth_fees_known`,
  `cost_is_lower_bound`, `setup_download_s`, `upload_s`, `download_s`,
  `compute_s`, `dlperf`, `assumptions` and the rest, so reports that read
  estimate receipts keep their fields. `offer.placement_estimate`,
  `placement.estimate_offer` and `placement.rank_for_job` work again.
- **Vast doc captures** behind the placement docs are in `docs/sources/vast/`.

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
  explicit `[gpu] max_usd_per_h` still caps it), and `docs/workload-placement.md`
  (now `docs/placement.md`). Placement's latency and geography fields were
  removed here and restored in 0.2.1.
- **Docs:** README, `docs/placement.md`, `docs/clients.md`, `docs/lessons.md`,
  `AGENTS.md`.

## 0.1.0 (2026-10-20 to 2026-10-27)

Rented GPUs from the CLI: Runpod, then Vast.ai and Shadeform behind one
interface; regions, UDP ports, preflight and host replacement, caps, reaper,
receipts.
