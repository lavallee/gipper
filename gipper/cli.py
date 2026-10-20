"""gipper: GPU work for the estate.

    gipper pod gpus [--min-mem GB] [--cloud COMMUNITY|SECURE]   types that fit, live prices
    gipper pod estimate JOB.toml [--throughput U/S]             what a run would cost
    gipper pod run JOB.toml [--dry-run] [--gpu TYPE]            rent, run, collect, terminate
    gipper pod ls                                               gipper's pods now running
    gipper pod reap [--dry-run]                                 end pods gipper forgot
    gipper pod cost [--since YYYY-MM-DD]                        spend per job from the ledger
    gipper pod account                                          balance and spend limit
    gipper ledger [--since YYYY-MM-DD] [--job ID]               raw receipts
"""
import argparse
import json
import sys
from collections import defaultdict


def _pod(args):
    from gipper.runpod.api import Api
    if args.cmd == "gpus":
        from gipper.runpod.select import candidates
        rows = candidates(Api().gpu_types(), args.min_mem, args.cloud)
        print(f"{'gpu':40s} {'GB':>4s} {'$/h':>6s}  stock")
        for g, p, stock in rows:
            print(f"{g['id'][:40]:40s} {g['memoryInGb']:>4} {p:>6.2f}  {stock or '-'}")
        return
    if args.cmd == "account":
        a = Api().account()
        print(f"balance ${a['clientBalance']:.2f}; spending ${a['currentSpendPerHr']:.2f}/h now; spend limit {a['spendLimit']}")
        return
    if args.cmd in ("estimate", "run"):
        from gipper.job import load
        from gipper.runpod import run as runmod
        job = load(args.job)
        if args.cmd == "estimate":
            c = runmod.pick_gpu(Api(), job)
            for g, p, stock in c[:6]:
                e = runmod.estimate(job, p, args.throughput)
                print(f"{g['id'][:40]:40s} ${p:.2f}/h  ~{e['wall_s'] / 60:.0f} min  ~${e['usd']:.3f}  ({e['basis']}; stock {stock or '-'})")
            return
        _, outcome = runmod.run(job, dry_run=args.dry_run, gpu_override=args.gpu, throughput=args.throughput)
        sys.exit(0 if outcome in ("succeeded", None) or args.dry_run else 1)
    if args.cmd == "ls":
        for p in Api().list_pods():
            if (p.get("name") or "").startswith("gipper-"):
                print(f"{p['id']}  {p['name']}  {p.get('desiredStatus')}  ${p.get('costPerHr')}/h  since {p.get('lastStartedAt')}")
        return
    if args.cmd == "reap":
        from gipper.runpod.reap import reap
        acted = reap(dry_run=args.dry_run)
        for pid, name, why in acted:
            print(f"{'would end' if args.dry_run else 'ended'} {pid} {name}: {why}")
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
    pod = sub.add_parser("pod", help="rented GPUs (Runpod)")
    ps = pod.add_subparsers(dest="cmd", required=True)
    g = ps.add_parser("gpus")
    g.add_argument("--min-mem", type=float, default=16)
    g.add_argument("--cloud", default="COMMUNITY", choices=["COMMUNITY", "SECURE"])
    for name in ("estimate", "run"):
        p = ps.add_parser(name)
        p.add_argument("job")
        p.add_argument("--throughput", type=float, help="units per second, if known")
        if name == "run":
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--gpu", help="force a GPU type id")
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
