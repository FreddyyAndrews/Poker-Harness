"""`arena compare | compares`: is bot A better than bot B?"""

import asyncio
import json
import sys

from poker_harness import compare as cmp
from poker_harness.spot import parse_blinds

DEFAULT_IMAGE = "poker-harness-sandbox:latest"


def _ci(x):
    lo, hi = x["ci95"]
    return f"(95% CI {lo:+.1f} .. {hi:+.1f})"


def compare_text(info: dict) -> str:
    s = info["stats"]
    a, b = info["a"]["id"], info["b"]["id"]
    w = max(len(a), len(b), len("difference")) + 2
    rot = s["rotations"]
    how = (f"duplicate ({next(iter(rot.values()))} rotations)" if info["duplicate"] else "plain")
    lines = [f"compare {info['id']} · {info['mode']} · {a} vs {b}"
             + (f" · field {', '.join(f['id'] for f in info['field'])}" if info["field"] else "")
             + f" · {how} · {s['deals']} deals · seed {info['seed']}"]
    lines.append(f"  {a:<{w}}{s['a']['bb_per_100']:+8.1f} bb/100  {_ci(s['a'])}")
    if info["mode"] == "field":
        lines.append(f"  {b:<{w}}{s['b']['bb_per_100']:+8.1f} bb/100  {_ci(s['b'])}")
        lines.append(f"  {'difference':<{w}}{s['diff']['bb_per_100']:+8.1f} bb/100  {_ci(s['diff'])}")
    else:
        lines.append(f"  {b:<{w}}{s['b']['bb_per_100']:+8.1f} bb/100")
    verdict = {"a_better": f"{a} is better (the interval excludes 0)",
               "b_better": f"{b} is better (the interval excludes 0)",
               "no_difference": "no significant difference"}[s["verdict"]]
    d, se = s["diff"]["bb_per_100"], s["diff"]["se"]
    if s["verdict"] == "no_difference" and d and se == se:
        hands_needed = s["deals"] * (1.96 * se / abs(d)) ** 2
        if hands_needed < 10**7:
            verdict += f"; a {abs(d):.1f} bb/100 gap would need ~{hands_needed:,.0f} hands per match to show"
    lines.append(f"  -> {verdict}")
    if info["duplicate"]:
        half = 1.96 * s["diff"]["se"]
        nhalf = 1.96 * s["naive"]["se"]
        tighter = ("exact" if s["diff"]["se"] < 1e-9 else f"{s['variance_reduction']}x tighter")
        lines.append(f"  duplicate: interval ±{half:.1f} vs ±{nhalf:.1f} for the same hands "
                     f"played plainly ({tighter})")
    errs = ", ".join(f"{k} x{v}" for k, v in info["errors"].items() if v) or "none"
    lines.append(f"  {len(info['matches'])} matches ({info['matches'][0]} ...) · bot errors: {errs}")
    return "\n".join(lines)


def cmd_compare(args):
    sb, bb = parse_blinds(args.blinds)
    if not args.json:
        p = cmp.plan(args.a, args.b, args.field, not args.no_duplicate)
        print(f"playing {len(p['tables'])} match(es) of {args.hands} hands...", file=sys.stderr)
    info = asyncio.run(cmp.run_compare(
        args.a, args.b, args.field, hands=args.hands, seed=args.seed,
        duplicate=not args.no_duplicate, jobs=args.jobs, timeout=args.timeout,
        small_blind=sb, big_blind=bb, stack=args.stack,
        docker_image=args.image if args.docker else None, compare_id=args.id,
        progress=None if args.json else (lambda m: print(m, file=sys.stderr))))
    print(json.dumps(info, indent=2) if args.json else compare_text(info))


def cmd_compares(args):
    if args.id:
        info = cmp.load_compare(args.id)
        print(json.dumps(info, indent=2) if args.json else compare_text(info))
        return
    rows = cmp.list_compares()
    if args.json:
        print(json.dumps([{k: r[k] for k in ("id", "mode", "a", "b", "stats")} for r in rows], indent=2))
        return
    if not rows:
        print("no comparisons yet (arena compare A B)")
    rows = rows[-args.limit:]
    names = [f"{r['a']['id']} vs {r['b']['id']}" for r in rows]
    w = max(len(n) for n in names) + 2
    for r, name in zip(rows, names):
        s = r["stats"]
        print(f"{r['id']:<26} {name:<{w}}diff {s['diff']['bb_per_100']:+7.1f} bb/100 "
              f"{_ci(s['diff'])}  {s['verdict']}")


def add_parsers(sub):
    import argparse
    p = sub.add_parser(
        "compare", help="is A better than B? duplicate deals, with a confidence interval",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  arena compare bots/mybot-v2 bots/mybot                 # head-to-head\n"
               "  arena compare bots/mybot-v2 bots/mybot --field bots/shark/bot.py "
               "--field bots/aggressor/bot.py\n\n"
               "Every hand starts from the starting stacks. With duplicate (the default)\n"
               "each deal is replayed with the seats rotated so every player gets every\n"
               "seat's cards; most of the card luck cancels. See poker_harness/compare.py.")
    p.add_argument("a", help="bot A (e.g. the new version)")
    p.add_argument("b", help="bot B (e.g. the current version)")
    p.add_argument("--field", action="append", default=[], metavar="BOT",
                   help="opponents A and B each play (repeatable); omit for head-to-head")
    p.add_argument("--hands", type=int, default=400, help="hands per match (= deals)")
    p.add_argument("--seed", type=int)
    p.add_argument("--no-duplicate", action="store_true", help="one match per side, no seat rotation")
    p.add_argument("-j", "--jobs", type=int, default=4, help="matches at once")
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--blinds", default="50/100")
    p.add_argument("--stack", type=int, default=10_000)
    p.add_argument("--docker", action="store_true")
    p.add_argument("--image", default=DEFAULT_IMAGE)
    p.add_argument("--id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_compare)

    p = sub.add_parser("compares", help="list comparisons, or show one")
    p.add_argument("id", nargs="?")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_compares)
