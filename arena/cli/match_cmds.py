"""`arena match ...`: run matches and look at stored runs."""

import asyncio
import json
import statistics
import sys
import time
from collections import Counter

from arena.cli import render
from arena.cli.store import save_spot
from arena.match import (
    MatchConfig, MatchRunner, bot_ids_for_paths, make_bot_seats, new_match_id,
)
from arena.replay import hand_to_spot, verify_run
from arena.runs import Run, RunWriter, check_id, list_runs, runs_dir
from arena.spot import parse_blinds

DEFAULT_IMAGE = "poker-harness-sandbox:latest"


class MatchCliError(Exception):
    pass


def _emit(args, data, text):
    print(json.dumps(data, indent=2, default=repr) if getattr(args, "json", False) else text)


def _chips(n):
    return f"{n:,}"


def _signed(n):
    return f"{n:+,}"


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def cmd_run(args):
    if not 2 <= len(args.bots) <= 9:
        raise MatchCliError(f"need 2-9 bots, got {len(args.bots)}")
    match_id = check_id("match id", args.id or new_match_id())
    ids      = bot_ids_for_paths(args.bots)
    sb, bb   = parse_blinds(args.blinds)
    seats    = make_bot_seats(dict(zip(ids, args.bots)), timeout=args.timeout,
                              docker_image=(args.image if args.docker else None))
    config   = MatchConfig(n_hands=args.hands, small_blind=sb, big_blind=bb,
                           starting_stack=args.stack, seed=args.seed)
    writer   = None if args.no_store else RunWriter(match_id)
    if not args.json:
        where = f" -> {writer.dir}/" if writer else ""
        print(f"match {match_id}: {', '.join(ids)}, {args.hands} hands{where}", file=sys.stderr)

    runner = MatchRunner(match_id, seats, config, writer=writer, verbose=args.verbose)
    result = asyncio.run(runner.run())
    result.pop("hands", None)
    _emit(args, result, _summary_text(result))


def _summary_text(r: dict) -> str:
    head = (f"match {r['match_id']} · {r['n_hands']} hands · seed {r['seed']} · "
            f"{r['duration_s']}s · {r['end_reason']}")
    width = max(len(b) for b in r["bot_ids"]) + 2
    rows = [head, f"  {'bot':<{width}}{'stack':>10}{'delta':>10}{'errors':>8}{'restarts':>10}"]
    for b in sorted(r["bot_ids"], key=lambda b: -r["final_stacks"][b]):
        rows.append(f"  {b:<{width}}{_chips(r['final_stacks'][b]):>10}"
                    f"{_signed(r['chip_delta'][b]):>10}{r['error_counts'][b]:>8}"
                    f"{r['restarts'][b]:>10}")
    if r.get("error"):
        rows.append(f"error: {r['error']}")
    if r.get("run_dir"):
        rows.append(f"run  {r['run_dir']}/   (arena match show {r['match_id']})")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# list / show
# ---------------------------------------------------------------------------

def cmd_list(args):
    rows = []
    for run in list_runs():
        m = run.meta
        res = m.get("result") or {}
        rows.append({
            "id": run.match_id, "status": m.get("status"),
            "created": m.get("created"), "bots": run.bot_ids(),
            "hands": res.get("n_hands"), "chip_delta": res.get("chip_delta"),
        })
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print(f"no runs in {runs_dir()}/ (start one with: arena match run BOT BOT ...)")
        return
    for r in rows[-args.limit:]:
        when = time.strftime("%m-%d %H:%M", time.localtime(r["created"] or 0))
        best = ""
        if r["chip_delta"]:
            top = max(r["chip_delta"], key=r["chip_delta"].get)
            best = f"  top {top} {_signed(r['chip_delta'][top])}"
        print(f"{r['id']:<26} {when}  {r['status']:<9} {r['hands'] or '-':>4} hands  "
              f"{', '.join(r['bots'])}{best}")


def bot_stats(run: Run, bot_id: str) -> dict:
    n, errors, corrected, logs, ms = 0, Counter(), 0, 0, []
    for d in run.decisions(bot_id):
        n += 1
        if d["error"]:
            errors[d["error"]] += 1
        corrected += bool(d["corrected"])
        logs += len(d["logs"])
        if d["bot_ms"] is not None:
            ms.append(d["bot_ms"])
    return {
        "decisions": n, "errors": dict(errors), "corrected": corrected, "logs": logs,
        "bot_ms_median": round(statistics.median(ms), 1) if ms else None,
        "bot_ms_max": max(ms) if ms else None,
    }


def cmd_show(args):
    run = Run.open(args.id)
    m   = run.meta
    res = m.get("result") or {}
    stats = {b: bot_stats(run, b) for b in run.bot_ids()}
    pots  = sorted((e for e in run.events() if e["type"] == "hand_end"),
                   key=lambda e: -e["pot"])[:args.top]
    data = {"meta": m, "bots": stats,
            "biggest_pots": [{"hand_num": e["hand_num"], "pot": e["pot"],
                              "winners": e["winners"], "showdown": e["showdown"]} for e in pots]}
    if args.json:
        print(json.dumps(data, indent=2, default=repr))
        return

    cfg = m["config"]
    out = [f"match {run.match_id} · {m['status']} · {res.get('n_hands', '?')} hands · "
           f"seed {cfg['seed']} · blinds {cfg['small_blind']}/{cfg['big_blind']}"
           + (f" · {res['end_reason']}" if res.get("end_reason") else "")]
    width = max(len(b) for b in run.bot_ids()) + 2
    out.append(f"  {'bot':<{width}}{'delta':>10}{'decisions':>11}{'errors':>8}"
               f"{'corrected':>11}{'logs':>7}{'ms med/max':>13}  version")
    seats = {s["bot_id"]: s for s in m["seats"]}
    for b in run.bot_ids():
        st = stats[b]
        err = sum(st["errors"].values())
        ms = (f"{st['bot_ms_median']}/{st['bot_ms_max']}" if st["bot_ms_median"] is not None else "-")
        delta = res.get("chip_delta", {}).get(b)
        out.append(f"  {b:<{width}}{_signed(delta) if delta is not None else '-':>10}"
                   f"{st['decisions']:>11}{err:>8}{st['corrected']:>11}{st['logs']:>7}"
                   f"{ms:>13}  {seats[b].get('version') or '-'}")
    for b in run.bot_ids():
        if stats[b]["errors"]:
            kinds = ", ".join(f"{k} x{v}" for k, v in stats[b]["errors"].items())
            restarts = res.get("restarts", {}).get(b, 0)
            out.append(f"  ! {b}: {kinds}" + (f"; {restarts} restart(s)" if restarts else ""))
    if pots:
        out.append("biggest pots:")
        for e in pots:
            won = Counter()
            for w in e["winners"]:
                won[w["bot_id"]] += w["amount"]
            ws = ", ".join(f"{b} {_chips(a)}" for b, a in won.most_common())
            kind = "showdown" if e["showdown"] else "no showdown"
            out.append(f"  hand {e['hand_num']:<5}{_chips(e['pot']):>9}  {kind:<12} {ws}")
    out.append(f"files: {run.dir}/  (arena match hand {run.match_id} N)")
    print("\n".join(out))


# ---------------------------------------------------------------------------
# hand / verify
# ---------------------------------------------------------------------------

def _action_text(ev) -> str:
    a = ev["action"]
    return {"fold": "f", "check": "x", "call": f"c{ev['amount']}",
            "all_in": f"a{ev['amount']}"}.get(a, f"r{ev['amount']}")


def cmd_hand(args):
    if args.hand is None:
        mid, _, hn = args.id.rpartition(":")
        if not mid or not hn.isdigit():
            raise MatchCliError("give MATCH HAND or MATCH:HAND, e.g. six1:26")
        args.id, args.hand = mid, int(hn)
    run = Run.open(args.id)
    events = run.hand_events(args.hand)
    if not any(e["type"] == "hand_start" for e in events):
        raise MatchCliError(f"match {args.id} has no hand {args.hand} "
                            f"(hands {run.hand_nums()[0]}..{run.hand_nums()[-1]})")
    actions = [e for e in events if e["type"] == "action"]
    upto = args.at
    if upto is not None and not 0 <= upto <= len(actions):
        raise MatchCliError(f"--at must be 0..{len(actions)} (the hand has {len(actions)} actions)")

    spot = hand_to_spot(events, upto=upto)
    eng, state = spot.to_engine(hand_id=f"{args.id}_h{args.hand}")
    names = next(e for e in events if e["type"] == "hand_start")["bot_ids"]

    if args.save:
        path = save_spot(spot.compact(), args.save, args.force)
        print(f"saved {path}", file=sys.stderr)

    decisions = _hand_decisions(run, names, args.hand)
    shown = actions if upto is None else actions[:upto]
    view = render.god_view(spot.with_actions_from(eng), eng, state,
                           with_equity=not args.no_equity, names=names)
    view["rigged"] = False      # cards are fixed only because this is a replay
    view["seed"]   = run.meta["config"]["seed"]
    view["decisions"] = [{**decisions.get(ev.get("decision_id"), {}),
                          "event": {k: ev[k] for k in ("street", "seat", "bot_id", "action", "amount")}}
                         for ev in shown]
    if args.json:
        print(json.dumps(view, indent=2, default=repr))
        return

    title = f"match {args.id} hand {args.hand}" + (f" @{upto}" if upto is not None else "")
    out = [render.god_view_text(view, title=title)]
    if shown:
        out.append("decisions:")
        for i, ev in enumerate(shown):
            d = decisions.get(ev.get("decision_id"), {})
            note = []
            if d.get("error"):
                note.append(f"ERROR {d['error']}")
            if d.get("corrected"):
                note.append(f"corrected from {json.dumps(d['response'])}")
            for entry in d.get("logs", [])[:args.logs]:
                msg = entry.get("msg") or ""
                data = json.dumps(entry.get("data")) if entry.get("data") else ""
                note.append(f"log: {msg} {data}".strip()[:160])
            ms = f"{d['bot_ms']}ms" if d.get("bot_ms") is not None else ""
            first = f"  {i:>2} {ev['street'][:4]:<5}s{ev['seat']} {ev['bot_id']:<14}{_action_text(ev):<9}{ms:>8}"
            out.append(first + (f"  {note[0]}" if note else ""))
            out += [" " * 47 + n for n in note[1:]]
    print("\n".join(out))


def _hand_decisions(run: Run, names: list, hand_num: int) -> dict:
    out = {}
    for b in dict.fromkeys(names):
        for d in run.decisions(b):
            if d["hand_num"] == hand_num:
                out[d["decision_id"]] = {k: d[k] for k in
                                         ("decision_id", "response", "applied", "corrected",
                                          "error", "detail", "logs", "bot_ms", "elapsed_ms")}
    return out


def cmd_verify(args):
    run = Run.open(args.id)
    bad = verify_run(run)
    n = len(run.hand_nums())
    _emit(args, {"hands": n, "mismatches": bad},
          f"{n} hands replayed: " + ("all match" if not bad else
                                      f"{len(bad)} mismatch(es)\n" +
                                      "\n".join(f"  hand {h}: {'; '.join(p)}" for h, p in bad.items())))
    if bad:
        raise MatchCliError(f"{len(bad)} hand(s) don't replay to the recorded result")


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def add_parser(sub):
    match = sub.add_parser("match", help="run matches and inspect stored runs")
    msub  = match.add_subparsers(dest="match_cmd", required=True, metavar="subcommand")

    p = msub.add_parser("run", help="play a match between 2-9 bots and store it")
    p.add_argument("bots", nargs="+", help="bot.py files, bot directories or .zip files")
    p.add_argument("--hands", type=int, default=400)
    p.add_argument("--seed", type=int, help="deals are reproducible from the seed (random if omitted)")
    p.add_argument("--id", help="match id (default: timestamp)")
    p.add_argument("--blinds", default="50/100")
    p.add_argument("--stack", type=int, default=10_000, help="starting stack")
    p.add_argument("--timeout", type=float, default=2.0, help="seconds per decision")
    p.add_argument("--docker", action="store_true", help="run each bot in the sandbox container")
    p.add_argument("--image", default=DEFAULT_IMAGE)
    p.add_argument("--no-store", action="store_true", help="don't write runs/<id>/")
    p.add_argument("--verbose", action="store_true", help="print every action to stderr")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = msub.add_parser("list", help="list stored runs")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_list)

    p = msub.add_parser("show", help="summary of a run: results, per-bot stats, biggest pots")
    p.add_argument("id")
    p.add_argument("--top", type=int, default=5, help="how many biggest pots to list")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_show)

    p = msub.add_parser("hand", help="god view of one hand, with each decision's logs",
                        epilog="--at K shows the position before action K (0 = before anyone acts)")
    p.add_argument("id", help="match id, or MATCH:HAND as printed by arena hands")
    p.add_argument("hand", type=int, nargs="?")
    p.add_argument("--at", type=int)
    p.add_argument("--logs", type=int, default=3, help="ctx.log lines shown per decision")
    p.add_argument("--save", metavar="NAME", help="save this position as a library spot")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-equity", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_hand)

    p = msub.add_parser("verify", help="replay every hand and check it matches the record")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_verify)
