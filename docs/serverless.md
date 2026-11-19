# Serverless queue jobs

Gipper can create a Runpod GPU queue endpoint, submit JSON jobs, retrieve results,
and verify deletion. This is separate from the existing Pod/SSH runner. The
client uses Python's standard library; a worker may use its host's SDK.

The initial adapter is Runpod. The interface separates endpoint lifecycle from
application inputs so another queue host can implement the same operations.
Vast and Shadeform serverless execution are not implemented. There is no
cross-provider placement or automatic job failover yet.

## One temporary job

A first development run can use a verified upstream CUDA image and install the
worker SDK at startup. This requires no local Docker access. After checking live
4090 Serverless stock and price, generate the spec with the observed rate:

```sh
python examples/serverless/bootstrap.py --rate-usd-h 1.34 > /tmp/gipper-smoke.toml
gipper serverless plan /tmp/gipper-smoke.toml
gipper serverless run /tmp/gipper-smoke.toml examples/serverless/input.json
```

Startup includes package installation and is not a baked-image latency measurement.
For a worker with dependencies preinstalled, build and push the worker image from `examples/serverless`, then edit its TOML
with your image reference and the current Serverless catalog rate.

```sh
docker build --platform linux/amd64 -t YOUR_REGISTRY/gipper-cuda-smoke:0.6.0 examples/serverless
docker push YOUR_REGISTRY/gipper-cuda-smoke:0.6.0
gipper serverless plan examples/serverless/runpod.toml
gipper serverless run examples/serverless/runpod.toml examples/serverless/input.json
```

The example runs matrix multiplication on CUDA, refuses CPU fallback, and returns
the GPU, worker identity, and answer. A COMPLETED job must have `answer == expected`.
Use an immutable image digest for repeatable production deployments. The Dockerfile
installs worker dependencies at build time; application images belong to the caller.

`run` deletes the temporary endpoint in `finally`, including failed jobs and normal
interruptions. SIGKILL or a lost machine cannot run `finally`; recover using the same
ledger with `gipper serverless reap --dry-run` then `gipper serverless reap`.
The reaper only deletes temporary endpoints whose recorded session deadline has
expired. The existing Pod reaper does not manage serverless endpoints. Schedule the
serverless command separately if you need unattended recovery.

## Reuse an endpoint across revisions

```sh
gipper serverless deploy examples/serverless/runpod.toml
# Use the returned endpoint ID below.
gipper serverless submit ENDPOINT examples/serverless/input.json --execution-seconds 120
gipper serverless wait ENDPOINT JOB --timeout 600
gipper serverless status ENDPOINT
gipper serverless ls
gipper serverless delete ENDPOINT
```

`deploy` deliberately keeps the endpoint. Minimum workers are always zero in this
first version. Idle workers can remain allocated and billed during `idle_seconds`;
FlashBoot can help subsequent starts but does not establish a latency guarantee.
Increasing the idle timeout trades money for readiness between revisions.
Deliberately kept endpoints are not expired by the temporary-session reaper.

A result reports the provider's `delayTime` and `executionTime` in milliseconds,
plus client waiting time. Queue delay can include startup and scheduling. It does
not isolate image pull or model initialization. Health rollups do not prove a worker
released its GPU or stopped billing. Retain the worker identity and instrument the
application if distinguishing warm reuse from a new worker matters.

## Python interface

```python
from gipper.serverless import Client, load

spec = load("examples/serverless/runpod.toml")
client = Client(spec.provider)
result = client.run(spec, {"size": 256})  # temporary endpoint, verified cleanup
assert result["status"] == "COMPLETED", result
```

For reuse, call `create(spec)`, `submit(endpoint, input, execution_seconds)`,
`wait(endpoint, job_id, timeout)`, and `delete(endpoint)`. Keep deletion in your own
`finally`. `wait` cancels on timeout or interruption and verifies a terminal job
state. A FAILED job returns its provider result so the application can interpret it;
the CLI exits nonzero. A `CleanupPending` exception takes priority over continuing
with another rental. Inspect the resource before making a replacement.

## Costs and custody

`rate_usd_h` is caller supplied from the current catalog. `plan` computes an occupancy
allowance using maximum workers times session duration plus the idle tail. It
refuses a configuration exceeding `max_usd`. This is an admission check, not a
provider-enforced cap. API delays, teardown delays, storage, and provider billing
rules can add cost. `session_seconds` bounds waiting in `run`; it does not expire
an endpoint created with `deploy`, or enforce a timeout on separately submitted jobs.
Execution timeout is explicitly sent to both the endpoint and each job.
Serverless receipts are separate from Pod cost/history and reconciliation; this
version does not claim to reconcile serverless bills or enforce the Pod daily cap.

Create, verified settings, submission intent, job IDs, terminal timing, cancellation,
and verified deletion are append-only `serverless.*` receipts. Input custody is
recorded as a SHA-256 and byte count; raw inputs and outputs are not written to the
ledger. Applications must retain their actual evidence separately. These receipts
are not included in `gipper custody report`, which currently covers Pod uploads.
Keys use Gipper's existing credential resolver and travel only in request headers.
Non-idempotent creates and submissions are never retried automatically. If a
response is lost, `SubmissionUnknown` and the receipt identify the ambiguity.
Inspect the exact resource name before retrying creation; an unrecorded creation
cannot be deleted through the owned-endpoint CLI and needs operator recovery.

## Storage and GPU placement

`runpod.pools` contains catalog **pool IDs**, such as `ADA_24`, rather than GPU names.
A pool can include multiple cards. Use `excluded_types` for explicit exclusions,
and check every remaining card fits your image and memory requirements.
`min_cuda_version` accepts newer compatible drivers. `data_centers` can narrow the
allowed placement. Actual availability is checked by the operator before deployment.

An existing `global_volume` can be attached without binding model files to a data
center. Alternatively, `network_volumes` attaches regional storage. They are mutually
exclusive. Gipper neither creates nor deletes these user-owned volumes. Keep Python
dependencies in the image and temporary decoding work on local disk. Global volume
writes and updates have object-storage semantics; roll workers to pick up new
versioned model assets. Global-volume latency is not qualified by the CUDA smoke test.

Runpod references are the [Serverless configuration](https://docs.runpod.io/serverless/endpoints/endpoint-configurations)
and [global storage documentation](https://docs.runpod.io/storage/globalvolume/globalvolume-serverless).
