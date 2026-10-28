"""Private network probing without SSH/provider access."""
from types import SimpleNamespace
import pytest
from gipper import network_probe as module
from gipper.placement import load_placement


class Remote:
    host='test-host';port=22
    def _base(self): return ['ssh','test-host']
    def run(self,*args,**kw):
        self.cleanup=(args,kw)
        return SimpleNamespace(returncode=0)


def setup(monkeypatch, *, duration=(4,8), damage=False):
    clock=[0.0];payload=[None];calls=[]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    def run(cmd,**kw):
        calls.append((cmd,kw))
        if 'stdin' in kw:
            payload[0]=kw['stdin'].read();clock[0]+=duration[0]
        else:
            kw['stdout'].write(payload[0][:-1] if damage else payload[0]);clock[0]+=duration[1]
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess,'run',run)
    return calls


def test_threshold_rejects_slow_path(monkeypatch):
    setup(monkeypatch)
    result=module.probe(Remote(),load_placement({'probe_transfer_mb':1,'min_measured_download_mbps':10}))
    assert result['status']=='rejected' and 'download' in result['error']


def test_corrupt_or_incomplete_download_is_not_measurement(monkeypatch):
    setup(monkeypatch,damage=True)
    result=module.probe(Remote(),load_placement({'probe_transfer_mb':1}))
    assert result['status']=='error' and result['download_mbps'] is None


def test_real_timeout_reaps_child(monkeypatch,tmp_path):
    # Execute a LOCAL Python child in place of ssh; no network/model/provider.
    import os,sys
    pidfile=tmp_path/'pid'
    remote=Remote()
    remote._base=lambda:[sys.executable,'-c',f'import os,time;open({str(pidfile)!r},"w").write(str(os.getpid()));time.sleep(30)']
    result=module.probe(remote,load_placement({'probe_transfer_mb':1,'probe_timeout_s':.3}))
    assert result['status']=='error' and result['wall_s']<2
    with pytest.raises(ProcessLookupError): os.kill(int(pidfile.read_text()),0)
