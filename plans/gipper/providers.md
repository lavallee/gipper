# gipper: more than one GPU market

**Status:** built 2026-10-26: Runpod region filter, Vast and Shadeform providers
(see "Built" at the end). Vast was verified with real rentals on 2026-10-27:
smoke and UDP echo. Shadeform has not rented a machine yet (no key).

## Problem

Runpod is the only rented placement. Two gaps keep coming up:

1. **Stock and region.** The GPU type we want is out of stock, or is only in
   stock far away (for example, a 4090 only in Europe for an RTC workload that
   needs to be near US users).
2. **No UDP.** Runpod's docs say plainly: "Pods do not support UDP connections"
   (only HTTP proxy or TCP on a public IP:
   <https://docs.runpod.io/pods/configuration/expose-ports>). WebRTC media
   wants UDP.

Constraint: keep money parked with as few providers as possible.

## Findings

| Option | GPUs | UDP | Region control | Automation | Billing |
|---|---|---|---|---|---|
| **Vast.ai** | Consumer cards in quantity (3090/4090/5090) plus datacenter | **Yes.** Map ports with `-p 8082:8082/udp`; the external port arrives in `VAST_UDP_PORT_X`. The IP is shared and external ports are random unless you ask for identity ports (>70000) | Filter offers by geolocation | REST API and CLI (`search offers`, `create instance`, `destroy`) | Prepaid credit |
| **Shadeform** | 30+ clouds, 100+ regions; mostly datacenter cards (L40S, A6000, H100). How many 4090s: *not verified* | Full VMs, so it depends on each underlying cloud's firewall (Lambda allows Custom/All UDP rules) | `region` filter and `available=true` on `/v1/instances/types` | REST API, `shade` CLI, MCP | One bill across every cloud ("without … juggling their billing"). Prepaid vs card: *not verified* |
| Lambda (direct) | Datacenter only (H100/B200/GH200 …) | Yes, through firewall rules, including over the API | US regions | Instance and firewall API | Card. Already reachable through Shadeform |
| Prime Intellect | Aggregator focused on H100 clusters | *not found* | `region` on availability | Availability and provisioning API | Single credit balance. Overlaps with Shadeform |
| TensorDock | Marketplace, "100+ locations" | *not found* (docs were thin) | ? | API | ? |

**A workaround that needs no new provider:** Cloudflare Realtime TURN accepts
TURN over TCP (3478 or 80) and TURN over TLS (5349 or **443/tcp**). The pod
dials out over TCP to `turn.cloudflare.com` and the anycast relay carries the
media to peers. This works on Runpod today. The cost is TCP head-of-line
latency, and the relay is rated at about 50–100 Mbps per allocation, which is
plenty for audio. Use it when the RTC session can tolerate relay latency; rent
real UDP (Vast) when it can't.

Source: <https://developers.cloudflare.com/realtime/turn/>.

## Recommendation

Keep two wallets, and add a third only on demand:

1. **Runpod (keep).** Fix gap 1 partly inside Runpod first. gipper sends no
   region constraint today (`dataCenterIds` only comes from a volume), so a pod
   can land in Europe without anyone deciding that. Add `[gpu] regions` to the
   job and pass Runpod's country or datacenter filter. The REST field name needs
   checking against the v1 schema. If nothing in the allowed regions is in
   stock, fail over to the next provider instead of going far away.
2. **Vast.ai (add, roughly a $25 top-up).** It is the closest match for gipper's
   model: a Docker image plus ssh, consumer GPUs, geolocation filters, and real
   UDP. Host health varies like Runpod community cloud does; the existing
   preflight-and-replace already handles that.
3. **Shadeform (optional, later).** One account that reaches many datacenter
   clouds (Lambda, Hyperstack, Crusoe, …) when we need L40S/H100-class cards in
   a specific region, or a whole VM with its own IP. Check how it bills before
   funding it.

Skip Prime Intellect (overlaps with Shadeform) and TensorDock (unclear docs)
unless one of the first three disappoints.

## Built (2026-10-26)

- `gipper/providers/{runpod,vast,shadeform}.py` behind one interface
  (`offers`, `create`, `wait_ready`, `terminate`, `list_ours`, `account`);
  `gipper/market.py` ranks every market's offers together; `gipper/run.py` and
  `gipper/reap.py` no longer know which provider they are talking to.
- Job: `[gpu] regions` (ISO country codes), `[gpu] providers`, `[pod] ports`
  (`"5004/udp"`). UDP ports skip Runpod; the job gets `GIPPER_PUBLIC_IP` and
  `GIPPER_PORT_<n>_<PROTO>`.
- Runpod: stock is asked per country (`lowestPrice(countryCode:, secureCloud:)`)
  and pods are created with `countryCodes`. Checked live: the only 4090 in stock
  was in EUR-IS-2, and a US filter drops it.
- Price ceiling: 2x the cheapest stocked offer of the *preferred* GPUs. Vast's
  long tail ($0.13 3080s) would otherwise price out a $0.43 4090.
- Checked live without keys: Vast's `POST /api/v0/bundles/` and Shadeform's
  `GET /v1/instances/types` are public, so `gipper pod gpus` lists both.

### Still to verify with real rentals

1. **Vast: done 2026-10-27.** Smoke on a US 4090 and UDP echo on a CA 3060
   both passed: root ssh on the mapped port, and `vastai/base-image` ran the
   bootstrap. Preflight replaced a busy host. Still open: the gap between
   computed and billed spend (about $0.016 computed, $0.04 billed), and which
   error text means "offer gone" (no offer was taken during these runs).
   The original checklist was: `examples/smoke.toml --provider vast`, then `examples/udp-echo.toml`.
   Points to watch:
   - ssh comes up as root on the mapped port;
   - `vastai/base-image` has `curl` and `python3` for the bootstrap;
   - the ssh-key list response shape in `_ensure_key`;
   - which error text means "offer gone".
2. **Shadeform:** the same two jobs with `--provider shadeform`. Points to watch:
   - the `shadeform` user's home and `/home/shadeform/job`;
   - whether `auto_delete` is accepted as sent;
   - whether UDP gets through the underlying cloud's own firewall (try Massed
     Compute and Lambda);
   - the boot time against the 20-minute wait.
3. Then fold what was learned into the README's lessons, as with Runpod.

## What it takes in gipper (as planned; see Built)

Most of `gipper/runpod/run.py` is not specific to Runpod: caps, upload, the
detached run, checkpoints, collect, terminate in `finally`, receipts. A provider
needs to supply:

- `offers(min_mem_gb, regions, needs) -> [Offer(provider, gpu, usd_h, region, stock, ref)]`
- `create(offer, image, pubkey, ports) -> id`, `get(id) -> ssh (host, port) + port map`, `terminate(id)`
- `list_ours()` so the reaper covers every provider

On the job side: `[gpu] regions = ["US"]` and `needs = ["udp"]`. `needs`
filters out Runpod and requests the UDP ports. Then `gipper pod gpus` and
`estimate` rank one merged list across providers, ordered by preference and
price. This is the executor seam the phase-2 plan (gipper-c2x) already expects.
Receipts gain a `provider` field.
