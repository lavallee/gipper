# How gipper chooses an offer

Code: [`gipper/costs.py`](../gipper/costs.py) (the estimate and score),
[`gipper/history.py`](../gipper/history.py) (memory), [`gipper/market.py`](../gipper/market.py)
(filters and order), [`gipper/placement.py`](../gipper/placement.py) (the job's hints).

## The order

1. **Filters.** Enough GPU memory; inside `[gpu] regions`; UDP if a port asks
   for it; at or under `max_usd_per_h`; expected cost within `[budget] max_usd`;
   and any `[placement]` floor the provider advertises
   (`min_host_download_mbps`, `min_host_upload_mbps`, `min_cuda`,
   `min_compute_capability`). A floor the provider doesn't advertise passes
   the filter and is checked on the machine itself. Runpod doesn't advertise
   compute capability, so a Runpod GPU below `min_compute_capability` is rented
   and then rejected; use `prefer` or `providers` to avoid that.
2. **`prefer`**, in the job's order. GPUs it doesn't list come after all the
   ones it does.
3. **Known stock first.** Runpod GPU types without reported stock mostly fail to
   rent; Vast and Shadeform offers are specific machines, always in stock.
4. **Score**, lowest first.
5. The provider order in settings, as a tie-break.

## The estimate

```
wall     = ready + setup + upload + fetch + compute + download
expected = wall x $/h x billing factor + bandwidth fees
risk     = p_fail x (failure_cost_usd + (ready + 60 s) x ($/h + hour value))
score    = expected + risk + hour value x wall
```

| Figure | First choice | Then | Then | Default |
|---|---|---|---|---|
| ready | memory: median for this machine | median for the provider | | Runpod 90 s, Vast 120 s, Shadeform 420 s |
| setup | `placement.setup_s` | | | 120 s |
| upload | `placement.upload_gb` (else the size of `[[inputs]]`) over the measured link to this machine | to the provider | advertised host download × `transfer_efficiency` | 100 Mb/s × efficiency |
| fetch | `placement.setup_download_gb` over the advertised host download × efficiency | | | 100 Mb/s × efficiency |
| compute | `compute_s_by_gpu` for this GPU | `--throughput` with `units.count` | `placement.compute_s` | memory: median for this workload on this GPU, else 30 min (capped at `max_hours`) |
| download | `placement.download_gb` over the measured link back | | advertised host upload × efficiency | 100 Mb/s × efficiency |
| bandwidth | Vast's advertised $/GB in and out | | | Runpod: none charged. Vast without advertised fees: 0, flagged `bandwidth_known: false` |
| billing factor | memory: billed ÷ computed for the provider, totals over rentals of 10 minutes or more (shorter ones mostly measure rounding) | | | 1.0 |
| p_fail | the machine's record, weighted 2 rentals towards the provider's rate | the provider's record, weighted 4 rentals towards 10% | | 10% |

"Measured" links come only from timed real transfers of 20 MB or more (inputs
going up, outputs coming down when the job doesn't checkpoint them). The optional
probe never counts: a 2 MiB probe mostly measures ssh start-up, so it reads low.

`gipper pod estimate JOB` prints the phases, the dollars, the risk and, under
"from", where each figure came from. The `estimate` receipt records all of it.

## Objectives

| `placement.objective` | Hour of waiting worth | Use for |
|---|---|---|
| `auto` (default) | `hour_value_usd`, $3 | most work |
| `cost` | $0 (failures still cost) | batch work nobody is waiting on |
| `time` | $50 | someone is waiting |

Settings live in `~/.config/gipper/config.toml`: `hour_value_usd`,
`failure_cost_usd` ($1) and `history_days` (90).

## Worked example

Memory on 2026-10-28: 82 Runpod rentals (81 good) and 27 Vast rentals (18 good,
6 bad); billing factors ×1.01 Runpod and ×1.10 Vast. Take a job with 15 minutes
of compute, a Vast 4090 at $0.40/h against a Runpod 4090 at $0.69/h (output of
`costs.estimate` with that memory):

| | wall | expected | risk | score |
|---|---|---|---|---|
| Vast, 15 min | 18 min | $0.13 | 23% → $0.25 | 1.27 |
| Runpod, 15 min | 18 min | $0.21 | 0.5% → $0.005 | **1.14** |
| Vast, 10 h | 10 h | $4.41 | $0.25 | **34.80** |
| Runpod, 10 h | 10 h | $6.98 | $0.005 | 37.16 |

The short job goes to Runpod by 13 cents, and the long one to Vast by $2.35. A
Vast machine with its own good record (9% after a few clean runs) narrows the
gap on short jobs too.

## Memory

The ledger is the memory (`~/.local/share/gipper/receipts`), read back over
`history_days`. `gipper pod hosts` shows it per provider and per machine. What
counts:

- **good:** `run.start` on the machine.
- **bad:** a broken host: `host.reject` for a busy GPU, CUDA that won't
  initialise, ssh that never came up, a machine that died while starting, or a
  failed probe; or a connection lost before or during the job.
- **neither:** a host that works but doesn't suit the job (`run.Unsuitable`,
  e.g. compute capability below the floor, or a client's runtime check); an
  error on this side (a local file that won't upload); a job that exits non-zero
  on a working host; a market with no stock. Rejects written before 0.2 have no
  `kind` and are classified by their message.
- **transfer speed:** `transfer` receipts (bytes and seconds of real uploads and
  downloads).
- **compute time:** `run.end.compute_s` from jobs that succeeded (older receipts:
  run.start → collect). Capped, failed or interrupted runs teach nothing about
  duration.
- **billing factor:** `spend.billed` (from `gipper pod reconcile`, which the reap
  timer runs) against `spend`.

A connection lost mid-run is only noticed when it ends the run; while the log
poll can't reach the host, gipper keeps waiting until the job's cap.

## The optional probe

```toml
[placement]
probe_transfer_mb = 16          # MiB each way through the job's own ssh, before inputs
probe_timeout_s = 120           # total, at most 300
min_measured_upload_mbps = 10   # reject the host below these
min_measured_download_mbps = 10
```

It costs the rental time it takes, and a host it rejects is replaced like any
other bad host. Use it when inputs are large and a slow path would waste the run.
Its readings gate the host but don't feed memory: the timed real upload does
that, for free.
