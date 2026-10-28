"""Workload placement contracts: entirely offline, no allocations."""
import math
from types import SimpleNamespace
import pytest
from gipper.job import Job, load
from gipper.market import Offer, rank, admit
from gipper.placement import load_placement, estimate_offer
from gipper.providers.vast import offers_from, Vast


def job(**kw):
    return Job(name='test', workload='voice', command='true', placement=load_placement(kw))


def offer(price=.2, down=100, up=100, gpu='A40', country='US', machine=1):
    return Offer('vast', gpu, 48, price, country=country, stock='yes', ref={'machine': machine},
                 metrics={'host_download_mbps':down,'host_upload_mbps':up})


@pytest.mark.parametrize('fields', [dict(upload_gb=-1),dict(setup_s=math.nan),dict(transfer_efficiency=0),
    dict(transfer_efficiency=2),dict(compute_s_by_gpu={'A40':False}),dict(max_rtt_ms=50),
    dict(near_latitude=20),dict(profile='oops'),dict(typo=3),dict(min_cuda=math.inf)])
def test_invalid_profile(fields):
    with pytest.raises(ValueError): load_placement(fields)


def test_profile_opt_in(tmp_path):
    p=tmp_path/'job.toml'; p.write_text('name="x"\nworkload="x"\n[run]\ncommand="true"\n')
    assert load(p).placement is None
    p.write_text(p.read_text()+'[placement]\nsetup_download_gb=5\n')
    assert load(p).placement.setup_download_gb == 5


def test_total_cost_can_favor_higher_hourly():
    j=job(upload_gb=10,setup_s=0,compute_s=100,transfer_efficiency=1)
    slow,fast=offer(.1,10),offer(.3,1000)
    assert rank([slow,fast], job=j)[0] is fast
    assert fast.placement_estimate['rental_usd'] < slow.placement_estimate['rental_usd']
    assert rank([slow,fast])[0] is slow


def test_external_setup_ignores_client_uplink_cap():
    e=estimate_offer(job(upload_gb=1,setup_download_gb=1,download_gb=1,client_upload_mbps=10,
                         client_download_mbps=20,transfer_efficiency=1), offer(down=1000,up=1000))
    assert e['upload_s']==800 and e['setup_download_s']==8 and e['download_s']==400
    assert e['bandwidth_fees_known'] is False


def test_compute_measured_by_workload_not_dlperf():
    j=job(compute_s=1000,compute_s_by_gpu={'A40':400})
    a,b=offer(),offer(gpu='L40S'); a.metrics['dlperf']=1; b.metrics['dlperf']=999
    assert estimate_offer(j,a)['compute_s']==400
    assert estimate_offer(j,b)['compute_s']==1000


@pytest.mark.parametrize('bad', [None,0,-1,float('nan'),float('inf')])
def test_missing_network_fallback_and_threshold_failclosed(bad):
    a=offer(down=bad)
    assert math.isfinite(estimate_offer(job(upload_gb=1),a)['wall_s'])
    assert rank([a],job=job(min_host_download_mbps=500)) == []


def test_batch_ignores_country_but_interactive_does_not_invent_rtt():
    a,b=offer(country='TR'),offer(country='US')
    assert rank([a,b],job=job(preferred_countries=['US']))[0] is a
    assert rank([a,b],job=job(profile='interactive',preferred_countries=['US']))[0] is b
    assert b.placement_estimate['measured_rtt_ms'] is None
    assert rank([a,b],job=job(profile='interactive',max_rtt_ms=30,latency_target='toronto')) == []
    j=job(profile='interactive',max_rtt_ms=30,latency_target='toronto',measured_rtt_ms_by_machine={'vast:machine:2':20})
    b.ref['machine']=2
    assert rank([a,b],job=j)==[b]


def test_vast_profile_keeps_network_variety_and_decimal_memory(monkeypatch):
    rows=[{'id':i,'gpu_name':'A40','gpu_ram':46068,'dph_total':.1+i*.01,'inet_down':100+i*200,'inet_up':200,
           'compute_cap':860,'cuda_max_good':12.8} for i in range(4)]
    assert len(offers_from(rows))==3
    assert len(offers_from(rows,per_gpu=None,decimal_memory=True))==4
    q={}
    v=Vast(); monkeypatch.setattr(v,'has_key',lambda:False)
    def request(method,path,body,**kw): q.update(body);return {'offers':rows}
    monkeypatch.setattr(v,'_req',request)
    j=job(min_host_download_mbps=500,min_host_upload_mbps=100,min_compute_capability=8.0)
    j.min_mem_gb=48
    result=v.offers(j)
    assert q['gpu_ram']['gte'] < 46068
    assert q['inet_down']['gte']==500 and q['compute_cap']['gte']==800
    assert len(admit(result,min_mem_gb=48))==4
    assert result[0].metrics['compute_capability']==8.6


def test_estimate_exposes_auditable_breakdown_without_legacy_change():
    from gipper.run import estimate
    j=job(setup_s=100,compute_s=400,upload_gb=0)
    assert estimate(j,.2,offer=offer())['basis']=='placement'
    j.placement=None
    assert estimate(j,.2)=={'wall_s':2220,'usd':.123,'basis':'guess'}


def test_profile_hourly_ceiling_never_silently_bypassed(monkeypatch):
    from gipper import run
    j=job();j.max_usd_per_h=.1
    monkeypatch.setattr(run.market,'shop',lambda *a,**kw:([offer(.3)],[]))
    with pytest.raises(SystemExit,match='hourly ceiling'): run.plan(j)
    j.max_usd_per_h=None
    assert run.plan(j)[0][0].usd_h==.3


@pytest.mark.parametrize('field',['setup_s','compute_s','download_gb','setup_download_gb','fallback_mbps'])
def test_required_default_cannot_be_null(field):
    with pytest.raises(ValueError): load_placement({field:None})


def test_advertised_bandwidth_changes_cost_ranking():
    a,b=offer(.1),offer(.3)
    a.metrics.update(host_download_usd_per_gb=.5,host_upload_usd_per_gb=.2)
    b.metrics.update(host_download_usd_per_gb=.001,host_upload_usd_per_gb=.001)
    j=job(upload_gb=1,setup_s=0,compute_s=100)
    assert rank([a,b],job=j)[0] is b
    assert a.placement_estimate['rental_usd'] < b.placement_estimate['rental_usd']


def test_fee_directions_cold_dependencies_and_probe_bytes():
    a=offer();a.metrics.update(host_download_usd_per_gb=.1,host_upload_usd_per_gb=.3)
    j=job(upload_gb=2,setup_download_gb=5,download_gb=3,probe_transfer_mb=16)
    e=estimate_offer(j,a);probe=16*1024**2/1e9
    assert e['incoming_gb']==pytest.approx(7+probe)
    assert e['outgoing_gb']==pytest.approx(3+probe)
    assert e['estimated_bandwidth_usd']==pytest.approx(round((7+probe)*.1+(3+probe)*.3,6))
    assert e['estimated_total_usd']==pytest.approx(e['rental_usd']+e['estimated_bandwidth_usd'],abs=1e-6)
    assert not e['cost_is_lower_bound']


def test_unknown_fees_are_explicit_not_zero():
    a=offer();a.metrics['host_download_usd_per_gb']=.1
    e=estimate_offer(job(upload_gb=2,download_gb=1),a)
    assert e['estimated_bandwidth_usd'] is None and e['host_upload_usd_per_gb'] is None
    assert e['known_bandwidth_usd']==.2 and e['cost_is_lower_bound']
    assert e['usd']==pytest.approx(e['rental_usd']+.2,abs=1e-6)
    a.metrics.update(host_upload_usd_per_gb=0,host_download_usd_per_gb=0)
    e=estimate_offer(job(),a)
    assert e['bandwidth_fees_known'] and e['estimated_bandwidth_usd']==0


def test_vast_raw_fee_mapping_and_throughput_override_keep_fees():
    from gipper.run import estimate
    a=offers_from([{'id':1,'gpu_name':'A40','gpu_ram':46068,'dph_total':.2,
                    'inet_down_cost':.1,'inet_up_cost':.3}])[0]
    assert a.metrics['host_download_usd_per_gb']==.1 and a.metrics['host_upload_usd_per_gb']==.3
    j=job(upload_gb=1,download_gb=1);j.units_count=10
    result=estimate(j,.2,throughput=1,offer=a)
    assert result['estimated_bandwidth_usd']==.4
    assert result['usd']==pytest.approx(result['rental_usd']+.4,abs=1e-6)
