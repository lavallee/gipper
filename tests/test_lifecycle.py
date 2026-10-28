import time
from types import SimpleNamespace
import pytest
from gipper import lifecycle,run
from gipper.job import Job
from gipper.market import Offer


@pytest.fixture
def receipts(monkeypatch):
    out=[]
    monkeypatch.setattr(lifecycle.ledger,'write',lambda kind,payload=None,**kw:out.append({'kind':kind,'payload':payload,**kw}))
    monkeypatch.setattr(lifecycle.time,'sleep',lambda _:None)
    return out


class Provider:
    name='vast'
    def __init__(self,responses=(),fail_delete=False):self.responses=iter(responses);self.deletes=0;self.fail_delete=fail_delete
    def terminate(self,iid):
        self.deletes+=1
        if self.fail_delete:raise RuntimeError('DELETE offline')
    def list_ours(self):
        r=next(self.responses)
        if isinstance(r,Exception):raise r
        return r


def kinds(out):return [r['kind'] for r in out]


def test_failed_delete_never_freezes_closure_or_spend(receipts):
    p=Provider(fail_delete=True)
    assert not lifecycle.terminate(p,'1',started=time.time()-100,usd_h=.2)
    assert p.deletes==5 and set(kinds(receipts))=={'pod.termination_pending'}


def test_accepted_delete_still_visible_remains_pending(receipts):
    p=Provider([[{'id':'1'}]]*4)
    assert not lifecycle.terminate(p,'1',started=time.time()-100,usd_h=.2)
    assert set(kinds(receipts))=={'pod.termination_pending'}


def test_visibility_outage_is_not_absence(receipts):
    p=Provider([RuntimeError('API down')]*4)
    assert not lifecycle.terminate(p,'1')
    assert 'pod.closure_verified' not in kinds(receipts)


def test_transient_outage_then_verified_absence(receipts):
    p=Provider([RuntimeError('API down'),[{'id':'1'}],[]])
    assert lifecycle.terminate(p,'1',started=time.time()-100,usd_h=.2)
    assert kinds(receipts)==['pod.termination_pending','pod.closure_verified','pod.terminate','spend']
    assert receipts[-1]['payload']['seconds']>=100


def test_pending_reaper_claim_has_priority(receipts,monkeypatch):
    from gipper.reap import state,verdict
    monkeypatch.setattr(lifecycle.ledger,'read',lambda **kw:iter([
        {'kind':'pod.create','job':'j','ts':'now','payload':{'provider':'vast','pod':'1','deadline':time.time()+99999}},
        {'kind':'pod.termination_pending','job':'j','payload':{'provider':'vast','pod':'1'}}]))
    rec=state()[('vast','1')]
    assert 'pending' in verdict(rec,{},time.time())
    assert not rec['terminated']


def test_run_finally_receipts_and_log_close_even_cleanup_throws(receipts,monkeypatch,tmp_path):
    import builtins
    from gipper.providers import BadHost
    j=Job(name='x',workload='x',command='true');p=Provider();o=Offer('vast','A40',48,.2)
    remote=SimpleNamespace(wait=lambda _:True)
    p.wait_ready=lambda *a:(remote,{})
    monkeypatch.setattr(run,'plan',lambda *a,**kw:([o],.4,[]))
    monkeypatch.setattr(run,'settings',lambda:{'daily_cap_usd':100})
    monkeypatch.setattr(run.ledger,'spend_since',lambda _:0)
    monkeypatch.setattr(run,'ensure_key',lambda:'public-key')
    allocations=[]
    def rent(*args):allocations.append(1);return p,o,{'id':'1','usd_h':.2,'gpu':'A40'}
    monkeypatch.setattr(run,'_rent',rent)
    monkeypatch.setattr(run,'preflight',lambda _:(_ for _ in ()).throw(BadHost('bad GPU')))
    monkeypatch.setattr(run,'_terminate',lambda *a:(_ for _ in ()).throw(RuntimeError('cleanup transport failed')))
    monkeypatch.setattr(run.signal,'signal',lambda *a:None)
    opened=[]
    def open_log(*args,**kw):
        handle=builtins.open(*args,**kw);opened.append(handle);return handle
    monkeypatch.setattr(run,'open',open_log,raising=False)
    _,outcome=run.run(j,log_dir=tmp_path/'log')
    assert outcome=='termination_pending' and len(allocations)==1
    assert receipts[-1]['kind']=='run.end' and receipts[-1]['payload']['outcome']=='termination_pending'
    assert opened[0].closed


def test_reaper_confirms_previously_accepted_now_absent(receipts,monkeypatch):
    from gipper import reap
    p=Provider([[],[]]);p.has_key=lambda:True
    monkeypatch.setattr(reap,'state',lambda:{('vast','1'):{'job':'j','terminated':False,'pending':True,
                        'delete_accepted':True,'started':time.time()-100,'usd_h':.2}})
    acted=reap.reap(providers=[p])
    assert len(acted)==1 and p.deletes==0
    assert 'pod.closure_verified' in kinds(receipts) and 'spend' in kinds(receipts)
