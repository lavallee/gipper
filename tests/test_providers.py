"""Reading each provider's answers, using the shapes their APIs returned in October
2026 (trimmed)."""
import pytest

from gipper.http import ProviderError
from gipper.providers import Unavailable

RUNPOD_TYPES = [   # GET /v2/catalog/gpus?include=AVAILABILITY&product=POD (trimmed), stock_* added by Api.gpu_types
    {"id": "A5000", "memory": 24, "community": True, "secure": True, "price": {"community": 0.16, "secure": 0.27},
     "stock_community": "NONE", "stock_US_community": "NONE", "stock_CA_community": "NONE"},
    {"id": "4090", "memory": 24, "community": True, "secure": True, "price": {"community": 0.34, "secure": 0.89},
     "stock_community": "LOW", "stock_US_community": "NONE", "stock_CA_community": "LOW"},
]


def test_runpod_stock_is_per_country():
    from gipper.providers.runpod import offers_from
    o = {x.gpu: x for x in offers_from(RUNPOD_TYPES, ["COMMUNITY"], ["US", "CA"])}
    assert (o["4090"].country, o["4090"].stock) == ("CA", "Low")    # only Canada had one
    assert (o["A5000"].country, o["A5000"].stock) == ("US", None)   # none anywhere: kept, ranked last
    assert {x.gpu: x.stock for x in offers_from(RUNPOD_TYPES, ["COMMUNITY"])} == {"4090": "Low", "A5000": None}


def test_runpod_ports_cuda_and_unrentable_types(monkeypatch):
    from gipper.job import Job
    from gipper.market import Offer
    from gipper.placement import Placement
    from gipper.providers import runpod
    assert runpod._tcp_ports(["8080/tcp", "8888/http"]) == ["22/tcp", "8080/tcp", "8888/http"]
    with pytest.raises(Unavailable):
        runpod._tcp_ports(["5004/udp"])
    assert runpod._cuda(12.8, [])["allowedCudaVersions"] == ["13.0", "12.9", "12.8"]
    assert runpod._cuda(None, []) == {}

    made = []

    class Api:
        def __init__(self, err=None, centers=("US-KS-2",)):
            self.err, self.centers = err, list(centers)

        def data_centers(self, gpu_id, cloud, countries):
            return self.centers

        def create_pod(self, spec):
            made.append(spec)
            if self.err:
                raise ProviderError(self.err)
            return {"id": "p1", "cost": 0.89, "gpu": {"id": spec["gpu"]["id"], "count": 1}}

    offer = Offer("runpod", "NVIDIA GeForce RTX 4090", 24, 0.7, ref={"gpu_id": "NVIDIA GeForce RTX 4090", "cloud": "SECURE"})
    job = Job(name="x", workload="w", command="true", placement=Placement(min_cuda=12.8), regions=["us"])
    monkeypatch.setattr(runpod.Runpod, "api", lambda self: Api())
    got = runpod.PROVIDER.create(offer, job, "ssh-ed25519 AAAA", "gipper-x")
    assert got == {"id": "p1", "usd_h": 0.89, "gpu": "NVIDIA GeForce RTX 4090"}
    spec = made[0]   # the v2 body: nothing from v1 may leak into it (v2 422s on unknown fields)
    assert spec["gpu"] == {"id": "NVIDIA GeForce RTX 4090", "count": 1, "allowedCudaVersions": ["13.0", "12.9", "12.8"]}
    assert spec["dataCenterIds"] == ["US-KS-2"] and spec["cloud"] == "SECURE" and "mounts" not in spec
    assert not {"imageName", "gpuTypeIds", "countryCodes", "containerDiskInGb", "volumeInGb", "supportPublicIp"} & set(spec)

    monkeypatch.setattr(runpod.Runpod, "api", lambda self: Api(centers=()))   # a country restriction fails closed
    with pytest.raises(Unavailable):
        runpod.PROVIDER.create(offer, job, "ssh-ed25519 AAAA", "gipper-x")

    monkeypatch.setattr(runpod.Runpod, "api", lambda self: Api(  # what v2 says when a data center list has no stock
        'POST /v2/pods: HTTP 400: {"detail": "There are no longer any instances available with the requested specifications."}'))
    with pytest.raises(Unavailable):
        runpod.PROVIDER.create(offer, job, "ssh-ed25519 AAAA", "gipper-x")

    monkeypatch.setattr(runpod.Runpod, "api", lambda self: Api('POST /v2/pods: HTTP 422: additional properties not allowed'))
    with pytest.raises(ProviderError):   # a bad body is a bug, not scarce capacity
        runpod.PROVIDER.create(offer, job, "ssh-ed25519 AAAA", "gipper-x")


def test_runpod_wait_ready_reads_runtime_ports(monkeypatch):
    from gipper.providers import runpod
    pod = {"id": "p1", "status": "RUNNING", "gpu": {"id": "4090", "count": 1}, "runtime": {"ports": [
        {"private": 22, "public": 40022, "type": "tcp", "ip": "1.2.3.4"},
        {"private": 8888, "public": 40088, "type": "http", "ip": "1.2.3.4"}]}}

    class Api:
        def get_pod(self, pod_id):
            return pod
    monkeypatch.setattr(runpod.Runpod, "api", lambda self: Api())
    remote, info = runpod.PROVIDER.wait_ready("p1", None)
    assert (remote.host, remote.port) == ("1.2.3.4", 40022)
    assert info == {"machine": None, "gpu": "4090", "ports": {"8888": {"host": "1.2.3.4", "port": 40088}}}


def test_runpod_lists_every_page(monkeypatch):
    from gipper.providers import runpod
    pages = {None: {"pods": [{"id": "a", "name": "gipper-a", "cost": 0.3, "status": "RUNNING", "startedAt": "2026-11-16T10:00:00Z"}],
                    "pagination": {"nextCursor": "c2", "hasNextPage": True}},
             "c2": {"pods": [{"id": "b", "name": "other", "status": "RUNNING"}, {"id": "c", "name": "gipper-c", "status": "EXITED"}],
                    "pagination": {"nextCursor": None, "hasNextPage": False}}}
    monkeypatch.setattr(runpod.Api, "_req", lambda self, m, url, body=None: pages[url.partition("?cursor=")[2] or None])
    ours = runpod.PROVIDER.list_ours.__func__(type("P", (), {"api": lambda self: runpod.Api("k")})())
    assert [p["id"] for p in ours] == ["a", "c"] and ours[0]["usd_h"] == 0.3 and ours[0]["started"] > 0


VAST_ROWS = [{"id": i, "machine_id": 100 + i, "gpu_name": "RTX 4090", "gpu_ram": 24564, "dph_total": 0.38 + i / 100,
              "geolocation": "Michigan, US", "inet_down": 900.5, "inet_up": 300, "inet_down_cost": 0.003, "inet_up_cost": 0.004,
              "cuda_max_good": 13.0, "compute_cap": 890} for i in range(10)] + [
             {"id": 99, "machine_id": 7, "gpu_name": "A40", "gpu_ram": 46068, "dph_total": 0.3, "geolocation": "Quebec, CA"}]


def test_vast_offers():
    from gipper.providers.vast import PER_GPU, _port_opts, offers_from
    o = offers_from(VAST_ROWS)
    assert sum(x.gpu == "RTX 4090" for x in o) == PER_GPU
    a40 = o[-1]
    assert a40.mem_gb >= 48 and a40.country == "CA" and a40.ref == {"ask": 99, "machine": "7"}
    assert o[0].metrics["compute_capability"] == 8.9 and o[0].metrics["host_download_usd_per_gb"] == 0.003
    assert _port_opts(["5004/udp", "8080"]) == {"-p 5004:5004/udp": "1", "-p 8080:8080": "1"}


def test_vast_and_runpod_bills(monkeypatch):
    from gipper.providers import runpod, vast
    charges = {"results": [{"type": "instance", "source": "instance-58107307", "amount": 0.028, "items": [
        {"type": "gpu", "amount": 0.026}, {"type": "disk", "amount": 0.002}, {"type": "bwd", "amount": 0.0}]}]}
    monkeypatch.setattr(vast.Vast, "_req", lambda self, *a, **kw: charges)
    assert vast.PROVIDER.billed(0) == {"58107307": (0.028, {"gpu": 0.026, "disk": 0.002, "bwd": 0.0})}

    rows = {"records": [   # GET /v2/billing/pods?bucketSize=day: one record per pod per day
        {"totalAmount": 0.04, "gpuAmount": 0.03, "cpuAmount": 0, "diskAmount": 0.01, "podId": "p1", "startTime": "2026-11-15T00:00:00Z"},
        {"totalAmount": 0.01, "gpuAmount": 0.008, "cpuAmount": 0, "diskAmount": 0.002, "podId": "p1", "startTime": "2026-11-16T00:00:00Z"}],
        "metadata": {"recordCount": 2}}

    class Api:
        def _req(self, method, url, body=None):
            return rows
    monkeypatch.setattr(runpod.Runpod, "api", lambda self: Api())
    usd, detail = runpod.PROVIDER.billed(0)["p1"]
    assert usd == pytest.approx(0.05) and detail == {"gpu": 0.038, "cpu": 0, "disk": 0.012}


SHADE_TYPES = [
    {"cloud": "massedcompute", "shade_instance_type": "A6000", "gpu_type": "A6000", "num_gpus": 1, "hourly_price": 57,
     "configuration": {"vram_per_gpu_in_gb": 48, "os_options": ["ubuntu22.04", "ubuntu22.04_cuda12.8_shade_os"]},
     "availability": [{"region": "desmoines-usa-1", "available": True, "display_name": "US, Des Moines", "rental_type": "on_demand"},
                      {"region": "desmoines-usa-1", "available": True, "display_name": "US, Des Moines", "rental_type": "spot"},
                      {"region": "oslo-norway-1", "available": False, "display_name": "NO, Oslo"}]},
    {"cloud": "massedcompute", "shade_instance_type": "A6000_plus", "gpu_type": "A6000", "num_gpus": 1, "hourly_price": 57,
     "configuration": {"vram_per_gpu_in_gb": 48},
     "availability": [{"region": "desmoines-usa-1", "available": True, "display_name": "US, Des Moines"}]},
]


def test_shadeform_offers():
    from gipper.providers.shadeform import _ufw, offers_from
    o = offers_from(SHADE_TYPES)
    assert len(o) == 1 and (o[0].country, o[0].usd_h) == ("US", 0.57)   # on-demand, available, one per card and region
    assert o[0].ref["os"] == "ubuntu22.04_cuda12.8_shade_os"
    assert _ufw(["5004/udp"]) == [{"port": "22", "proto": "tcp"}, {"port": "5004", "proto": "udp"}]
