"""Phase 0 tests: no network, no spending."""
import json
import time

import pytest

SECRET = "rpa_TESTSECRET0123456789"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GIPPER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("RUNPOD_API_KEY", SECRET)
    monkeypatch.setenv("GIPPER_ENV", str(tmp_path / "no.env"))   # never read the real keys
    for k in ("VAST_API_KEY", "SHADEFORM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    import importlib
    import gipper.config
    import gipper.ledger
    importlib.reload(gipper.config)
    importlib.reload(gipper.ledger)
    yield tmp_path


def test_ledger_never_records_the_key():
    from gipper import ledger
    ledger.write("error", {"error": f"HTTP 401 for Bearer {SECRET}"}, job="j1")
    lines = list(ledger.read())
    assert lines and SECRET not in json.dumps(lines)
    assert "<redacted>" in lines[0]["payload"]["error"]


def test_spend_since_sums_today():
    from gipper import ledger
    ledger.write("spend", {"usd": 0.25, "seconds": 600})
    ledger.write("spend", {"usd": 0.10, "seconds": 300})
    assert abs(ledger.spend_since(ledger.today_start()) - 0.35) < 1e-9


def _job(tmp_path, body):
    p = tmp_path / "job.toml"
    p.write_text(body)
    from gipper.job import load
    return load(p)


def test_job_requires_command(tmp_path):
    from gipper.job import JobError
    with pytest.raises(JobError):
        _job(tmp_path, 'name = "x"\nworkload = "w"\n')


def test_job_rejects_secrets_in_env(tmp_path):
    from gipper.job import JobError
    with pytest.raises(JobError):
        _job(tmp_path, 'name="x"\nworkload="w"\n[run]\ncommand="true"\n[env]\nHF_TOKEN="x"\n')


def test_job_resolves_paths_and_defaults(tmp_path):
    (tmp_path / "in").mkdir()
    j = _job(tmp_path, 'name="x"\nworkload="w"\n[run]\ncommand="true"\n[[inputs]]\nsrc="in"\ndst="in"\n'
                       '[[outputs]]\nsrc="out"\ndst="got"\n')
    assert j.inputs[0].src == str(tmp_path / "in")
    assert j.outputs[0].dst == str(tmp_path / "got")
    assert j.max_usd > 0 and j.sensitivity == "cloud-ok"


GPUS = [
    {"id": "A5000", "memoryInGb": 24, "communityCloud": True, "secureCloud": True, "communityPrice": 0.16, "securePrice": 0.27, "lowestPrice": {"stockStatus": None}},
    {"id": "4090", "memoryInGb": 24, "communityCloud": True, "secureCloud": True, "communityPrice": 0.34, "securePrice": 0.89, "lowestPrice": {"stockStatus": "Low"},
     "stock_US_community": None, "stock_US_secure": None, "stock_CA_community": {"stockStatus": "Low"}},
    {"id": "5090", "memoryInGb": 32, "communityCloud": True, "secureCloud": True, "communityPrice": 0.69, "securePrice": 1.19, "lowestPrice": {"stockStatus": "Low"}},
    {"id": "small", "memoryInGb": 8, "communityCloud": True, "secureCloud": True, "communityPrice": 0.05, "securePrice": 0.1, "lowestPrice": {"stockStatus": "High"}},
]


def test_runpod_offers_fit_stock_and_preference():
    from gipper.market import admit, rank
    from gipper.providers.runpod import offers_from
    o = rank(admit(offers_from(GPUS, ["COMMUNITY"]), min_mem_gb=16))
    assert [x.gpu for x in o] == ["4090", "5090", "A5000"]  # stocked first, then price; 8 GB excluded
    o = rank(admit(offers_from(GPUS, ["COMMUNITY"]), min_mem_gb=16), prefer=["5090"])
    assert o[0].gpu == "5090"


def test_runpod_stock_is_per_country():
    from gipper.providers.runpod import offers_from
    o = {x.gpu: x for x in offers_from(GPUS, ["COMMUNITY"], ["US", "CA"])}
    assert o["4090"].country == "CA" and o["4090"].stock == "Low"   # only Canada has one
    assert o["5090"].stock is None and o["5090"].country == "US"     # no stock in either: kept, ranked last


def test_runpod_refuses_udp_ports():
    from gipper.providers import Unavailable
    from gipper.providers.runpod import _tcp_ports
    assert _tcp_ports(["8080/tcp", "8888/http"]) == ["22/tcp", "8080/tcp", "8888/http"]
    with pytest.raises(Unavailable):
        _tcp_ports(["5004/udp"])


VAST = [
    {"id": 1, "gpu_name": "RTX 4090", "gpu_ram": 24564, "dph_total": 0.38, "geolocation": "Michigan, US", "machine_id": 9},
    {"id": 2, "gpu_name": "RTX 4090", "gpu_ram": 24564, "dph_total": 0.39, "geolocation": "Saskatchewan, CA"},
    {"id": 3, "gpu_name": "RTX 4090", "gpu_ram": 24564, "dph_total": 0.40, "geolocation": "Texas, US"},
    {"id": 4, "gpu_name": "RTX 4090", "gpu_ram": 24564, "dph_total": 0.41, "geolocation": "Ohio, US"},
]


def test_vast_offers_and_ports():
    from gipper.providers.vast import _port_opts, offers_from
    o = offers_from(VAST)
    assert len(o) == 3 and o[0].mem_gb == 24 and o[0].country == "US" and o[1].country == "CA"   # 3 per GPU model
    assert o[0].udp and o[0].ref["ask"] == 1
    assert _port_opts(["5004/udp", "8080"]) == {"-p 5004:5004/udp": "1", "-p 8080:8080": "1"}


SHADE = [
    {"cloud": "massedcompute", "shade_instance_type": "A6000", "gpu_type": "A6000", "num_gpus": 1, "hourly_price": 57,
     "configuration": {"vram_per_gpu_in_gb": 48, "os_options": ["ubuntu22.04", "ubuntu22.04_cuda12.8_shade_os"]},
     "availability": [{"region": "desmoines-usa-1", "available": True, "display_name": "US, Des Moines", "rental_type": "on_demand"},
                      {"region": "desmoines-usa-1", "available": True, "display_name": "US, Des Moines", "rental_type": "spot"},
                      {"region": "oslo-norway-1", "available": False, "display_name": "NO, Oslo"}]},
    {"cloud": "massedcompute", "shade_instance_type": "A6000_plus", "gpu_type": "A6000", "num_gpus": 1, "hourly_price": 57,
     "configuration": {"vram_per_gpu_in_gb": 48},
     "availability": [{"region": "desmoines-usa-1", "available": True, "display_name": "US, Des Moines"}]},
    {"cloud": "x", "shade_instance_type": "H100x8", "gpu_type": "H100", "num_gpus": 8, "hourly_price": 2000,
     "configuration": {}, "availability": [{"region": "r", "available": True, "display_name": "US, r"}]},
]


def test_shadeform_offers():
    from gipper.providers.shadeform import _ufw, offers_from
    o = offers_from(SHADE)
    assert len(o) == 1   # on-demand only, available only, single GPU, duplicate instance types folded
    assert (o[0].country, o[0].usd_h, o[0].mem_gb) == ("US", 0.57, 48)
    assert o[0].ref["os"] == "ubuntu22.04_cuda12.8_shade_os" and o[0].udp
    assert _ufw(["5004/udp"]) == [{"port": "22", "proto": "tcp"}, {"port": "5004", "proto": "udp"}]


def test_names_match_across_providers():
    from gipper.market import matches, norm
    assert norm("NVIDIA GeForce RTX 4090") == norm("RTX 4090") == "4090"
    assert norm("NVIDIA RTX A6000") == norm("A6000") == norm("RTX A6000")
    assert norm("NVIDIA RTX 6000 Ada Generation") == norm("RTX6000Ada")
    assert matches("A100 80GB", "A100_80G") and matches("L40S", "NVIDIA L40S")
    assert not matches("4090", "RTX 5090")
    assert not matches("RTX 4090", "RTX 4090D") and not matches("L40", "L40S") and matches("A100", "A100 80GB")


def _offer(provider, gpu, usd, country="US", stock="yes", udp=False, mem=24):
    from gipper.market import Offer
    return Offer(provider, gpu, mem, usd, country=country, stock=stock, udp=udp)


def test_admit_regions_udp_and_memory():
    from gipper.market import admit
    offers = [_offer("runpod", "4090", 0.34, "IS", "Low"), _offer("vast", "RTX 4090", 0.38, udp=True),
              _offer("vast", "RTX 3060", 0.08, udp=True, mem=12)]
    assert [o.provider for o in admit(offers, regions=["us"])] == ["vast", "vast"]
    assert [o.gpu for o in admit(offers, min_mem_gb=20, ports=["5004/udp"])] == ["RTX 4090"]


def test_ceiling_follows_the_preferred_gpu():
    from gipper.market import ceiling
    offers = [_offer("vast", "RTX 3080", 0.13), _offer("vast", "RTX 4090", 0.43)]
    assert ceiling(offers) == pytest.approx(0.26)                       # cheap tail sets it with no preference
    assert ceiling(offers, prefer=["RTX 4090"]) == pytest.approx(0.86)  # but not over the card asked for
    assert ceiling(offers, job_max=0.5, prefer=["RTX 4090"]) == 0.5


def test_job_ports_regions_and_volume(tmp_path):
    from gipper.job import JobError
    j = _job(tmp_path, 'name="x"\nworkload="w"\n[run]\ncommand="true"\n[gpu]\nregions=["us","ca"]\n[pod]\nports=["5004/udp"]\n')
    assert j.regions == ["US", "CA"] and j.ports == ["5004/udp"] and j.image == ""
    with pytest.raises(JobError):
        _job(tmp_path, 'name="x"\nworkload="w"\n[run]\ncommand="true"\n[pod]\nports=["udp"]\n')
    with pytest.raises(JobError):
        _job(tmp_path, 'name="x"\nworkload="w"\n[run]\ncommand="true"\n[gpu]\nproviders=["vast"]\n[pod]\nvolume="v"\n')


def test_redact_scrubs_every_provider_key(monkeypatch):
    monkeypatch.setenv("VAST_API_KEY", "vast_SECRET_abc")
    from gipper.config import redact
    assert redact(f"a {SECRET} b vast_SECRET_abc") == "a <redacted> b <redacted>"


def test_rent_moves_to_the_next_offer(monkeypatch):
    from gipper import run as runmod
    from gipper.providers import Unavailable

    class P:
        name = "fake"

        def create(self, offer, job, pubkey, name):
            if offer.gpu == "gone":
                raise Unavailable("taken")
            return {"id": "i1", "usd_h": offer.usd_h, "gpu": offer.gpu}

    monkeypatch.setattr(runmod, "load", lambda name: P())
    queue = [_offer("fake", "gone", 0.1), _offer("fake", "ok", 0.2)]
    _, offer, made = runmod._rent(queue, None, "ssh-ed25519 AAAA", "gipper-x")
    assert offer.gpu == "ok" and made["id"] == "i1" and not queue
    with pytest.raises(Unavailable):
        runmod._rent([_offer("fake", "gone", 0.1)], None, "k", "n")


class FakeProvider:
    def __init__(self, name, machines):
        self.name, self.machines, self.terminated = name, machines, []

    def has_key(self):
        return True

    def list_ours(self):
        return self.machines

    def terminate(self, mid):
        self.terminated.append(mid)


def test_reaper_ends_only_forgotten_gipper_machines_on_every_provider():
    from gipper import ledger
    from gipper.reap import reap
    ledger.write("pod.create", {"pod": "live", "deadline": time.time() + 3600}, job="j1")   # old receipt: Runpod's
    ledger.write("pod.create", {"pod": "late", "deadline": time.time() - 10}, job="j2")
    ledger.write("pod.create", {"provider": "vast", "pod": "77", "deadline": time.time() + 3600}, job="j3")
    rp = FakeProvider("runpod", [{"id": "live", "name": "gipper-a"}, {"id": "late", "name": "gipper-b"},
                                 {"id": "orphan", "name": "gipper-c", "started": 0}])
    va = FakeProvider("vast", [{"id": 77, "name": "gipper-d"}, {"id": "live", "name": "gipper-e", "started": 0},
                               {"id": 78, "name": "gipper-f", "started": time.time()}])   # new: still in grace
    acted = reap(providers=[rp, va])
    assert sorted((p, str(i)) for p, i, _, _ in acted) == [("runpod", "late"), ("runpod", "orphan"), ("vast", "live")]
    assert rp.terminated == ["late", "orphan"] and va.terminated == ["live"]   # same id, other provider: not confused


def test_old_runpod_import_paths():
    from gipper.runpod.api import Api, RunpodError  # noqa: F401
    from gipper.runpod.run import _wait_ready  # noqa: F401
    import gipper.runpod.run as old
    assert Api()._key == SECRET and hasattr(Api(), "list_pods") and hasattr(Api(), "_gql")
    with pytest.raises(AttributeError, match="plan"):
        old.pick_gpu
