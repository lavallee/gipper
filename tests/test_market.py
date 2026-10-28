"""Rules every market shares: GPU names, what an offer must have, how they're ordered."""
from gipper.market import Offer, admit, matches, norm, rank
from gipper.placement import Placement


def test_gpu_names_match_across_providers():
    assert norm("NVIDIA GeForce RTX 4090") == norm("RTX 4090") == "4090"
    assert norm("NVIDIA RTX A6000") == norm("A6000") and norm("NVIDIA RTX 6000 Ada Generation") == norm("RTX6000Ada")
    assert matches("A100 80GB", "A100_80G") and matches("L40S", "NVIDIA L40S") and matches("A100", "A100 80GB")
    assert not matches("4090", "RTX 5090") and not matches("RTX 4090", "RTX 4090D") and not matches("L40", "L40S")


def _o(provider, gpu, usd, country="US", udp=False, mem=24, **metrics):
    return Offer(provider, gpu, mem, usd, country=country, stock="yes", udp=udp, metrics=metrics)


def test_admit_regions_udp_memory_and_floors():
    offers = [_o("runpod", "4090", 0.34, "IS"), _o("vast", "RTX 4090", 0.38, udp=True, cuda_max_good=12.4),
              _o("vast", "RTX 3060", 0.08, udp=True, mem=12), _o("vast", "A40", 0.3, udp=True)]
    assert [o.gpu for o in admit(offers, regions=["us"])] == ["RTX 4090", "RTX 3060", "A40"]
    assert [o.gpu for o in admit(offers, min_mem_gb=20, ports=["5004/udp"])] == ["RTX 4090", "A40"]
    # a floor rejects what is advertised below it; unadvertised passes (checked on the machine)
    assert [o.gpu for o in admit(offers, min_mem_gb=20, placement=Placement(min_cuda=12.8))] == ["4090", "A40"]


def test_preference_then_stock_then_price():
    unknown = Offer("runpod", "NVIDIA RTX A5000", 24, 0.16, stock=None)
    cheap, dear = _o("vast", "RTX 3090", 0.20), _o("vast", "RTX 4090", 0.40)
    assert [o.gpu for o in rank([unknown, dear, cheap])] == ["RTX 3090", "RTX 4090", "NVIDIA RTX A5000"]
    assert rank([unknown, dear, cheap], prefer=["RTX 4090"])[0].gpu == "RTX 4090"


def test_interactive_jobs_go_to_the_measured_nearest_host():
    from gipper.job import Job
    from gipper.placement import load_placement
    p = load_placement({"profile": "interactive", "latency_target": "toronto", "preferred_countries": ["CA"],
                        "measured_rtt_ms_by_machine": {"vast:machine:far": 90, "vast:machine:near": 20}})
    job = Job(name="x", workload="w", command="true", placement=p)
    far = Offer("vast", "RTX 4090", 24, 0.30, country="US", stock="yes", ref={"machine": "far"})
    near = Offer("vast", "RTX 4090", 24, 0.60, country="US", stock="yes", ref={"machine": "near"})
    canada = Offer("vast", "RTX 4090", 24, 0.20, country="CA", stock="yes", ref={"machine": "unmeasured"})
    assert [o.ref["machine"] for o in rank([canada, far, near], job=job)] == ["near", "far", "unmeasured"]
    p.max_rtt_ms = 50   # a limit on round trip fails what wasn't measured
    assert [o.ref["machine"] for o in admit([canada, far, near], placement=p)] == ["near"]
