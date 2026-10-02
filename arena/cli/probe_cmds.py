"""`arena probe | probes | sweep`: ask a bot about a position."""

import asyncio
import json
import sys
from collections import Counter
from types import SimpleNamespace

from arena import probe as pr
from arena.cli.spotargs import SPOT_HELP, add_spot_options, spot_from_args
from arena.replay import hand_to_spot
from arena.runs import Run

DEFAULT_IMAGE = "poker-harness-sandbox:latest"


def _chips(n):
    return f"{n:,}"


def _pct(x):
    return f"{x * 100:.0f}%"


def _parse_from(ref: str):
    mid, _, hn = ref.rpartition(":")
    if not mid or not hn.isdigit():
        raise pr.ProbeError("--from takes MATCH:HAND, e.g. six1:26")
    return Run.open(mid), int(hn)


def _situation(state: dict) -> str:
    board = " ".join(state["community_cards"]) or "-"
    owed  = state["amount_owed"]
    facing = (f"facing {_chips(owed)} ({owed / (state['pot'] + owed) * 100:.0f}% pot odds)"
              if owed else "can check")
    return (f"s{state['seat_to_act']} · {state['street']} · board {board} · "
            f"{''.join(state['your_cards'])} · pot {_chips(state['pot'])} · stack "
            f"{_chips(state['your_stack'])} · {facing}")


def _summary_lines(summary: dict) -> list:
    lines = []
    for kind, n in summary["counts"].items():
        line = f"  {kind:<6}{n:>4}  {_pct(summary['freq'][kind]):>5}"
        if kind in ("bet", "raise"):
            sizes = [s for s in summary["sizes"]]
            if sizes:
                line += "   " + ", ".join(f"to {_chips(s['to'])} ({s['bb']}bb, {s['pot']}x pot)"
                                         + (f" x{s['count']}" if s["count"] > 1 else "")
                                         for s in sizes[:4])
        note = summary["notes"].get(kind)
        if note:
            line += f'   "{note[:80]}"'
        lines.append(line)
    errs = ", ".join(f"{k} x{v}" for k, v in summary["errors"].items()) or "none"
    lines.append(f"  decide() median {summary['bot_ms_median']}ms max {summary['bot_ms_max']}ms"
                 f" · errors {errs}" + (f" · all-in {summary['all_in']}" if summary["all_in"] else ""))
    return lines


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

def cmd_probe(args):
    if args.from_ref:
        run, hand = _parse_from(args.from_ref)
        target = pr.target_from_run(run, hand, at=args.at, warm=args.warm)
        if args.bot:
            bot = args.bot
        else:
            seat = next(s for s in run.meta["seats"] if s["bot_id"] == target.source["bot_id"])
            bot = seat["path"]
            if not bot:
                raise pr.ProbeError("that seat wasn't a bot process; pass BOT")
        if args.ref:
            raise pr.ProbeError("give either a SPOT or --from, not both")
    else:
        if args.warm:
            raise pr.ProbeError("--warm needs --from MATCH:HAND")
        if not args.bot:
            raise pr.ProbeError("give a BOT (bot.py, directory or .zip)")
        spot = spot_from_args(args)
        target = pr.target_from_spot(spot, label=spot.name or "spot")
        bot = args.bot

    d = pr.new_probe_dir("p")
    if not args.json and target.warm_states:
        print(f"warming up with {len(target.warm_states)} earlier decision(s)...", file=sys.stderr)
    res = asyncio.run(pr.run_probe(
        bot, target, args.n, timeout=args.timeout, fresh=args.fresh,
        docker_image=args.image if args.docker else None, stderr_path=d / "stderr.log"))
    summary = pr.summarize(res["samples"], target)
    info = {"kind": "probe", "bot": pr.bot_info(bot), "target": target.label,
            "source": target.source, "state": target.state, "n": args.n, "fresh": args.fresh,
            "warm": res["warm"], "summary": summary,
            "recorded": ({"response": target.recorded["response"],
                          "applied": target.recorded["applied"]} if target.recorded else None)}
    pr.save_probe(d, info, res["samples"])

    if args.json:
        print(json.dumps({"id": d.name, **info}, indent=2, default=repr))
        return
    lines = [f"probe {d.name} · {bot} · {target.label} · {args.n} sample(s)"
             + (f" · warmed with {res['warm']['states']}" if res["warm"]["states"] else ""),
             _situation(target.state)]
    lines += _summary_lines(summary)
    if target.recorded:
        a = target.recorded["applied"]
        lines.append(f"  in the match: {a['action']} {a['amount'] or ''}".rstrip())
    lines.append(f"details: arena probes {d.name} --verbose")
    print("\n".join(lines))


def cmd_probes(args):
    if not args.id:
        rows = pr.list_probes()
        if args.json:
            print(json.dumps([{k: r[k] for k in ("id", "kind", "target", "n")} for r in rows], indent=2))
            return
        if not rows:
            print("no probes yet (arena probe BOT SPOT)")
        for r in rows[-args.limit:]:
            modal = r.get("summary", {}).get("modal") if r["kind"] == "probe" else None
            extra = f"mostly {modal}" if modal else f"{len(r.get('variants', []))} variants"
            print(f"{r['id']:<28} {r['kind']:<6} {r['bot']['path']}  {r['target']}  {extra}")
        return
    d, info, samples = pr.load_probe(args.id)
    if args.json:
        print(json.dumps({**info, "samples": samples}, indent=2, default=repr))
        return
    if info["kind"] == "sweep":
        print(_sweep_text(info))
    else:
        print("\n".join([f"probe {info['id']} · {info['bot']['path']} · {info['target']}",
                         _situation(info["state"])] + _summary_lines(info["summary"])))
    if args.verbose:
        print("samples:")
        for s in samples:
            what = s["kind"] + (f" to {_chips(s['to'])}" if s["to"] else "")
            head = f"  {s.get('variant', '')}{'  ' if s.get('variant') else ''}#{s['i']:<3} {what:<16}"
            if s["error"]:
                head += f" ERROR {s['error']}: {(s['detail'] or '').splitlines()[-1:]}"
            print(head + f" {s['bot_ms']}ms")
            for entry in s["logs"]:
                data = json.dumps(entry["data"]) if entry.get("data") else ""
                print(f"        log: {(entry.get('msg') or '')} {data}".rstrip()[:200])
    print(f"files: {d}/")


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------

def _sweep_text(info: dict) -> str:
    lines = [f"sweep {info['id']} · {info['bot']['path']} · {info['target']} · "
             f"{info['n']} sample(s) per variant · {len(info['variants'])} row(s)"]
    width = max([len(r["label"]) for r in info["variants"]] + [7])
    lines.append(f"  {'variant':<{width}}  {'fold':>5} {'check':>5} {'call':>5} {'raise':>5}  avg raise   n")
    prev = None
    for r in info["variants"]:
        if r.get("invalid") and not r.get("summary"):
            lines.append(f"  {r['label']:<{width}}  invalid: {r['invalid'][0][:70]}")
            continue
        f = r["summary"]["freq"]
        raise_f = f.get("bet", 0) + f.get("raise", 0)
        sizes = [s["to"] for s in r["summary"]["sizes"] for _ in range(s["count"])]
        avg = f"{sum(sizes) / len(sizes):,.0f}" if sizes else "-"
        modal = r["summary"]["modal"]
        modal = "raise" if modal == "bet" else modal
        mark = f"  < {prev} -> {modal}" if prev and modal != prev else ""
        lines.append(f"  {r['label']:<{width}}  {_pct(f.get('fold', 0)):>5} {_pct(f.get('check', 0)):>5} "
                     f"{_pct(f.get('call', 0)):>5} {_pct(raise_f):>5}  {avg:>9} {r['summary']['n']:>3}{mark}")
        prev = modal
    return "\n".join(lines)


def cmd_sweep(args):
    if args.from_ref:
        if args.ref:
            raise pr.ProbeError("give either a SPOT or --from, not both")
        run, hand = _parse_from(args.from_ref)
        spot = hand_to_spot(run.hand_events(hand), upto=args.at)
        label = f"{args.from_ref}" + (f" @{args.at}" if args.at is not None else "")
    else:
        spot = spot_from_args(args)
        label = spot.name or "spot"
    variants = pr.expand_vary(spot, args.vary, max_variants=args.max_variants)
    d = pr.new_probe_dir("s")
    if not args.json:
        print(f"sweeping {len(variants)} variant(s) x {args.n} sample(s), {args.jobs} at a time...",
              file=sys.stderr)
    rows = asyncio.run(pr.run_sweep(args.bot, variants, args.n, jobs=args.jobs, timeout=args.timeout,
                                    docker_image=args.image if args.docker else None, stderr_dir=d))
    grouped = pr.group_rows(rows)
    out_rows, samples = [], []
    for g in grouped:
        row = {"label": g["label"], "variants": g["variants"], "invalid": g["invalid"]}
        if g["samples"]:
            target = SimpleNamespace(state={"big_blind": spot.blinds[1], "pot": g["pot"]})
            row["summary"] = pr.summarize(g["samples"], target)
            for s in g["samples"]:
                samples.append({"variant": g["label"], **s})
        out_rows.append(row)
    info = {"kind": "sweep", "bot": pr.bot_info(args.bot), "target": label,
            "spot": spot.to_dict(), "vary": args.vary, "n": args.n, "variants": out_rows}
    pr.save_probe(d, info, samples)
    if args.json:
        print(json.dumps({"id": d.name, **info}, indent=2, default=repr))
        return
    info["id"] = d.name
    print(_sweep_text(info))
    print(f"details: arena probes {d.name} --verbose")


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def _run_options(p):
    p.add_argument("-n", type=int, default=10, help="samples (per variant for sweeps)")
    p.add_argument("--timeout", type=float, default=2.0, help="seconds per decision")
    p.add_argument("--docker", action="store_true", help="run the bot in the sandbox container")
    p.add_argument("--image", default=DEFAULT_IMAGE)
    p.add_argument("--json", action="store_true")


def add_parsers(sub):
    fmt = __import__("argparse").RawDescriptionHelpFormatter
    p = sub.add_parser(
        "probe", help="ask a bot what it does in a spot or a recorded match position",
        formatter_class=fmt,
        epilog="examples:\n"
               "  arena probe bots/mybot 6max-tptk-vs-flop-lead -n 20\n"
               "  arena probe bots/mybot --cards BTN=AsKh --actions 'pre: CO r250' --to-act BTN\n"
               "  arena probe --from six1:26 --at 3 --warm     # the bot that played it\n"
               "  arena probe bots/mybot-v2 --from six1:26 --at 3\n\n" + SPOT_HELP)
    p.add_argument("bot", nargs="?", help="bot.py, bot directory or .zip (default with --from: "
                                         "the bot that made the decision)")
    p.add_argument("ref", nargs="?", help="spot file or library name")
    add_spot_options(p)
    p.add_argument("--from", dest="from_ref", metavar="MATCH:HAND",
                   help="probe a decision from a stored match instead of a spot")
    p.add_argument("--at", type=int, help="with --from: action index in the hand (default: last)")
    p.add_argument("--warm", action="store_true",
                   help="with --from: first replay the seat's earlier decisions in that match")
    p.add_argument("--fresh", action="store_true", help="new bot process for every sample")
    _run_options(p)
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("probes", help="list probes and sweeps, or show one")
    p.add_argument("id", nargs="?")
    p.add_argument("--verbose", action="store_true", help="every sample with its notes")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_probes)

    p = sub.add_parser(
        "sweep", help="re-probe a spot while varying one or more things; marks where the answer flips",
        formatter_class=fmt,
        epilog="--vary (repeatable; variants are the combinations):\n"
               "  bet=200..2000:200        the last raise in the actions (what the bot faces)\n"
               "  bet=300,600,900\n"
               "  cards.BTN=AsKh,QdQc      a seat's hole cards\n"
               "  cards.BTN=QQ+,AKs        a range; rows group by hand class\n"
               "  turn=*  river=Ah,Kd      a board card (flop1 flop2 flop3 turn river); * = all\n"
               "  stack.BB=1000..5000:1000\n\n" + SPOT_HELP)
    p.add_argument("bot")
    p.add_argument("ref", nargs="?", help="spot file or library name")
    add_spot_options(p)
    p.add_argument("--vary", action="append", required=True, metavar="FIELD=VALUES")
    p.add_argument("--from", dest="from_ref", metavar="MATCH:HAND",
                   help="sweep a position from a stored match (all cards known)")
    p.add_argument("--at", type=int, help="with --from: stop before this action")
    p.add_argument("-j", "--jobs", type=int, default=4, help="bot processes at once")
    p.add_argument("--max-variants", type=int, default=300)
    _run_options(p)
    p.set_defaults(fn=cmd_sweep, n=5)
