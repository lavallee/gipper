"""gipper: GPU work for the estate.

    gipper pod gpus [--min-mem GB] [--region US,CA] [--udp] [--provider P]   offers across markets
    gipper pod estimate JOB.toml [--throughput U/S] [--provider P]           what a run would cost
    gipper pod run JOB.toml [--dry-run] [--gpu NAME] [--provider P]          rent, run, collect, terminate
    gipper pod ls                                               gipper's machines now running, every provider
    gipper pod reap [--dry-run]                                 end machines gipper forgot
    gipper pod cost [--since YYYY-MM-DD]                        spend per job from the ledger
    gipper pod account                                          balance per provider
    gipper ledger [--since YYYY-MM-DD] [--job ID]               raw receipts

Providers: runpod, vast, shadeform (keys RUNPOD_API_KEY, VAST_API_KEY,
SHADEFORM_API_KEY in gipper/.env). Vast and Shadeform list offers without a key.
"""
import argparse
import json
import sys
from collections import defaultdict


def _providers():
    from gipper.config import settings
    from gipper.providers import load
    return [load(n) for n in settings()["providers"]]


def _pod(args):
    if args.cmd == "gpus":
        from gipper import market
        from gipper.job import Job
        job = Job(name="gpus", workload="-", command="-", min_mem_gb=args.min_mem, cloud=args.cloud,
                  regions=[r.strip().upper() for r in (args.region or "").split(",") if r.strip()],
                  ports=["9/udp"] if args.udp else [])
        offers, notes = market.shop(job, names=[args.provider] if args.provider else None, keyed_only=False)
        keyed = {p.name for p in _providers() if p.has_key()}
        print(f"{'provider':10s} {'gpu':34s} {'GB':>4s} {'$/h':>6s}  {'stock':6s} where")
        for o in offers[:args.limit]:
            print(f"{o.provider:10s} {o.gpu[:34]:34s} {o.mem_gb:>4.0f} {o.usd_h:>6.2f}  {o.stock or '-':6s} {o.where}"
                  + ("" if o.provider in keyed else "  (no key)"))
        for n in notes:
            print(f"note: {n}")
        return
    if args.cmd == "account":
        for p in _providers():
            if not p.has_key():
                print(f"{p.name:10s} no {p.key_name}")
                continue
            try:
                print(f"{p.name:10s} {p.account()}")
            except Exception as e:
                print(f"{p.name:10s} error: {e}")
        return
    if args.cmd in ("estimate", "run"):
        from gipper import run as runmod
        from gipper.job import load
        job = load(args.job)
        if args.cmd == "estimate":
            offers, ceiling, notes = runmod.plan(job, provider=args.provider)
            for o in offers[:8]:
                e = runmod.estimate(job, o.usd_h, args.throughput)
                print(f"{o.provider:10s} {o.gpu[:30]:30s} ${o.usd_h:.2f}/h  ~{e['wall_s'] / 60:.0f} min  ~${e['usd']:.3f}  "
                      f"({e['basis']}; {o.where}; stock {o.stock or '-'})")
            print(f"price ceiling ${ceiling:.2f}/h" + "".join(f"\nnote: {n}" for n in notes))
            return
        _, outcome = runmod.run(job, dry_run=args.dry_run, gpu_override=args.gpu, provider=args.provider,
                                throughput=args.throughput)
        sys.exit(0 if outcome in ("succeeded", None) or args.dry_run else 1)
    if args.cmd == "ls":
        for p in _providers():
            if not p.has_key():
                continue
            try:
                for m in p.list_ours():
                    print(f"{p.name:10s} {m['id']}  {m['name']}  {m.get('status')}  ${m.get('usd_h')}/h")
            except Exception as e:
                print(f"{p.name:10s} error: {e}")
        return
    if args.cmd == "reap":
        from gipper.reap import reap
        acted = reap(dry_run=args.dry_run)
        for prov, pid, name, why in acted:
            print(f"{'would end' if args.dry_run else 'ended'} {prov} {pid} {name}: {why}")
        if not acted:
            print("nothing to reap")
        return
    if args.cmd == "cost":
        from gipper import ledger
        per = defaultdict(lambda: {"usd": 0.0, "s": 0, "name": "", "outcome": ""})
        for r in ledger.read(since=args.since, kinds={"spend", "run.end"}):
            j = per[r["job"]]
            j["name"] = r["refs"].get("name", j["name"])
            if r["kind"] == "spend":
                j["usd"] += r["payload"]["usd"]
                j["s"] += r["payload"]["seconds"]
            else:
                j["outcome"] = r["payload"]["outcome"]
        total = 0.0
        for jid, j in per.items():
            if j["s"]:
                print(f"{jid}  {j['name'][:30]:30s} {j['s'] / 60:6.1f} min  ${j['usd']:.3f}  {j['outcome']}")
                total += j["usd"]
        print(f"total ${total:.3f}")


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)  # live output when redirected to a file
    ap = argparse.ArgumentParser(prog="gipper", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="group")
    pod = sub.add_parser("pod", help="rented GPUs (Runpod, Vast, Shadeform)")
    ps = pod.add_subparsers(dest="cmd", required=True)
    providers = ["runpod", "vast", "shadeform"]
    g = ps.add_parser("gpus")
    g.add_argument("--min-mem", type=float, default=16)
    g.add_argument("--region", help="ISO country codes, comma-separated (US,CA)")
    g.add_argument("--udp", action="store_true", help="only providers that can expose UDP ports")
    g.add_argument("--provider", choices=providers)
    g.add_argument("--cloud", default="COMMUNITY", choices=["COMMUNITY", "SECURE"], help="Runpod cloud to start from")
    g.add_argument("--limit", type=int, default=30)
    for name in ("estimate", "run"):
        p = ps.add_parser(name)
        p.add_argument("job")
        p.add_argument("--throughput", type=float, help="units per second, if known")
        p.add_argument("--provider", choices=providers, help="shop only this provider")
        if name == "run":
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--gpu", help="only this GPU (e.g. \"RTX 4090\")")
    ps.add_parser("ls")
    ps.add_parser("account")
    r = ps.add_parser("reap")
    r.add_argument("--dry-run", action="store_true")
    c = ps.add_parser("cost")
    c.add_argument("--since")
    lg = sub.add_parser("ledger", help="raw receipts")
    lg.add_argument("--since")
    lg.add_argument("--job")
    a = ap.parse_args(argv)
    if a.group == "pod":
        return _pod(a)
    if a.group == "ledger":
        from gipper import ledger
        for rec in ledger.read(since=a.since, job=a.job):
            print(json.dumps(rec))
        return
    ap.print_help()


if __name__ == "__main__":
    main()
