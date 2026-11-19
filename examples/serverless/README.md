# CUDA serverless smoke worker

Build this directory for Linux amd64, push it to your registry, and replace the
image in `runpod.toml`. Run `gipper serverless plan runpod.toml` before
`gipper serverless run runpod.toml input.json`.

The handler multiplies two matrices on CUDA and reports the GPU and worker. It
refuses CPU fallback. Dependencies are installed in the image; no model download
is needed. The Gipper client has no SDK dependency.

This is an infrastructure smoke test, not an application throughput benchmark.
See [the full guide](../../docs/serverless.md) for reusable endpoint operation,
verified deletion, and budget qualifications.

For a first development run without Docker, use `bootstrap.py --rate-usd-h RATE`
to print a temporary spec for the upstream CUDA image. See the full guide for
commands. This installs Runpod at worker start and has higher startup latency.
