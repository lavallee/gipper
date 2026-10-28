"""Fail-closed provider termination: acceptance is not confirmed disappearance."""
import time
from gipper import ledger


def terminate(provider, iid, *, job_id=None, started=None, usd_h=None, refs=None, delete_accepted=False):
    iid=str(iid)
    base={'provider':provider.name,'pod':iid}
    ledger.write('pod.termination_pending', {**base,'delete_accepted':delete_accepted,'reason':'termination requested; provider disappearance unverified'},job=job_id)
    accepted=delete_accepted;error=None
    for attempt in range(0 if accepted else 5):
        try:
            provider.terminate(iid);accepted=True;break
        except Exception as failure:
            error=f'{type(failure).__name__}: {failure}'
            if attempt<4:time.sleep(2)
    absent=False
    if accepted:
        for attempt in range(4):
            try:
                machines=provider.list_ours()
                if not isinstance(machines,list) or any(not isinstance(m,dict) or 'id' not in m for m in machines):
                    raise ValueError('invalid provider visibility response')
                absent=not any(str(m['id'])==iid for m in machines)
                error=None if absent else 'provider still lists the instance'
                if absent:break
            except Exception as failure:
                error=f'{type(failure).__name__}: {failure}'
            if attempt<3:time.sleep(2)
    if not accepted or not absent:
        ledger.write('pod.termination_pending',{**base,'delete_accepted':accepted,'reason':error or 'closure unverified'},job=job_id)
        print(f'{provider.name} {iid}: termination pending; {error or "closure unverified"}',flush=True)
        return False
    ledger.write('pod.closure_verified',{**base,'basis':'accepted DELETE followed by provider-list absence'},job=job_id)
    ledger.write('pod.terminate',base,job=job_id)
    if started is not None and usd_h is not None:
        seconds=max(0,time.time()-started)
        ledger.write('spend',{**base,'seconds':round(seconds),'usd_per_h':usd_h,'usd':round(seconds*usd_h/3600,4),
                             'basis':'computed through verified closure','accuracy':'estimated'},job=job_id,refs=refs)
    return True
