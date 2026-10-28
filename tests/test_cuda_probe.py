import json
from types import SimpleNamespace
import pytest
from gipper.cuda_probe import check,CudaProbeError,compat_path


def good(path='/usr/local/nvidia/lib64'):
    return {'cu_init':0,'cu_device_get_count':0,'device_count':1,'ld_library_path':path,'loaded_library_paths':['/actual/driver/libcuda.so.580']}


class Remote:
    def __init__(self,*responses):self.responses=iter(responses);self.calls=[]
    def run(self,cmd,**kw):
        self.calls.append((cmd,kw));result=next(self.responses)
        if isinstance(result,Exception):raise result
        return SimpleNamespace(returncode=0,stdout=json.dumps(result))


def test_success_leaves_environment_unchanged_and_no_secret_env_dump():
    remote=Remote(good())
    result=check(remote,{'HF_TOKEN':'DO_NOT_SEND','SOME_SECRET':'NOPE'})
    assert result['status']=='ok' and result['environment_override']=={}
    assert len(remote.calls)==1
    assert 'DO_NOT_SEND' not in remote.calls[0][0] and 'NOPE' not in json.dumps(result)
    assert 'os.environ.items' not in remote.calls[0][0]


def test_804_retry_new_process_and_persist_exact_verified_path():
    original='/usr/local/cuda/compat:/usr/local/nvidia/lib64:/keep'
    remote=Remote({'cu_init':804,'ld_library_path':original,'loaded_library_paths':['/usr/local/cuda/compat/libcuda.so.1']},good('/usr/local/nvidia/lib64:/keep'))
    result=check(remote)
    assert len(remote.calls)==2 and remote.calls[1][0].startswith('LD_LIBRARY_PATH=/usr/local/nvidia/lib64:/keep python3')
    assert result['environment_override']=={'LD_LIBRARY_PATH':'/usr/local/nvidia/lib64:/keep'}
    assert result['removed_compat_entries']==['/usr/local/cuda/compat']
    assert result['attempts'][1]['loaded_library_paths']==['/actual/driver/libcuda.so.580']


@pytest.mark.parametrize('response',[{'cu_init':804,'ld_library_path':'/usr/local/cuda/compat'}, {'cu_init':999}, {'cu_init':0,'cu_device_get_count':0,'device_count':0}])
def test_sanitization_or_fatal_cuda_stays_failclosed(response):
    remote=Remote(response,{'cu_init':804,'ld_library_path':''})
    with pytest.raises(CudaProbeError) as error:check(remote)
    assert error.value.receipt['environment_override']=={}
    assert error.value.receipt['status']=='rejected'
    if response['cu_init']!=804:assert len(remote.calls)==1


def test_804_without_explicit_compat_entry_never_guesses_driver_path():
    remote=Remote({'cu_init':804,'ld_library_path':'/driver/lib'})
    with pytest.raises(CudaProbeError):check(remote)
    assert len(remote.calls)==1


def test_only_cuda_compat_entries_removed():
    assert compat_path('/usr/local/cuda-12.4/compat')
    assert compat_path('/usr/local/cuda-compat-12-4/lib')
    assert not compat_path('/opt/other/compat')
    assert not compat_path('/usr/local/cuda/lib64')


def test_missing_python_or_driver_rejects():
    remote=Remote(RuntimeError('python3 unavailable'))
    with pytest.raises(CudaProbeError,match='python3 unavailable'):check(remote)


def test_retry_environment_must_match_exact_job_override():
    remote=Remote({'cu_init':804,'ld_library_path':'/usr/local/cuda/compat:/keep'},good('/unexpected'))
    with pytest.raises(CudaProbeError):check(remote)
