"""Old import path `gipper.runpod.run`. Only `_wait_ready` is kept, for inspection
scripts. Everything else moved to `gipper.run` and works across providers: `plan()`
replaced `pick_gpu()`. Asking for any other name here fails loudly, so a script that
monkeypatches the old selection can't silently patch nothing."""
import time

from gipper.ssh import Remote


def _wait_ready(api, pod_id, timeout_s=900):
    """(pod, Remote) once the Runpod pod has a public IP and a mapped ssh port."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        p = api.get_pod(pod_id)
        port = (p.get("portMappings") or {}).get("22")
        if p.get("publicIp") and port:
            return p, Remote(p["publicIp"], port)
        time.sleep(5)
    raise RuntimeError(f"pod {pod_id} had no ssh endpoint after {timeout_s}s")


def __getattr__(name):
    raise AttributeError(f"gipper.runpod.run.{name} is gone: use `from gipper import run` "
                         "(run.plan() replaced pick_gpu; run.run(job, provider=...) picks the market)")
