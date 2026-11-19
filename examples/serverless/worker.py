"""Real CUDA smoke handler. Application logic stays independent of the host SDK."""
import os
import socket
import time

BOOT = time.time()


def assess(payload):
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA unavailable; refusing CPU fallback")
    n = int(payload.get("size", 256))
    if not 1 <= n <= 2048:
        raise ValueError("size must be between 1 and 2048")
    start = time.perf_counter()
    a = torch.ones((n, n), device="cuda")
    answer = (a @ a)[0, 0].item()
    return {"answer": answer, "expected": n, "device": str(a.device),
            "gpu": torch.cuda.get_device_name(), "worker": socket.gethostname(),
            "worker_started_at": BOOT, "compute_seconds": time.perf_counter() - start,
            "torch": torch.__version__, "pod_id": os.environ.get("RUNPOD_POD_ID")}


if __name__ == "__main__":
    import runpod
    runpod.serverless.start({"handler": lambda job: assess(job["input"])})
