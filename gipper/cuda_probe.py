"""Initialize the actual CUDA driver before any model/input upload.

nvidia-smi uses NVML and cannot prove cuInit succeeds. Only an 804 may trigger a
fresh-process retry excluding explicit CUDA forward-compatibility path entries.
No driver paths are guessed, libraries installed, or host configuration changed.
"""
import json
import shlex
from pathlib import PurePosixPath
from gipper.providers import BadHost

SCRIPT = r'''
import ctypes,json,os
result={"ld_library_path":os.environ.get("LD_LIBRARY_PATH",""),"loaded_library_paths":[],"device_count":0}
try:
    cuda=ctypes.CDLL("libcuda.so.1")
    cuda.cuInit.argtypes=[ctypes.c_uint];cuda.cuInit.restype=ctypes.c_int
    code=cuda.cuInit(0);result["cu_init"]=code
    if code==0:
        count=ctypes.c_int()
        cuda.cuDeviceGetCount.argtypes=[ctypes.POINTER(ctypes.c_int)];cuda.cuDeviceGetCount.restype=ctypes.c_int
        result["cu_device_get_count"]=cuda.cuDeviceGetCount(ctypes.byref(count));result["device_count"]=count.value
    try:
        result["loaded_library_paths"]=sorted({line.split()[-1] for line in open("/proc/self/maps") if "/libcuda.so" in line})
    except OSError: pass
except Exception as error:
    result["error"]=type(error).__name__+": "+str(error)
print(json.dumps(result))
'''


class CudaProbeError(BadHost):
    def __init__(self, message, receipt):
        super().__init__(message)
        self.receipt=receipt


def compat_path(value):
    parts=PurePosixPath(value).parts
    return (len(parts)>1 and parts[-1]=='compat' and parts[-2].startswith('cuda')) or any(p.startswith('cuda-compat-') for p in parts)


def initialized(result):
    return result.get('cu_init')==0 and result.get('cu_device_get_count')==0 and isinstance(result.get('device_count'),int) and result['device_count']>0


def check(remote, env=None):
    receipt={'attempts':[], 'environment_override':{}, 'status':'rejected',
             'basis':'stdlib ctypes libcuda.so.1 cuInit(0) and cuDeviceGetCount; fresh Python per attempt'}
    def attempt(ld=None):
        prefix=('LD_LIBRARY_PATH='+shlex.quote(ld)+' ') if ld is not None else ''
        response=remote.run(prefix+'python3 -c '+shlex.quote(SCRIPT),check=False,timeout=30)
        if response.returncode:
            raise RuntimeError('CUDA Python preflight exited '+str(response.returncode))
        data=json.loads(response.stdout)
        if not isinstance(data,dict):raise ValueError('invalid CUDA preflight response')
        receipt['attempts'].append(data)
        return data
    try:
        first=attempt((env or {}).get('LD_LIBRARY_PATH'))
        if initialized(first):
            receipt['status']='ok';return receipt
        if first.get('cu_init')==804:
            path=first.get('ld_library_path','')
            if not isinstance(path,str): raise ValueError('invalid CUDA library path response')
            entries=path.split(':')
            removed=[p for p in entries if compat_path(p)]
            if removed:
                cleaned=':'.join(p for p in entries if not compat_path(p))
                receipt['removed_compat_entries']=removed
                second=attempt(cleaned)  # new SSH/Python; never retry CDLL in loaded process
                if initialized(second) and second.get("ld_library_path") == cleaned:
                    receipt.update(status='ok',environment_override={'LD_LIBRARY_PATH':cleaned})
                    return receipt
        receipt['error']='CUDA initialization/device enumeration failed: '+json.dumps(receipt['attempts'][-1])
    except Exception as error:
        receipt['error']=f'{type(error).__name__}: {error}'
    raise CudaProbeError(receipt['error'],receipt)
