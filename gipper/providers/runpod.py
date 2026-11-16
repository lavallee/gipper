"""Runpod: REST v2 (api.runpod.io/v2) for prices, stock, pods, network volumes and
billing; GraphQL only for the account (`myself` has no v2 route). Containers with
ssh as root; TCP and HTTP ports only ("Pods do not support UDP connections",
docs.runpod.io/pods/configuration/expose-ports).

Regions: stock is asked per country (the catalog's `countryCodes` filter), and the
pod is created with `dataCenterIds` taken from that same answer, so a 4090 that
only exists in Iceland is not rented for a job that must stay in the US. v2 has no
`countryCodes` on create; `dataCenterIds` is enforced, so the restriction holds.
"""
import re
import time
from datetime import datetime, timezone

from gipper.config import settings
from gipper.http import ProviderError, request
from gipper.market import Offer
from gipper.providers import BadHost, Provider, Unavailable
from gipper.ssh import Remote

GRAPHQL = "https://api.runpod.io/graphql"
REST = "https://api.runpod.io/v2"
RunpodError = ProviderError
# What gpu.allowedCudaVersions accepts on POST /v2/pods (the v1 list, 2026-10; v2 matches exactly)
CUDA_VERSIONS = ("13.0", "12.9", "12.8", "12.7", "12.6", "12.5", "12.4", "12.3", "12.2", "12.1", "12.0", "11.8")


class Api:
    def __init__(self, key):
        self._key = key

    def _req(self, method, url, body=None):
        return request(method, url, body, headers={"Authorization": f"Bearer {self._key}"})

    def gql(self, query):
        d = self._req("POST", GRAPHQL, {"query": query})
        if d.get("errors"):
            raise ProviderError(str(d["errors"])[:500])
        return d["data"]

    def account(self):
        return self.gql("query { myself { clientBalance currentSpendPerHr spendLimit } }")["myself"]

    def catalog(self, cloud, country=None):
        """GET /catalog/gpus with stock for one cloud, optionally scoped to one country."""
        q = f"include=AVAILABILITY&product=POD&count=1&cloud={cloud}" + (f"&countryCodes={country}" if country else "")
        return self._req("GET", f"{REST}/catalog/gpus?{q}")["gpus"]

    def gpu_types(self, countries=()):
        """Every GPU type with prices; stock (NONE|LOW|MEDIUM|HIGH) per cloud as
        `stock_<community|secure>` and, per country, `stock_<CC>_<community|secure>`."""
        out = {}
        for c in ("community", "secure"):
            for cc in (countries or (None,)):
                key = f"stock_{cc}_{c}" if cc else f"stock_{c}"
                for g in self.catalog(c.upper(), cc):
                    out.setdefault(g["id"], g)[key] = g.get("availability")
        return list(out.values())

    def data_centers(self, gpu_id, cloud, countries):
        """Data centers in `countries` that offer this GPU type (the stand-in for v1's
        `countryCodes` on create)."""
        ids = []
        for g in self.catalog(cloud, ",".join(countries)):
            if g["id"] == gpu_id:
                ids += [dc["id"] for dc in g.get("dataCenters") or [] if dc["id"] not in ids]
        return ids

    def create_pod(self, spec):
        return self._req("POST", f"{REST}/pods", spec)

    def get_pod(self, pod_id):
        return self._req("GET", f"{REST}/pods/{pod_id}")

    def list_pods(self):
        """Every pod, walking the pages (`GET /pods` takes no filters)."""
        out, cursor = [], None
        while True:
            d = self._req("GET", f"{REST}/pods" + (f"?cursor={cursor}" if cursor else ""))
            out += d.get("pods", [])
            page = d.get("pagination") or {}
            cursor = page.get("nextCursor")
            if not (page.get("hasNextPage") and cursor):
                return out

    def terminate_pod(self, pod_id):
        return self._req("DELETE", f"{REST}/pods/{pod_id}")

    def volumes(self):
        return self._req("GET", f"{REST}/network-volumes").get("networkVolumes", [])

    def create_volume(self, name, size_gb, datacenter):
        return self._req("POST", f"{REST}/network-volumes", {"name": name, "size": size_gb, "dataCenter": datacenter})

    def delete_volume(self, volume_id):
        return self._req("DELETE", f"{REST}/network-volumes/{volume_id}")

    _gql = gql   # the name scripts written before 2026-10-26 use


def _price(g, cloud):
    return (g.get("price") or {}).get("community" if cloud == "COMMUNITY" else "secure") or None


def _stock(level):
    """v2's NONE|LOW|MEDIUM|HIGH as the High/Medium/Low receipts and Offer.stock have
    always carried; NONE (and unknown) is no stock."""
    return level.title() if level in ("LOW", "MEDIUM", "HIGH") else None


def offers_from(gpu_types, clouds=("COMMUNITY", "SECURE"), countries=()):
    """One offer per GPU type and cloud that Runpod prices. With countries, stock is
    the best stock in any of them and the offer is placed in the first stocked one;
    without stock anywhere it still counts (Runpod's stock is a hint), ranked last."""
    out = []
    for cloud in clouds:
        c = cloud.lower()
        for g in gpu_types:
            if not g.get("community" if cloud == "COMMUNITY" else "secure"):
                continue
            p = _price(g, cloud)
            if not p:
                continue
            if countries:
                stocked = [(cc, _stock(g.get(f"stock_{cc}_{c}"))) for cc in countries]
                stocked = [(cc, s) for cc, s in stocked if s]
                country, stock = stocked[0] if stocked else (countries[0], None)
            else:
                country, stock = None, _stock(g.get(f"stock_{c}"))
            out.append(Offer("runpod", g["id"], float(g.get("memory") or 0), p, where=f"{c} {country or ''}".strip(),
                             country=country,
                             stock=stock, udp=False, ref={"gpu_id": g["id"], "cloud": cloud}))
    return out


def _tcp_ports(ports):
    """Job ports ("8080/tcp", "8888/http") as Runpod wants them; 22 always."""
    out = ["22/tcp"]
    for p in ports:
        num, _, proto = p.partition("/")
        proto = (proto or "tcp").lower()
        if proto == "udp":
            raise Unavailable("Runpod pods cannot expose UDP")
        if f"{num}/{proto}" not in out:
            out.append(f"{num}/{proto}")
    return out


class Runpod(Provider):
    name = "runpod"
    key_name = "RUNPOD_API_KEY"
    udp = False

    def api(self):
        return Api(self.key())

    def offers(self, job):
        clouds = [job.cloud] + (["SECURE"] if job.cloud == "COMMUNITY" else [])
        regions = [r.upper() for r in job.regions]
        if any(not re.fullmatch(r"[A-Z]{2}", r) for r in regions):
            raise ProviderError(f"regions must be ISO country codes, got {regions}")
        return offers_from(self.api().gpu_types(regions), clouds, regions)

    def create(self, offer, job, pubkey, name):
        api = self.api()
        s = settings()
        cloud = offer.ref["cloud"]
        gpu = {"id": offer.ref["gpu_id"], "count": 1, **_cuda(job.placement.min_cuda, s["cuda_versions"])}
        spec = {
            "name": name, "image": job.image or s["image"], "gpu": gpu, "cloud": cloud, "disk": job.container_disk_gb,
            "ports": _tcp_ports(job.ports),
            "env": {"PUBLIC_KEY": pubkey, **job.env},
        }
        if job.volume:  # the volume pins the data centre, which already fixes the country
            vol = next((v for v in api.volumes() if v.get("name") == job.volume), None)
            if not vol:
                raise SystemExit(f"network volume {job.volume!r} not found")
            spec.update(mounts={"network": [{"volumeId": vol["id"], "path": "/workspace"}]}, dataCenterIds=[vol["dataCenter"]])
        elif job.regions:
            # v1's `countryCodes` was enforced by the scheduler; v2 wants the data centres named. Fail closed:
            # a country restriction is never widened here.
            regions = [r.upper() for r in job.regions]
            centers = api.data_centers(gpu["id"], cloud, regions)
            if not centers:
                raise Unavailable(f"Runpod has no {offer.gpu} {cloud.lower()} data center in {','.join(regions)}")
            spec["dataCenterIds"] = centers
        try:
            pod = api.create_pod(spec)
        except ProviderError as e:
            msg = str(e).lower()
            if "http 400" in msg and ("instances available" in msg or "no instances" in msg):
                raise Unavailable(str(e)) from None
            raise
        return {"id": pod["id"], "usd_h": pod.get("cost") or offer.usd_h, "gpu": (pod.get("gpu") or {}).get("id") or offer.gpu}

    def wait_ready(self, pod_id, job, timeout_s=900):
        api = self.api()
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            p = api.get_pod(pod_id)
            # runtime.ports is null until the pod is RUNNING; each entry carries its own host
            mapped = {x["private"]: x for x in (p.get("runtime") or {}).get("ports") or [] if x.get("public") and x.get("ip")}
            if 22 in mapped:
                ports = {str(k): {"host": x["ip"], "port": x["public"]} for k, x in mapped.items() if k != 22}
                # v2 exposes no host machine id: Runpod hosts are remembered by provider, not machine
                return Remote(mapped[22]["ip"], mapped[22]["public"]), {"machine": None, "gpu": (p.get("gpu") or {}).get("id"), "ports": ports}
            time.sleep(5)
        raise BadHost(f"pod {pod_id} had no ssh endpoint after {timeout_s}s")

    def terminate(self, pod_id):
        self.api().terminate_pod(pod_id)

    def list_ours(self):
        out = []
        for p in self.api().list_pods():
            if (p.get("name") or "").startswith("gipper-"):
                started = p.get("startedAt")
                try:
                    started = datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp() if started else None
                except ValueError:
                    started = None
                out.append({"id": p["id"], "name": p["name"], "started": started,
                            "status": p.get("status"), "usd_h": p.get("cost")})
        return out

    def billed(self, since):
        """{pod id: (usd, detail)} Runpod billed since `since` (epoch s), one record per pod per day."""
        start = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = datetime.fromtimestamp(time.time() + 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")   # v2 wants both bounds
        d = self.api()._req("GET", f"{REST}/billing/pods?bucketSize=day&startTime={start}&endTime={end}")
        out = {}
        for r in d.get("records", []) if isinstance(d, dict) else []:
            usd, detail = out.get(r["podId"], (0.0, {}))
            for k in ("gpu", "cpu", "disk"):
                detail[k] = round(detail.get(k, 0) + (r.get(f"{k}Amount") or 0), 6)
            out[r["podId"]] = (usd + (r.get("totalAmount") or 0), detail)
        return out

    def account(self):
        a = self.api().account()
        return f"balance ${a['clientBalance']:.2f}; spending ${a['currentSpendPerHr']:.2f}/h now; spend limit {a['spendLimit']}"


PROVIDER = Runpod()


def _cuda(min_cuda, configured):
    """gpu.allowedCudaVersions: the configured list, or every version Runpod knows at or
    above the job's min_cuda; nothing (any host) if neither is set."""
    versions = list(configured or CUDA_VERSIONS)
    if min_cuda:
        versions = [v for v in versions if tuple(map(int, v.split("."))) >= tuple(map(int, f"{min_cuda}".split(".")[:2]))]
    if (configured or min_cuda) and not versions:   # an empty list would mean "any" to Runpod
        raise Unavailable(f"no Runpod CUDA version satisfies min_cuda {min_cuda}")
    return {"allowedCudaVersions": versions} if (configured or min_cuda) else {}
