"""Round trips: what counts as measured, what's only a proxy, and that batch work
can still go far away when it's cheaper."""
from conftest import receipt

from gipper.history import History
from gipper.job import Job
from gipper.latency import HERE, _RTT
from gipper.market import Offer, admit, rank
from gipper.placement import Placement, best_rtt, load_placement


def test_reads_linux_ping_summary():
    out = "3 packets transmitted, 3 received, 0% packet loss, time 2003ms\nrtt min/avg/max/mdev = 25.992/26.396/27.188/0.559 ms\n"
    assert float(_RTT.search(out).group(1)) == 26.396


def _offer(machine, country, usd=0.4, pinged=None):
    return Offer("vast", "RTX 4090", 24, usd, country=country, stock="yes", ref={"machine": machine},
                 metrics={"rtt_ms_here": pinged} if pinged is not None else {})


def test_best_round_trip_in_order_of_trust():
    h = History.load(records=[
        receipt("market.rtt", {"origin": HERE, "samples": [{"provider": "vast", "machine": "seen", "country": "AU", "ms": 240}]}),
        receipt("market.rtt", {"origin": HERE, "samples": [{"provider": "vast", "machine": "other", "country": "CA", "ms": 30}]}),
        receipt("market.rtt", {"origin": "elsewhere", "samples": [{"provider": "vast", "machine": "x", "country": "CA", "ms": 999}]})])
    p = load_placement({"profile": "interactive", "latency_target": "toronto",
                        "measured_rtt_ms_by_machine": {"vast:machine:own": 12}})
    assert best_rtt(_offer("own", "US", pinged=80), p, h) == (12, "measured")
    assert best_rtt(_offer("new", "US", pinged=80), p, h) == (80, "pinged from here (proxy)")
    assert best_rtt(_offer("seen", "AU"), p, h) == (240, "memory of this machine")
    assert best_rtt(_offer("stranger", "CA"), p, h) == (30, "memory of CA")      # other origins don't count
    assert best_rtt(_offer("stranger", "NZ"), p, h) == (None, "unknown")


def test_pings_pass_max_rtt_only_when_the_user_is_here():
    near, far = _offer("a", "CA", pinged=25), _offer("b", "AU", pinged=240)
    elsewhere = load_placement({"profile": "interactive", "latency_target": "toronto", "max_rtt_ms": 50})
    here = load_placement({"profile": "interactive", "latency_target": "here", "max_rtt_ms": 50})
    assert admit([near, far], placement=elsewhere) == []          # a ping from here isn't the user's round trip
    assert admit([near, far], placement=here) == [near]


def test_batch_work_can_go_far_when_it_is_cheaper():
    job = Job(name="x", workload="w", command="true", max_hours=24, placement=Placement(compute_s=6 * 3600))
    sydney, quebec = _offer("au", "AU", usd=0.30, pinged=240), _offer("ca", "CA", usd=0.45, pinged=26)
    assert rank([quebec, sydney], job=job, history=History.load(records=[]))[0] is sydney
    job.placement = load_placement({"profile": "interactive", "compute_s": 6 * 3600})
    assert rank([quebec, sydney], job=job, history=History.load(records=[]))[0] is quebec


def test_link_speed_memory_is_per_country_before_per_provider():
    recs = []
    for pod, country, secs in (("1", "AU", 40), ("2", "US", 4)):
        recs += [receipt("pod.create", {"provider": "vast", "pod": pod, "machine": pod, "country": country}, job=pod),
                 receipt("transfer", {"provider": "vast", "pod": pod, "direction": "up", "bytes": 100e6, "seconds": secs}, job=pod)]
    h = History.load(records=recs)
    assert h.mbps("vast", "new", "up", "AU") == 20 and h.mbps("vast", "new", "up", "US") == 200
    assert h.mbps("vast", "new", "up", "NZ") == 110      # no record for the country: the provider's median
