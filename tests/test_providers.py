"""Reading each provider's answers, using the shapes their APIs returned in October
2026 (trimmed)."""
import pytest

from gipper.http import ProviderError
from gipper.providers import Unavailable

RUNPOD_TYPES = [
    {"id": "A5000", "memoryInGb": 24, "communityCloud": True, "secureCloud": True, "communityPrice": 0.16, "securePrice": 0.27,
     "lowestPrice": {"stockStatus": None}},
    {"id": "4090", "memoryInGb": 24, "communityCloud": True, "secureCloud": True, "communityPrice": 0.34, "securePrice": 0.89,
     "lowestPrice": {"stockStatus": "Low"}, "stock_US_community": None, "stock_CA_community": {"stockStatus": "Low"}},
]


def test_runpod_stock_is_per_country():
    from gipper.providers.runpod import offers_from
    o = {x.gpu: x for x in offers_from(RUNPOD_TYPES, ["COMMUNITY"], ["US", "CA"])}
    assert (o["4090"].country, o["4090"].stock) == ("CA", "Low")    # only Canada had one
    assert (o["A5000"].country, o["A5000"].stock) == ("US", None)   # none anywhere: kept, ranked last


def test_runpod_ports_cuda_and_unrentable_types(monkeypatch):
    from gipper.job import Job
    from gipper.market import Offer
    from gipper.providers import runpod
    assert runpod._tcp_ports(["8080/tcp", "8888/http"]) == ["22/tcp", "8080/tcp", "8888/http"]
    with pytest.raises(Unavailable):
        runpod._tcp_ports(["5004/udp"])
    assert runpod._cuda(12.8, [])["allowedCudaVersions"] == ["13.0", "12.9", "12.8"]
    assert runpod._cuda(None, []) == {}

    class Api:   # what POST /pods says about a type GraphQL lists but REST won't create
        def create_pod(self, spec):
            raise ProviderError('POST /pods: HTTP 400: [{"problems":["At /pods/properties/gpuTypeIds/items/enum: ..."]}]')
    monkeypatch.setattr(runpod.Runpod, "api", lambda self: Api())
    with pytest.raises(Unavailable):
        runpod.PROVIDER.create(Offer("runpod", "NVIDIA RTX PRO 6000 Blackwell MIG", 24, 0.7, ref={"gpu_id": "x", "cloud": "SECURE"}),
                               Job(name="x", workload="w", command="true"), "ssh-ed25519 AAAA", "gipper-x")


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

    rows = [{"amount": 0.04, "timeBilledMs": 260203, "diskSpaceBilledGB": 140, "podId": "p1", "time": "2026-10-25 00:00:00"},
            {"amount": 0.01, "timeBilledMs": 60000, "diskSpaceBilledGB": 140, "podId": "p1", "time": "2026-10-26 00:00:00"}]

    class Api:
        def _req(self, method, url, body=None):
            return rows
    monkeypatch.setattr(runpod.Runpod, "api", lambda self: Api())
    usd, detail = runpod.PROVIDER.billed(0)["p1"]
    assert usd == pytest.approx(0.05) and detail["billed_s"] == 320


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
