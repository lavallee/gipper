"""Round trips from this machine: to a host's public IP before renting (system
`ping`; about 60% of Vast hosts answer), and to a rented machine's ssh port after
(TCP connect, no login). Both measure from *here*; for a job whose user is
elsewhere (`latency_target` names where its own measurements came from), they're
a proxy, used to rank but never to pass `max_rtt_ms`, unless the target is "here".

Vast's location labels can be wrong (on 2026-10-28 a "China" host answered in
80 ms from the IP an "United States" host had used), so a measured round trip
outranks the country it claims.
"""
import re
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

HERE = socket.gethostname()
_RTT = re.compile(r"= [\d.]+/([\d.]+)/")   # ping summary: min/avg/max/mdev -> avg


def ping_ms(ip, count=3, timeout_s=2):
    """Average ICMP round trip in ms, or None if the host doesn't answer."""
    try:
        r = subprocess.run(["ping", "-n", "-q", "-c", str(count), "-W", str(timeout_s), ip],
                           capture_output=True, text=True, timeout=count * timeout_s + 3)
    except (OSError, subprocess.SubprocessError):
        return None
    m = _RTT.search(r.stdout)
    return float(m.group(1)) if r.returncode == 0 and m else None


def ping_all(ips, workers=16):
    """{ip: ms or None}, pinged in parallel (about 6 s for any number up to `workers`)."""
    ips = sorted({ip for ip in ips if ip})
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(ips, pool.map(ping_ms, ips)))


def connect_ms(host, port, tries=5, timeout_s=3):
    """Fastest TCP connect to host:port in ms (a round trip, without logging in)."""
    best = None
    for _ in range(tries):
        t = time.perf_counter()
        try:
            with socket.create_connection((host, int(port)), timeout=timeout_s):
                ms = (time.perf_counter() - t) * 1000
        except OSError:
            continue
        best = ms if best is None else min(best, ms)
    return round(best, 1) if best is not None else None
