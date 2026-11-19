"""CLI for explicitly owned serverless endpoints; never prints provider env."""
import json
import signal
from pathlib import Path

from gipper.config import redact
from gipper.serverless import Client, load


def parser(sub):
    group = sub.add_parser("serverless", help="queue workers (Runpod first; separate from Pod/SSH jobs)")
    commands = group.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "deploy", "run"):
        command = commands.add_parser(name)
        command.add_argument("spec", help="endpoint TOML")
        if name == "run":
            command.add_argument("input", help="handler input JSON file")
    for name in ("ls", "status", "submit", "wait", "cancel", "delete", "reap"):
        command = commands.add_parser(name)
        command.add_argument("--provider", default="runpod", choices=["runpod"])
        if name == "reap":
            command.add_argument("--dry-run", action="store_true")
        if name not in ("ls", "reap"):
            command.add_argument("endpoint")
        if name in ("wait", "cancel"):
            command.add_argument("job")
        if name == "submit":
            command.add_argument("input")
            command.add_argument("--execution-seconds", type=int, default=300)
        if name == "wait":
            command.add_argument("--timeout", type=float, default=600)


def main(args):
    spec = load(args.spec) if hasattr(args, "spec") else None
    client = Client(spec.provider if spec else args.provider)
    if args.cmd == "plan":
        result = {"provider": spec.provider, "name": spec.name, "image": spec.image,
                  "min_workers": 0, "max_workers": spec.max_workers,
                  "session_seconds": spec.session_seconds, "allowance_usd": spec.estimate_usd,
                  "rate_usd_h": spec.rate_usd_h, "basis": "caller supplied rate; occupancy allowance, not a bill or hard spending cap"}
    elif args.cmd == "deploy":
        result = {"endpoint": client.create(spec), "kept": True, "min_workers": 0}
    elif args.cmd == "run":
        def interrupted(signum, frame):
            raise KeyboardInterrupt("terminated; cleaning up")
        previous = signal.signal(signal.SIGTERM, interrupted)
        try:
            result = client.run(spec, json.loads(Path(args.input).read_text()))
        finally:
            signal.signal(signal.SIGTERM, previous)
    elif args.cmd == "reap":
        result = {"endpoints": client.reap(dry_run=args.dry_run), "dry_run": args.dry_run}
    elif args.cmd == "ls":
        result = [{k: r[k] for k in ("id", "name", "image", "workers") if k in r} for r in client.list()]
    elif args.cmd == "status":
        resource = client._owned(args.endpoint)
        result = {"present": resource is not None,
                  "workers": resource.get("workers") if resource else None,
                  "health": client.backend.health(args.endpoint) if resource else None}
    elif args.cmd == "submit":
        result = {"job": client.submit(args.endpoint, json.loads(Path(args.input).read_text()), args.execution_seconds)}
    elif args.cmd == "wait":
        result = client.wait(args.endpoint, args.job, args.timeout)
    elif args.cmd == "cancel":
        result = client.cancel(args.endpoint, args.job)
    else:
        client.delete(args.endpoint)
        result = {"endpoint": args.endpoint, "verified_absent": True}
    print(redact(json.dumps(result, indent=2)))
    if isinstance(result, dict) and result.get("status") in ("FAILED", "TIMED_OUT", "CANCELLED"):
        raise SystemExit(1)
