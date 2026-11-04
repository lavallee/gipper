# What the rentals taught

Historical observations from October 2026, not current provider guarantees.
Newest first. Each lesson is either built into gipper now or noted as advice for jobs.

## 2026-10-28: round trips (0.2.2)

- **Hosts can be measured before renting.** Vast publishes each host's public IP;
  pinging the 48 cheapest from the test client took about 10 seconds, and 31 answered
  (Quebec 28 ms, Oregon 83 ms, South Korea 205 ms, Thailand 267 ms).
- **Vast's country labels aren't reliable.** A host listed in China answered in
  81 ms from an IP another host had listed as the United States. Measured round
  trips outrank the label.

## 2026-10-28: memory and bills (0.2)

- **Bills differ from rate × lifetime, and not evenly.** Over 107 machines,
  providers billed $17.29 against $16.47 computed. On rentals of 10 minutes or
  more, Runpod billed ×1.01 and Vast ×1.10. Vast bills disk from creation (GPU
  only while running) and rounds short rentals up: a 47-second smoke run was
  billed 0.07 GPU-hours, 5× its computed cost, which is why short rentals are
  left out of the factor. gipper now reconciles bills (`gipper pod reconcile`,
  run by the reap timer), and estimates apply each provider's factor.
- **A late bill belongs to its machine's day.** Bills arrive days after the
  rental; counting them by receipt date would have charged old work to today's
  cap. Spend is attributed to when the machine was created.
- **Runpod: 81 of 81 hosts good. Vast: 18 good, 6 bad** (ssh never came up,
  ssh timeouts, failed transfer probes, a busy GPU, a dropped upload). A further
  Vast host worked but didn't suit a job's pinned CUDA runtime; that counts
  against nobody. On a short job a 23% risk outweighs Vast's lower price; on a
  long one it doesn't. That's why the score weighs both.
- **Small probes measure ssh, not bandwidth.** The 2 MiB probes read 15 Mb/s
  against hosts advertising 900. Real uploads and downloads are now timed, and
  only those (20 MB or more) feed memory.
- **Runpod's GraphQL lists GPU types its REST API won't create** (MIG slices,
  new SKUs), and the 400 used to end the whole run. Such offers are now skipped.
  The same error also caught a client passing a CUDA version Runpod doesn't know
  (`allowedCudaVersions` takes "12.8", not "12.8.1").
- **Runpod charges nothing for data transfer**
  ([pricing](https://docs.runpod.io/pods/pricing)); Vast charges per GB at rates
  that vary by host and are advertised with each offer.

## 2026-10-27: Vast and verified teardown

- **A first US 4090 smoke run:** ssh in 38 s, the whole round trip 47 s.
- **UDP works on Vast:** an echo from the test client replied 5 of 5 in about 22 ms, from
  the same public port, so `GIPPER_PUBLIC_IP:GIPPER_PORT_<n>_UDP` is the address
  to advertise.
- **Preflight earns its keep:** the first host for the UDP test was at 100% GPU
  use before the job started.
- **A provider accepting a delete is not the machine being gone.** Client runs
  hit deletes that were accepted but still listed, and list calls that failed
  outright. Teardown now counts only once the machine is absent from the
  provider's list, and no replacement is rented until then.
- **`nvidia-smi` working doesn't mean CUDA does.** Some images' CUDA
  forward-compat libraries make `cuInit` fail with 804 on newer drivers. Every
  host now runs a `cuInit` check, retrying once without the compat path.

## 2026-10-26: regions and markets

- **Runpod's stock is per country.** The only 4090 in stock was in EUR-IS-2
  (Iceland); without a region filter it would have been rented for US-facing
  work. Stock is now asked per country, and pods are created with `countryCodes`.
- **Runpod pods can't do UDP**
  ([docs](https://docs.runpod.io/pods/configuration/expose-ports)).

## 2026-10-20: first runs (Runpod)

- **Cloudflare in front of Runpod's API rejects Python's default user agent**
  (error 1010), so gipper sends its own.
- **Filtering hosts by CUDA version or requiring a public IP can empty the
  market.** gipper picks the torch build per host from the driver version (not
  `nvidia-smi`'s "CUDA Version", which compat libraries inflate) instead.
- **Backgrounding `cd X && nohup job &` over ssh backgrounds the whole list,** so
  ssh waits for the job. gipper backgrounds only the job, with `setsid` and every
  stream redirected.
- **Host health varies:** a 5090 at 100% use with 1 GB held before the job
  started; an L40S whose first CUDA call failed twice.
- **Micro-batch 1 leaves a 4090 mostly idle for LoRA training** (3.9 s/step
  against the 3060's 4.5). Keep the effective batch and raise the micro-batch
  (e.g. 4 × 4).
- **A unit started with `systemd-run` disappears when stopped.** To resume, run
  the same command line again.
- **First real job:** 227 VoxCPM2 takes on a secure-cloud 4090 in 19.7 minutes
  (about 4 of them setup) for $0.29; the same work took about 3.5 h on the local machine.
