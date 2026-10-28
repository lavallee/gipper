# gipper: more than one GPU market

**Status:** decided and built. Runpod and Vast are in use (0.2, 2026-10-28);
Shadeform is wired in but deferred (no key) by decision on 2026-10-28. How offers
are chosen now lives in [docs/placement.md](../../docs/placement.md), and what
the rentals taught in [docs/lessons.md](../../docs/lessons.md). This page keeps
the reasoning behind choosing these markets.

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

## Outcome

- **Built in 0.1** (2026-10-26): providers behind one interface, regions, UDP
  ports, per-country Runpod stock.
- **Vast verified with real rentals** (2026-10-27): root ssh on the mapped port,
  bootstrap on `vastai/base-image`, UDP echo, preflight replacing a busy host.
- **0.2** (2026-10-28): offers judged on the whole run with memory of host
  reliability and billing. The 0.1 hourly ceiling described above is gone.
- **Shadeform, if it's ever needed:** run `examples/smoke.toml` and
  `examples/udp-echo.toml` with `--provider shadeform`, and watch four things:
  - the `shadeform` user's home;
  - whether `auto_delete` is accepted as sent;
  - whether UDP gets through the underlying cloud's firewall;
  - boot time against the 20-minute wait.

  Shadeform has no billing API, so its spend stays computed.
- **Still unknown on Vast:** which error text means "offer gone" (no offer has
  been taken from under gipper yet). gipper treats any create failure that looks
  like unavailability as "try the next offer".
