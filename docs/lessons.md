# Operational notes

These are implementation lessons from live rentals, useful across workloads.
They describe why safeguards exist; they are not provider performance rankings
or promises about current stock, prices, or reliability.

## Cleanup and billing

- A successful delete request does not prove a machine is gone. gipper verifies
  that the provider no longer lists it before marking closure complete or renting
  a replacement. Check the provider console if API calls remain unavailable.
- Stopping the remote command does not stop rental billing. Keep the reaper
  available independently of a long-running client.
- Rate × lifetime can differ from a provider's bill because of storage,
  bandwidth, or rounding. Reconciliation records actual bills; billing-factor
  history excludes rentals shorter than ten minutes to limit rounding effects.
- A bill belongs to the rental's day, even if it arrives later. Moving old
  charges to the reconciliation date would distort the daily cap.

## Host checks and placement

- `nvidia-smi` can work while CUDA initialization fails. Preflight calls
  `cuInit`; error 804 triggers a retry without forward-compatibility libraries.
- A working host can be unsuitable for a particular runtime. Raise
  `run.Unsuitable` for job-specific rejection so it does not damage host history.
- Host location labels are approximate. Measure latency from the endpoint that
  matters to an interactive job; probes from another client are only proxies.
- Small transfer probes include SSH startup overhead. Only real transfers of
  at least 20 MB update link-speed history; the optional probe checks suitability.
- Advertised GPU types do not always have rentable stock. Runpod's catalog reports
  stock per cloud and country; an offer without stock is ranked last, not dropped.
- Runpod retired REST v1 and its GraphQL pricing on short notice (v1 on
  2026-11-16, GraphQL integrations early 2027). Keep every provider call in its
  one adapter module so a migration touches one file, and read the provider's
  published spec for query rules the docs omit (v2 billing needs both `startTime`
  and `endTime`).
- Set explicit CUDA and compute-capability requirements when your workload has
  them. Provider filters are supplemented by checks on the rented host.

## Job setup and connections

- Keep application installation in `[run] setup`. The bootstrap provides `uv`
  and a PyTorch wheel-index hint matched to the host driver; it does not install
  models, training libraries, renderers, or application dependencies.
- Published outside ports can differ from container ports. Read
  `GIPPER_PUBLIC_IP` and `GIPPER_PORT_<n>_<PROTO>` when advertising a service.
  The Runpod adapter does not expose UDP; choose a compatible provider or your
  application's own relay arrangement.
- Detached jobs redirect every stream and run under a remote timeout. Simply
  backgrounding a shell list over SSH can leave the connection waiting.
- Give comparable jobs the same `workload` name so compute-time history is
  useful. Different data sizes, dependency versions, or training settings may
  need separate names or explicit `[placement]` estimates.

Live provider changes should be qualified with bounded smoke runs and recorded
with their date, job, provider, and closure result. Unit tests establish parsing
and decisions; they do not demonstrate current provider behavior.


## Serverless queue qualification (2026-11-19)

The Runpod adapter completed four real OCR extraction jobs through the public
Gipper client. A 60.97-second clip returned in 16.27 seconds warm; a repetitive
four-minute graphics fixture returned in 27.01 seconds warm. The first short
request took 118.89 seconds including development dependency installation.
These exclude downstream model review and are single-run observations, not
service latency promises. Worker CUDA session selection and input hashes were
checked, and endpoint deletion was independently verified.

Installing the worker SDK directly into the CUDA base image failed when pip
tried to uninstall Debian-managed `cryptography` without a RECORD file. An
isolated virtual environment with `--system-site-packages` retained access to
Torch/CUDA while keeping SDK dependencies separate. Apply the same isolation
when building the example image. The example Dockerfile is authored; its image
has not been built on this host because Docker daemon access is unavailable.

Runpod read-back can add `workers.initTimeout = null` and omit an empty
`gpu.excludedTypes`. Compare requested fields with their actual defaults, while
rejecting changed worker bounds, GPU selection, and execution timeout. Provider
health rollups alone cannot establish that a GPU was released or stopped billing.
