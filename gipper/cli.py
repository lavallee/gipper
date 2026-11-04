"""gipper: GPU jobs across cloud markets.

    gipper pod gpus [--min-mem GB] [--region US,CA] [--udp] [--provider P]   offers across markets
    gipper pod estimate JOB.toml [--throughput U/S] [--provider P]           what a run would take and cost
    gipper pod run JOB.toml [--dry-run] [--gpu NAME] [--provider P]          rent, run, collect, terminate
    gipper pod hosts [--provider P]               what gipper has learned about providers and machines
    gipper pod ls                                 gipper's machines now running, every provider
    gipper pod reap [--dry-run]                   end machines gipper forgot
    gipper pod reconcile [--days N]               record what providers actually billed
    gipper pod cost [--since YYYY-MM-DD]          spend per job: billed where known, else computed
    gipper pod account                            balance per provider
    gipper ledger [--since YYYY-MM-DD] [--job ID] raw receipts

Providers: runpod, vast, shadeform (keys RUNPOD_API_KEY, VAST_API_KEY,
SHADEFORM_API_KEY in the environment or ~/.config/gipper/.env). Vast and Shadeform list offers without a key.
"""
import argparse
import json
import signal
import sys
from collections import defaultdict

PROVIDERS = ["runpod", "vast", "shadeform"]


def _providers(name=None):
    from gipper.config import settings
    from gipper.providers import load
    return [load(n) for n in ([name] if name else settings()["providers"])]


def _offer_line(o):
    e = o.estimate
    return (f"{o.provider:9s} {o.gpu[:28]:28s} {o.mem_gb:>4.0f} {o.usd_h:>6.2f} {e.get('wall_s', 0) / 60:>5.0f} "
            f"{e.get('usd', 0):>7.3f} {e.get('p_fail', 0):>5.0%} {e.get('score', 0):>7.3f}  {o.where}")


HEADER = f"{'provider':9s} {'gpu':28s} {'GB':>4s} {'$/h':>6s} {'min':>5s} {'$run':>7s} {'risk':>5s} {'score':>7s}  where"


def _pod(args):
    if args.cmd == "gpus":
        from gipper import market
        from gipper.job import Job
        job = Job(name="gpus", workload="-", command="-", min_mem_gb=args.min_mem, cloud=args.cloud,
                  regions=[r.strip().upper() for r in (args.region or "").split(",") if r.strip()],
                  ports=["9/udp"] if args.udp else [])
        offers, notes = market.shop(job, names=[args.provider] if args.provider else None, keyed_only=False)
        keyed = {p.name for p in _providers() if p.has_key()}
        print(HEADER + "   (a 30-minute job)")
        for o in offers[:args.limit]:
            print(_offer_line(o) + ("" if o.provider in keyed else "  (no key)"))
        for n in notes:
            print(f"note: {n}")
        return
    if args.cmd in ("estimate", "run"):
        from gipper import run as runmod
        from gipper.job import load
        job = load(args.job)
        if args.cmd == "run":
            _, outcome = runmod.run(job, dry_run=args.dry_run, gpu_override=args.gpu, provider=args.provider,
                                    throughput=args.throughput)
            sys.exit(0 if outcome in ("succeeded", None) or args.dry_run else 1)
        offers, _, notes = runmod.plan(job, provider=args.provider, throughput=args.throughput)
        print(HEADER)
        for o in offers[:8]:
            print(_offer_line(o))
        best = offers[0].estimate
        print(f"\nbest: {offers[0].label}, objective {best['objective']}")
        print("  seconds: " + ", ".join(f"{k} {v}" for k, v in best["phases_s"].items()))
        print(f"  dollars: rental {best['rental_usd']:.4f} + bandwidth {best['bandwidth_usd']:.4f}"
              + ("" if best["bandwidth_known"] else " (fees not advertised)")
              + (f", x{best['billing_factor']:.2f} billed vs computed" if best["billing_factor"] else ""))
        print(f"  host risk {best['p_fail']:.0%} -> ${best['risk_usd']:.3f}; waiting valued at ${best['hour_value_usd']}/h")
        print("  from: " + ", ".join(f"{k} {v}" for k, v in best["basis"].items()))
        for n in notes:
            print(f"note: {n}")
        return
    if args.cmd == "hosts":
        from gipper.history import History
        h = History.load()
        for name in [args.provider] if args.provider else PROVIDERS:
            s = h.summary(name)
            if not s["rentals"]:
                continue
            fmt = lambda v, f: f.format(v) if v is not None else "-"
            print(f"{name}: {s['rentals']} rentals, {s['good']} good, {s['bad']} bad -> {s['failure']:.0%} risk for a new one; "
                  f"ready {fmt(s['ready_s'], '{:.0f}')} s; measured up {fmt(s['up_mbps'], '{:.0f}')} / "
                  f"down {fmt(s['down_mbps'], '{:.0f}')} Mb/s; billed {fmt(s['billing_factor'], 'x{:.2f}')} computed"
                  + (f"; {s['cleanup_pending']} cleanup(s) unconfirmed" if s["cleanup_pending"] else ""))
            for why in s["reasons"]:
                print(f"  bad: {why[:110]}")
            machines = defaultdict(list)
            for r in h.rentals_of(name):
                if r.machine:
                    machines[r.machine].append(r)
            for m, rs in sorted(machines.items(), key=lambda kv: -len(kv[1])):
                bad = sum(r.outcome == "bad" for r in rs)
                if len(rs) > 1 or bad:
                    print(f"  machine {m:>10s}  {len(rs)} rentals, {bad} bad -> {h.failure(name, m):.0%}  "
                          f"{rs[-1].gpu or ''}")
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
            print(f"{'would end' if args.dry_run else 'ended or retried'} {prov} {pid} {name}: {why}")
        if not acted:
            print("nothing to reap")
        return
    if args.cmd == "reconcile":
        from gipper.billing import reconcile
        written = reconcile(days=args.days)
        for prov, iid, usd in written:
            print(f"{prov} {iid}: billed ${usd:.4f}")
        print(f"{len(written)} bill(s) recorded" if written else "bills already up to date")
        return
    if args.cmd == "cost":
        from gipper import ledger
        # a job belongs to the day it started; its bill may be recorded days later
        per = defaultdict(lambda: {"computed": 0.0, "billed": {}, "s": 0, "name": "", "outcome": "", "first": None})
        for r in ledger.read(kinds={"pod.create", "spend", "spend.billed", "run.end"}):
            j = per[r["job"]]
            j["first"] = j["first"] or r["ts"]
            if r["kind"] == "pod.create":
                continue
            j["name"] = r["refs"].get("name", j["name"])
            if r["kind"] == "spend":
                j["computed"] += r["payload"]["usd"]
                j["s"] += r["payload"]["seconds"]
            elif r["kind"] == "spend.billed":
                j["billed"][(r["payload"]["provider"], r["payload"]["pod"])] = r["payload"]["usd"]
            else:
                j["outcome"] = r["payload"]["outcome"]
        computed = billed_total = 0.0
        print(f"{'job':16s}  {'name':30s} {'min':>6s} {'computed':>9s} {'billed':>8s}  outcome")
        for jid, j in per.items():
            if (not j["s"] and not j["billed"]) or (args.since and (j["first"] or "") < args.since):
                continue
            billed = sum(j["billed"].values()) if j["billed"] else None
            shown = f"{billed:8.3f}" if billed is not None else f"{'-':>8s}"
            print(f"{jid:16s}  {j['name'][:30]:30s} {j['s'] / 60:6.1f} {j['computed']:9.3f} {shown}  {j['outcome']}")
            computed += j["computed"]
            billed_total += billed if billed is not None else j["computed"]
        print(f"total computed ${computed:.3f}; billed where known, else computed: ${billed_total:.3f}")


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)  # live output when redirected to a file
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # `gipper pod hosts | head` ends quietly
    ap = argparse.ArgumentParser(prog="gipper", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="group")
    pod = sub.add_parser("pod", help="rented GPUs (Runpod, Vast, Shadeform)")
    ps = pod.add_subparsers(dest="cmd", required=True)
    g = ps.add_parser("gpus")
    g.add_argument("--min-mem", type=float, default=16)
    g.add_argument("--region", help="ISO country codes, comma-separated (US,CA)")
    g.add_argument("--udp", action="store_true", help="only providers that can expose UDP ports")
    g.add_argument("--provider", choices=PROVIDERS)
    g.add_argument("--cloud", default="COMMUNITY", choices=["COMMUNITY", "SECURE"], help="Runpod cloud to start from")
    g.add_argument("--limit", type=int, default=30)
    for name in ("estimate", "run"):
        p = ps.add_parser(name)
        p.add_argument("job")
        p.add_argument("--throughput", type=float, help="units per second, if known")
        p.add_argument("--provider", choices=PROVIDERS, help="shop only this provider")
        if name == "run":
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--gpu", help="only this GPU (e.g. \"RTX 4090\")")
    h = ps.add_parser("hosts")
    h.add_argument("--provider", choices=PROVIDERS)
    ps.add_parser("ls")
    ps.add_parser("account")
    r = ps.add_parser("reap")
    r.add_argument("--dry-run", action="store_true")
    rc = ps.add_parser("reconcile")
    rc.add_argument("--days", type=int, default=7)
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
