"""Small opt-in private SSH transfer probe; no public speed-test endpoint.

subprocess.run owns, kills and waits for its SSH child on timeout. No pipeline
children are created. Timings include SSH setup, encryption and local disk I/O;
these are path goodput observations, never RTT measurements.
"""
import hashlib
import os
import shlex
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def probe(remote, profile):
    size = int(profile.probe_transfer_mb * 1024**2)  # configuration MB is explicitly MiB
    start = time.monotonic()
    deadline = start + profile.probe_timeout_s
    cleanup_reserve = min(3.0, profile.probe_timeout_s / 4)
    remote_path = '/tmp/gipper-network-probe-' + uuid.uuid4().hex
    quoted = shlex.quote(remote_path)
    receipt = {'origin_hostname': socket.gethostname(), 'destination_host': remote.host,
               'destination_port': remote.port, 'bytes_per_direction': size,
               'basis': 'random private bytes over Remote SSH; wall goodput includes SSH overhead, not RTT',
               'status': 'error', 'upload_mbps': None, 'download_mbps': None,
               'min_upload_mbps': profile.min_measured_upload_mbps,
               'min_download_mbps': profile.min_measured_download_mbps}
    def remaining():
        value = deadline - time.monotonic() - cleanup_reserve
        if value <= 0:
            raise subprocess.TimeoutExpired('network probe', profile.probe_timeout_s)
        return value
    try:
        with tempfile.TemporaryDirectory(prefix='gipper-network-probe-') as directory:
            native, returned = Path(directory)/'random', Path(directory)/'returned'
            native.write_bytes(os.urandom(size))
            with native.open('rb') as src:
                t = time.monotonic()
                result = subprocess.run(remote._base() + [f"umask 077; trap 'rm -f {quoted}' HUP INT TERM; cat > {quoted}"],
                                        stdin=src, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=remaining())
                elapsed = time.monotonic() - t
            if result.returncode:
                raise RuntimeError('probe upload SSH failed')
            receipt.update(upload_s=elapsed, upload_mbps=size*8/1e6/max(elapsed,1e-9))
            with returned.open('wb') as dst:
                t = time.monotonic()
                result = subprocess.run(remote._base() + [f"trap 'rm -f {quoted}' EXIT HUP INT TERM; cat {quoted}"],
                                        stdout=dst, stderr=subprocess.PIPE, timeout=remaining())
                elapsed = time.monotonic() - t
            if result.returncode:
                raise RuntimeError('probe download SSH failed')
            if returned.stat().st_size != size or hashlib.sha256(native.read_bytes()).digest() != hashlib.sha256(returned.read_bytes()).digest():
                raise RuntimeError('probe roundtrip bytes failed integrity check')
            receipt.update(download_s=elapsed, download_mbps=size*8/1e6/max(elapsed,1e-9))
            failures = [f'{direction} below configured minimum' for direction in ('upload','download')
                        if (getattr(profile, f'min_measured_{direction}_mbps') is not None
                            and receipt[f'{direction}_mbps'] < getattr(profile, f'min_measured_{direction}_mbps'))]
            receipt['status'] = 'rejected' if failures else 'ok'
            if failures:
                receipt['error'] = '; '.join(failures)
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        receipt['error'] = f'{type(error).__name__}: {error}'
    finally:
        # Best effort within the SAME total deadline; rental teardown is separate.
        budget = deadline - time.monotonic()
        if budget > 0:
            try:
                result = remote.run(f'rm -f {quoted}', check=False, timeout=budget)
                receipt['remote_cleanup_confirmed'] = result.returncode == 0
            except (OSError, subprocess.SubprocessError, RuntimeError):
                receipt['remote_cleanup_confirmed'] = False
        else:
            receipt['remote_cleanup_confirmed'] = False
        receipt['wall_s'] = time.monotonic() - start
    return receipt
