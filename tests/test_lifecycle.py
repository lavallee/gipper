import time
import pytest
from gipper import lifecycle


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


def test_reaper_confirms_previously_accepted_now_absent(receipts,monkeypatch):
    from gipper import reap
    p=Provider([[],[]]);p.has_key=lambda:True
    monkeypatch.setattr(reap,'state',lambda:{('vast','1'):{'job':'j','terminated':False,'pending':True,
                        'delete_accepted':True,'started':time.time()-100,'usd_h':.2}})
    acted=reap.reap(providers=[p])
    assert len(acted)==1 and p.deletes==0
    assert 'pod.closure_verified' in kinds(receipts) and 'spend' in kinds(receipts)
