"""Runpod: GraphQL for prices, stock and the account, REST for pods and network
volumes. Containers with ssh as root; TCP and HTTP ports only ("Pods do not
support UDP connections", docs.runpod.io/pods/configuration/expose-ports).

Regions: stock is asked per country (`lowestPrice(countryCode:)`), and the pod is
created with `countryCodes`, so a 4090 that only exists in Iceland is not rented
for a job that must stay in the US.
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
REST = "https://rest.runpod.io/v1"
RunpodError = ProviderError
# What POST /pods accepts for allowedCudaVersions (REST v1 schema, 2026-10)
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

    def gpu_types(self, countries=()):
        """Every GPU type with prices; stock overall (`lowestPrice`) and, per country
        and cloud, as `stock_<CC>_<community|secure>`."""
        extra = "".join(
            f' stock_{cc}_{c}: lowestPrice(input:{{gpuCount:1, countryCode:"{cc}", secureCloud:{str(c == "secure").lower()}}}){{ stockStatus }}'
            for cc in countries for c in ("community", "secure"))
        q = ("query { gpuTypes { id displayName memoryInGb secureCloud communityCloud securePrice communityPrice "
             f"lowestPrice(input:{{gpuCount:1}}){{ uninterruptablePrice stockStatus }}{extra} }} }}")
        return self.gql(q)["gpuTypes"]

    def create_pod(self, spec):
        return self._req("POST", f"{REST}/pods", spec)

    def get_pod(self, pod_id):
        return self._req("GET", f"{REST}/pods/{pod_id}")

    def list_pods(self):
        d = self._req("GET", f"{REST}/pods")
        return d if isinstance(d, list) else d.get("pods", d.get("data", []))

    def terminate_pod(self, pod_id):
        return self._req("DELETE", f"{REST}/pods/{pod_id}")

    def volumes(self):
        d = self._req("GET", f"{REST}/networkvolumes")
        return d if isinstance(d, list) else d.get("networkVolumes", d.get("data", []))

    def create_volume(self, name, size_gb, datacenter):
        return self._req("POST", f"{REST}/networkvolumes", {"name": name, "size": size_gb, "dataCenterId": datacenter})

    def delete_volume(self, volume_id):
        return self._req("DELETE", f"{REST}/networkvolumes/{volume_id}")

    _gql = gql   # the name scripts written before 2026-10-26 use


def _price(g, cloud):
    return g.get("communityPrice" if cloud == "COMMUNITY" else "securePrice") or None


def offers_from(gpu_types, clouds=("COMMUNITY", "SECURE"), countries=()):
    """One offer per GPU type and cloud that Runpod prices. With countries, stock is
    the best stock in any of them and the offer is placed in the first stocked one;
    without stock anywhere it still counts (Runpod's stock is a hint), ranked last."""
    out = []
    for cloud in clouds:
        c = cloud.lower()
        for g in gpu_types:
            if not g.get("communityCloud" if cloud == "COMMUNITY" else "secureCloud"):
                continue
            p = _price(g, cloud)
            if not p:
                continue
            if countries:
                stocked = [(cc, (g.get(f"stock_{cc}_{c}") or {}).get("stockStatus")) for cc in countries]
                stocked = [(cc, s) for cc, s in stocked if s]
                country, stock = stocked[0] if stocked else (countries[0], None)
            else:
                country, stock = None, (g.get("lowestPrice") or {}).get("stockStatus")
            out.append(Offer("runpod", g["id"], float(g.get("memoryInGb") or 0), p, where=f"{c} {country or ''}".strip(),
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
        spec = {
            "name": name, "imageName": job.image or s["image"], "gpuTypeIds": [offer.ref["gpu_id"]], "gpuCount": 1,
            "cloudType": cloud, "computeType": "GPU", "containerDiskInGb": job.container_disk_gb, "volumeInGb": 0,
            # direct ssh (tar streams) needs a public IP: community hosts must offer one; secure always does
            "ports": _tcp_ports(job.ports), **({"supportPublicIp": True} if cloud == "COMMUNITY" else {}),
            **_cuda(job.placement.min_cuda, s["cuda_versions"]),
            "env": {"PUBLIC_KEY": pubkey, **job.env},
        }
        if job.volume:  # the volume pins the data centre, which already fixes the country
            vol = next((v for v in api.volumes() if v.get("name") == job.volume), None)
            if not vol:
                raise SystemExit(f"network volume {job.volume!r} not found")
            spec.update(networkVolumeId=vol["id"], volumeMountPath="/workspace", dataCenterIds=[vol["dataCenterId"]])
        elif job.regions:
            spec["countryCodes"] = [r.upper() for r in job.regions]
        try:
            pod = api.create_pod(spec)
        except ProviderError as e:
            msg = str(e).lower()
            if "no instances" in msg or "not available" in msg:
                raise Unavailable(str(e)) from None
            if "http 400" in msg and "gputypeids" in msg:
                # GraphQL lists types (MIG slices, new SKUs) that POST /pods doesn't accept yet
                raise Unavailable(f"Runpod won't create {offer.gpu} through its REST API") from None
            raise
        return {"id": pod["id"], "usd_h": pod.get("costPerHr") or offer.usd_h,
                "gpu": (pod.get("machine") or {}).get("gpuTypeId") or offer.gpu}

    def wait_ready(self, pod_id, job, timeout_s=900):
        api = self.api()
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            p = api.get_pod(pod_id)
            mapped = p.get("portMappings") or {}
            ip = p.get("publicIp")
            if ip and mapped.get("22"):
                ports = {k: {"host": ip, "port": v} for k, v in mapped.items() if k != "22"}
                return Remote(ip, mapped["22"]), {"machine": p.get("machineId"),
                                                  "gpu": (p.get("machine") or {}).get("gpuTypeId"), "ports": ports}
            time.sleep(5)
        raise BadHost(f"pod {pod_id} had no ssh endpoint after {timeout_s}s")

    def terminate(self, pod_id):
        self.api().terminate_pod(pod_id)

    def list_ours(self):
        out = []
        for p in self.api().list_pods():
            if (p.get("name") or "").startswith("gipper-"):
                started = p.get("lastStartedAt")
                try:
                    started = datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp() if started else None
                except ValueError:
                    started = None
                out.append({"id": p["id"], "name": p["name"], "started": started,
                            "status": p.get("desiredStatus"), "usd_h": p.get("costPerHr")})
        return out

    def billed(self, since):
        """{pod id: (usd, detail)} Runpod billed since `since` (epoch s), per pod per day."""
        start = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.api()._req("GET", f"{REST}/billing/pods?bucketSize=day&grouping=podId&startTime={start}")
        out = {}
        for r in rows if isinstance(rows, list) else []:
            usd, detail = out.get(r["podId"], (0.0, {}))
            detail["billed_s"] = round(detail.get("billed_s", 0) + (r.get("timeBilledMs") or 0) / 1000)
            detail["disk_gb"] = r.get("diskSpaceBilledGB")
            out[r["podId"]] = (usd + (r.get("amount") or 0), detail)
        return out

    def account(self):
        a = self.api().account()
        return f"balance ${a['clientBalance']:.2f}; spending ${a['currentSpendPerHr']:.2f}/h now; spend limit {a['spendLimit']}"


PROVIDER = Runpod()


def _cuda(min_cuda, configured):
    """allowedCudaVersions: the configured list, or every version Runpod knows at or
    above the job's min_cuda; nothing (any host) if neither is set."""
    versions = list(configured or CUDA_VERSIONS)
    if min_cuda:
        versions = [v for v in versions if tuple(map(int, v.split("."))) >= tuple(map(int, f"{min_cuda}".split(".")[:2]))]
    if (configured or min_cuda) and not versions:   # an empty list would mean "any" to Runpod
        raise Unavailable(f"no Runpod CUDA version satisfies min_cuda {min_cuda}")
    return {"allowedCudaVersions": versions} if (configured or min_cuda) else {}
