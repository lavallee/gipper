"""Print a runnable development spec without a Docker builder or registry.

Dependencies install at worker start. Use the Dockerfile for lower startup latency.
Check live stock and supply the current Serverless rate before running the spec.
"""
import argparse
import json
import shlex
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rate-usd-h", type=float, required=True)
    args = parser.parse_args()
    if args.rate_usd_h <= 0:
        parser.error("rate must be positive")
    worker = Path(__file__).with_name("worker.py").read_text()
    command = "python -m venv --system-site-packages /opt/gipper-worker && /opt/gipper-worker/bin/python -m pip install --no-cache-dir runpod==2.3.0 && exec /opt/gipper-worker/bin/python -u -c " + shlex.quote(worker)
    print('[endpoint]')
    print('name = ' + json.dumps('gipper-cuda-smoke-' + str(time.time_ns())))
    print('image = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"')
    print('command = ' + json.dumps(['/bin/bash', '-lc', command]))
    print('execution_seconds = 120\nsession_seconds = 600\nidle_seconds = 15\nmax_workers = 1')
    print('[budget]')
    print(f'rate_usd_h = {args.rate_usd_h}\nmax_usd = 0.50')
    print('[runpod]\npools = ["ADA_24"]\nmin_cuda_version = "12.8"\nflashboot = "FLASHBOOT"')


if __name__ == '__main__':
    main()
