"""
arena: command-line tool for building poker situations and stepping
through hands with full (god) view. Built for coding agents: every command
runs to completion without prompts, output is short, and --json gives the
full structure.

  arena spot [REF] [spot options]      build/check a spot and show it
  arena spots                          list the spot library
  arena hand new [REF] [spot options]  start a hand you can step through
  arena hand act ID ACTION...          apply actions (strict by default)
  arena hand state ID                  show the current position
  arena hand undo ID [-n N]            take back actions
  arena hand save ID NAME              save the current position as a spot
  arena hand list | rm ID
  arena equity HAND HAND... [--board]  showdown equity
  arena match run BOT BOT...           play a match; stored in runs/<id>/
  arena match list | show ID | hand ID N [--at K] | verify ID
  arena stats BOT                      win rate (with confidence interval), style, leaks
  arena hands [filters]                find hands, e.g. --bot X --lost-more 2000
  arena decisions [filters]            find decisions with the bot's notes
  arena sql "SELECT ..." | --schema    read-only SQL over the index
  arena probe BOT SPOT [-n 20]         what does the bot do here? (or --from MATCH:HAND)
  arena sweep BOT SPOT --vary ...      ... and where does its answer flip?
  arena probes [ID]                    stored probes, sweeps and test runs
  arena test BOT [--suite NAME]        spots with expected answers; exit 1 on failure
  arena compare A B [--field BOT...]   is A better than B? duplicate deals + confidence interval
  arena compares [ID]                  stored comparisons
  arena brief BOT|MATCH                short summary for an agent's context: result, leaks, hands

Run `arena <command> -h` for options. Spot notation is described in
poker_harness/spot.py and `arena spot -h`.
"""

import argparse
import json
import random
import sys

from poker_harness.cli import brief_cmds, compare_cmds, index_cmds, match_cmds, probe_cmds, render, test_cmds
from poker_harness.cli.spotargs import SPOT_HELP, add_spot_options as _add_spot_options
from poker_harness.cli.spotargs import spot_from_args as _spot_from_args
from poker_harness.cli.store import HandStore, list_spots, save_spot, spot_ref, spots_dir
from poker_harness.engine.game import IllegalActionError
from poker_harness.equity import DEFAULT_ITERS, equity
from poker_harness.spot import Spot, SpotError, parse_action_token



class CliError(Exception):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _emit(args, view_or_data, text: str):
    if getattr(args, "json", False):
        print(json.dumps(view_or_data, indent=2))
    else:
        print(text)


def _show(args, spot: Spot, eng, state, title=None):
    if getattr(args, "as_bot", False):
        if state["type"] != "action_request":
            raise CliError("--as-bot: the hand is over; nobody is to act")
        print(json.dumps(state, indent=2))
        return
    view = render.god_view(spot, eng, state, with_equity=not args.no_equity)
    _emit(args, view, render.god_view_text(view, title=title))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_spot(args):
    spot = _spot_from_args(args)
    eng, state = spot.to_engine()
    if args.save:
        path = save_spot(spot.compact() if spot.actions else spot, args.save, args.force)
        if not args.json and not args.as_bot:
            print(f"saved {path}")
    _show(args, spot.with_actions_from(eng), eng, state,
          title=f"spot {spot.name}" if spot.name else "spot")


def cmd_spots(args):
    rows = []
    for path, spot, err in list_spots():
        if err:
            rows.append({"name": spot_ref(path), "path": str(path), "error": err})
            continue
        try:
            _, state = spot.to_engine()
            where = (f"{state['street']}, s{state['seat_to_act']} to act"
                     if state["type"] == "action_request" else "hand over")
        except SpotError as e:
            where, err = "invalid", str(e)
        rows.append({"name": spot_ref(path), "path": str(path), "players": spot.players,
                     "where": where, "description": spot.description, "tags": spot.tags,
                     "expect": spot.expect, "error": err})
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print(f"no spots in {spots_dir()}/ (save one with: arena spot ... --save NAME)")
        return
    width = max(len(r["name"]) for r in rows) + 2
    for r in rows:
        if r.get("error"):
            print(f"{r['name']:<{width}} ERROR {r['error']}")
        else:
            desc = f"  {r['description']}" if r["description"] else ""
            marks = (" [expect]" if r["expect"] else "") + \
                    (f" [{', '.join(r['tags'])}]" if r["tags"] else "")
            print(f"{r['name']:<{width}} {r['players']}p  {r['where']}{marks}{desc}")


def cmd_hand_new(args):
    store = HandStore()
    spot  = _spot_from_args(args)
    if spot.seed is None:
        spot.seed = random.randrange(1, 10**6)   # a hand must replay identically
    hand_id = args.id or store.new_id()
    if store.exists(hand_id):
        raise CliError(f"hand {hand_id!r} already exists")
    eng, state = spot.to_engine(hand_id=hand_id)
    spot = spot.with_actions_from(eng)
    store.save(hand_id, spot)
    _show(args, spot, eng, state, title=f"hand {hand_id}")


def _load_hand(hand_id: str):
    rec  = HandStore().load(hand_id)
    spot = Spot.from_dict(rec["spot"])
    eng, state = spot.to_engine(hand_id=hand_id)
    return rec, spot, eng, state


def cmd_hand_act(args):
    store = HandStore()
    rec, spot, eng, state = _load_hand(args.id)
    notes = []
    tokens = [t.strip() for a in args.actions for t in a.split(",") if t.strip()]
    for tok in tokens:
        if state["type"] != "action_request":
            raise CliError(f"{tok!r}: the hand is already over")
        a = parse_action_token(tok, spot, state["street"])
        try:
            state = spot._skip_to(eng, state, a, f"{tok!r}")
            seat  = state["seat_to_act"]
            state = eng.apply_action(seat, a.raw(), strict=not args.lenient)
        except IllegalActionError as e:
            legal = render.legal_text(eng.legal_actions())
            raise CliError(f"{tok!r}: {e}\n  legal: {legal}") from None
        applied = eng.action_log[-1]
        if args.lenient and (applied["action"] != a.action or
                             (a.action == "raise" and applied["amount"] != a.amount)):
            notes.append(f"note: s{seat} {a.code()} was applied as "
                         f"{applied['action']} {applied['amount'] or ''}".rstrip())
    spot = spot.with_actions_from(eng)
    store.save(args.id, spot, created=rec["created"])
    for n in notes:
        print(n, file=sys.stderr)
    _show(args, spot, eng, state, title=f"hand {args.id}")


def cmd_hand_state(args):
    _, spot, eng, state = _load_hand(args.id)
    _show(args, spot, eng, state, title=f"hand {args.id}")


def cmd_hand_undo(args):
    store = HandStore()
    rec, spot, _, _ = _load_hand(args.id)
    if args.n < 1 or args.n > len(spot.actions):
        raise CliError(f"can't undo {args.n}; the hand has {len(spot.actions)} action(s)")
    spot.actions = spot.actions[:-args.n]
    eng, state = spot.to_engine(hand_id=args.id)
    store.save(args.id, spot, created=rec["created"])
    _show(args, spot, eng, state, title=f"hand {args.id}")


def cmd_hand_save(args):
    _, spot, _, _ = _load_hand(args.id)
    spot = spot.compact()
    if args.description:
        spot.description = args.description
    path = save_spot(spot, args.name, args.force)
    _emit(args, {"saved": str(path)}, f"saved {path}")


def cmd_hand_list(args):
    rows = []
    for rec in HandStore().list():
        spot = Spot.from_dict(rec["spot"])
        try:
            _, state = spot.to_engine()
            where = (f"{state['street']}, s{state['seat_to_act']} to act, pot {state['pot']:,}"
                     if state["type"] == "action_request" else "hand over")
        except SpotError as e:
            where = f"invalid: {e}"
        rows.append({"id": rec["id"], "players": spot.players, "where": where})
    if args.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("no hands (start one with: arena hand new ...)")
    else:
        for r in rows:
            print(f"{r['id']:<6} {r['players']}p  {r['where']}")


def cmd_hand_rm(args):
    HandStore().delete(args.id)
    _emit(args, {"deleted": args.id}, f"deleted {args.id}")


def cmd_equity(args):
    r = equity(args.hands, board=args.board or "", dead=args.dead or "",
               iters=args.iters, seed=args.seed)
    lines = [f"equity  {r['method'].replace('_', ' ')} ({r['samples']:,} "
             f"{'boards' if r['method'] == 'exact' else 'samples'})"
             + (f"   board {' '.join(r['board'])}" if r["board"] else "")]
    width = max(len(p["hand"]) for p in r["players"])
    for p in r["players"]:
        lines.append(f"  {p['hand']:<{width}}  {p['equity'] * 100:6.2f}%   "
                     f"win {p['win'] * 100:5.1f}%  tie {p['tie'] * 100:4.1f}%")
    _emit(args, r, "\n".join(lines))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    fmt = argparse.RawDescriptionHelpFormatter
    ap  = argparse.ArgumentParser(prog="arena", description=__doc__, formatter_class=fmt)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")

    def view_flags(p, as_bot=True):
        p.add_argument("--json", action="store_true", help="full structured output")
        p.add_argument("--no-equity", action="store_true", help="skip equity column")
        if as_bot:
            p.add_argument("--as-bot", action="store_true",
                           help="print the exact JSON state the seat to act would receive")

    p = sub.add_parser("spot", help="build and check a spot; show it in god view",
                       epilog=SPOT_HELP, formatter_class=fmt)
    p.add_argument("ref", nargs="?", help="spot file or library name to start from")
    _add_spot_options(p)
    p.add_argument("--save", metavar="NAME", help=f"save to the library ({spots_dir()}/NAME.yaml)")
    p.add_argument("--force", action="store_true", help="overwrite an existing saved spot")
    view_flags(p)
    p.set_defaults(fn=cmd_spot)

    p = sub.add_parser("spots", help="list the spot library")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_spots)

    hand = sub.add_parser("hand", help="step through a hand, controlling every seat")
    hsub = hand.add_subparsers(dest="hand_cmd", required=True, metavar="subcommand")

    p = hsub.add_parser("new", help="start a hand from a spot (or a fresh deal)",
                        epilog=SPOT_HELP, formatter_class=fmt)
    p.add_argument("ref", nargs="?", help="spot file or library name to start from")
    _add_spot_options(p)
    p.add_argument("--id", help="hand id (default h1, h2, ...)")
    view_flags(p)
    p.set_defaults(fn=cmd_hand_new)

    p = hsub.add_parser("act", help="apply one or more actions",
                        epilog="actions: f x c rN a, optionally prefixed by a seat "
                               "(\"BB r900\"); several per call: act h1 c x \"BTN r250\"")
    p.add_argument("id")
    p.add_argument("actions", nargs="+")
    p.add_argument("--lenient", action="store_true",
                   help="correct illegal actions like a live match does, instead of erroring")
    view_flags(p)
    p.set_defaults(fn=cmd_hand_act)

    p = hsub.add_parser("state", help="show the current position")
    p.add_argument("id")
    view_flags(p)
    p.set_defaults(fn=cmd_hand_state)

    p = hsub.add_parser("undo", help="take back the last action(s)")
    p.add_argument("id")
    p.add_argument("-n", type=int, default=1)
    view_flags(p)
    p.set_defaults(fn=cmd_hand_undo)

    p = hsub.add_parser("save", help="save the current position as a library spot")
    p.add_argument("id")
    p.add_argument("name")
    p.add_argument("--desc", dest="description")
    p.add_argument("--force", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_hand_save)

    p = hsub.add_parser("list", help="list hands")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_hand_list)

    p = hsub.add_parser("rm", help="delete a hand")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_hand_rm)

    p = sub.add_parser("equity", help="showdown equity for 2-9 hands or ranges",
                       epilog="hands: AsKh | a range like QQ+,AKs | any (or '??', quoted) for random")
    p.add_argument("hands", nargs="+")
    p.add_argument("--board", help="e.g. Kd7c2s")
    p.add_argument("--dead", help="cards known to be out of the deck")
    p.add_argument("--iters", type=int, default=DEFAULT_ITERS, help="Monte Carlo samples")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_equity)

    match_cmds.add_parser(sub)
    index_cmds.add_parsers(sub)
    probe_cmds.add_parsers(sub)
    test_cmds.add_parser(sub)
    compare_cmds.add_parsers(sub)
    brief_cmds.add_parser(sub)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except (CliError, match_cmds.MatchCliError, index_cmds.IndexCliError, SpotError,
            probe_cmds.pr.ProbeError, compare_cmds.cmp.CompareError, brief_cmds.BriefError, IllegalActionError,
            ValueError, FileNotFoundError, FileExistsError) as e:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
