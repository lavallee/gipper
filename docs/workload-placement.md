# Opt-in workload placement

Existing jobs without `[placement]` retain the original GPU preference/hourly ranking and estimate. Explicit profiles rank by estimated rental cost or elapsed time, preserving GPU preferences as tie breakers. Search remains bounded to the provider's first 200 filtered offers; this is not a global optimizer.

Example for short audio training (measurements must be updated for the actual job):

```toml
[placement]
profile = "batch"
objective = "cost"
setup_s = 420
setup_download_gb = 5
upload_gb = 0.09
download_gb = 0.3
compute_s = 900
min_host_download_mbps = 500
min_host_upload_mbps = 100
transfer_efficiency = 0.5
[placement.compute_s_by_gpu]
A40 = 400
```

All GB are decimal (1e9 bytes); Mbps are megabits/s; times are seconds. `setup_s` covers fixed cold start, SSH, installer/resolver overhead, excluding explicitly estimated traffic. `setup_download_gb` covers external pip/Hugging Face dependencies and weights, and uses advertised host download bandwidth. `upload_gb` is client→host input traffic; omit it to count local regular input files, approximating rsync exclusions. `download_gb` is host→client output traffic. Optional `client_upload_mbps` and `client_download_mbps` cap only client transfers. All network estimates multiply limiting bandwidth by `transfer_efficiency`; neither advertised bandwidth nor this heuristic measures the actual SSH route, compression, server throttling or path goodput.

`compute_s_by_gpu` uses the existing GPU name matcher and explicit job-specific duration estimates. Unmatched GPUs use `compute_s`, labeled unmeasured. DLPerf is a provider generic score, reported and optionally filtered via `min_dlperf`; it is never converted into this workload's throughput. `min_compute_capability` (e.g. 8.0) and `min_cuda` (e.g. 12.8) are optional hard filters. Missing hard-filter metrics reject offers. Missing unfiltered network metrics use `fallback_mbps` (100 by default), explicitly recorded. Cost includes advertised bandwidth rates when available and excludes taxes and retry costs; `dph_total` includes the requested disk allocation on Vast. Estimates are assumptions, not guarantees. Runtime enforcement and spend receipts remain rental-time based; forecast bandwidth does not establish actual invoice charges. External guards must retain headroom for traffic charges.

Vast profile searches keep all returned same-GPU hosts for bandwidth-aware comparison. Only explicit profiles correct Vast's raw MiB memory to decimal GB, allowing nominal 48GB A40 cards with ~46068MiB to qualify at 48 decimal GB. Legacy jobs retain their original memory admission behavior.

Interactive example:

```toml
[placement]
profile = "interactive"
objective = "latency"
preferred_countries = ["CA", "US"]
latency_target = "toronto-unreal-host"
max_rtt_ms = 50
[placement.measured_rtt_ms_by_machine]
"vast:machine:12345" = 28
```

RTT entries must be actual measurements from the named client origin to that provider-qualified machine. No automatic probing or freshness verification is performed. Country preference and optional `near_latitude`/`near_longitude`, `max_distance_km` are geographic proxies, never inferred RTT; unavailable coordinates fail a hard distance filter. Geographic preferences affect only the latency objective, not batch cost/time. A maximum RTT with no corresponding measurement fails closed. Country selection without measurements is only a proximity hint. For real-time Unreal, measure the actual path before relying on it.

`gipper pod estimate JOB --provider vast` prints phase breakdowns and assumptions. Run/dry-run estimate receipts include the same breakdown, offer metrics and provider reference. Explicit CLI `--throughput` overrides estimate compute duration when units are set, while ranking uses the profile's compute map. No rental is created by the estimate command. Current running rentals are unaffected by profile edits.

A bounded pre-upload measured network test is intentionally separate future work: the current profile cannot establish client-path throughput from provider bandwidth alone. Do not cancel a progressing rental merely because an estimate proves optimistic.

Primary references: [Vast offer schema](https://docs.vast.ai/api-reference/search/search-offers), [official CLI raw field handling](https://github.com/vast-ai/vast-cli/blob/master/vast.py), [Vast billing: bandwidth distinct from rental](https://github.com/vast-ai/docs/blob/main/guides/reference/billing.mdx), [choosing instances](https://docs.vast.ai/guides/instances/choosing/overview). Provider metrics are advertised host observations, not independent workload benchmarks.

## Optional measured transfer check before inputs

```toml
# Add under [placement]. Off unless probe_transfer_mb is nonzero.
probe_transfer_mb = 16
probe_timeout_s = 120
min_measured_upload_mbps = 10
min_measured_download_mbps = 10
```

Despite the compact field name, `probe_transfer_mb` means **MiB (1024² bytes) per direction**, bounded to 1–64 MiB (0 disables). A private random file is uploaded and downloaded through the same `Remote` SSH endpoint before bootstrap or job inputs. Its SHA-256 roundtrip must match. Each directional goodput includes SSH startup, encryption and local file I/O; receipts identify the actual client hostname and destination. It is neither RTT nor a general bandwidth benchmark. No public bandwidth endpoint or secret input is used.

The total `probe_timeout_s` deadline (at most 300s) includes both directions and best-effort remote cleanup, with time reserved for cleanup. Timed-out SSH children are killed and waited for; temporary local files are removed. A remote download shell traps exit/signals to remove its random file; cleanup success is reported separately when the host becomes unreachable. Unknown/failed measurements reject the host, as do configured minimum failures, through the existing maximum-three-host replacement loop. Probe traffic and rejected-host rental time cost money; existing outer job guards and rental teardown still apply. Every result is saved as `host.network_probe` before input uploads. A passing check does not guarantee sustained throughput later. This opt-in check never changes a running rental.

## Actual CUDA driver check before setup

Every rented GPU now must pass an early stdlib Python `ctypes.CDLL("libcuda.so.1")`, `cuInit(0)` and `cuDeviceGetCount` check in addition to `nvidia-smi`. NVML success alone does not establish CUDA initialization. This happens before transfer probing, bootstrap, inputs or model dependencies, with a 30-second limit per subprocess. Missing Python/driver, initialization failure or zero devices rejects the host.

Only CUDA error 804 permits one additional fresh Python process after removing explicit CUDA compatibility entries from that process's `LD_LIBRARY_PATH`. No native driver directory is guessed, packages changed or system configuration edited. The retry must actually initialize and enumerate devices under the requested cleaned environment; only then does the selected job receive that exact `LD_LIBRARY_PATH` override. Normal successful hosts retain their environment. `host.cuda_probe` receipts record the initial and retry outcomes, loaded libcuda mappings when available, removed entries and the applied job override; no complete environment or secrets are captured. NVIDIA documents [804 and forward compatibility package path selection](https://docs.nvidia.com/deploy/cuda-compatibility/forward-compatibility.html).

## Advertised transfer fees in placement

Vast offer `inet_down_cost` and `inet_up_cost` are host inbound/outbound prices in dollars per GB, mapped to `host_download_usd_per_gb` and `host_upload_usd_per_gb`. Incoming traffic is `upload_gb + setup_download_gb + probe_bytes/1e9`; outgoing is `download_gb + probe_bytes/1e9`. Each is multiplied by its corresponding advertised rate. Probe bytes are included but probe wall time remains unmeasured extra overhead.

`rental_usd` stays separate; `estimated_bandwidth_usd` plus rental yields `estimated_total_usd`, also exposed as `usd` and used by cost ranking. If either rate is missing/invalid, `estimated_bandwidth_usd` is null, `bandwidth_fees_known` is false and `cost_is_lower_bound` is true. `known_bandwidth_usd` contains only forecast charges whose rates are known. The ranking total is then a **lower bound**, not an invented zero price for unknown traffic. This can favor missing-price offers, so inspect the explicit warnings. Estimates and receipts never claim actual billed traffic or synthesize bandwidth spend records. Compression, hidden dependency traffic, container pulls, retries, taxes and billing metering may change actual cost.

Sources: [Vast official CLI](https://raw.githubusercontent.com/vast-ai/vast-cli/master/vast.py) documents raw internet speeds as Mb/s and raw `inet_down_cost`/`inet_up_cost` as $/GB; [search offer schema](https://docs.vast.ai/api-reference/search/search-offers) confirms fee units (its speed wording differs); [billing](https://raw.githubusercontent.com/vast-ai/docs/main/guides/reference/billing.mdx) distinguishes bandwidth charges from hourly rental. Raw speed handling follows the official CLI's Mb/s convention.
