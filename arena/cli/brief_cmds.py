"""
`arena brief`: a short summary of a bot or a match, written to go into an
LLM's context.

A brief leads with the result and its confidence interval, then the most
suspicious patterns (each with the command that drills into it), the
costliest hands, and the bot's latest test and comparison results. It
stays within --max-lines; everything it mentions can be expanded with the
commands it prints.

Leak heuristics (shown only with enough data to mean something):
  - folds that were +EV to call against the cards actually held
    (equity > pot odds; uses hindsight), with the approximate chips given up
  - calls that were -EV against the cards actually held
  - errors (timeouts, crashes, exceptions) and replies the engine corrected
  - extreme style: folding to most bets on a street, never raising
    preflop, rarely betting postflop, playing very few or very many hands
  - the position that loses the most
"""

import json
from types import SimpleNamespace

from arena import compare as cmp
from arena import probe as pr
from arena.cli import index_cmds as ic
from arena.runs import Run


class BriefError(ValueError):
    pass


def _pct(x):
    return "-" if x is None else f"{x * 100:.0f}%"


def _ci(s):
    lo, hi = s["ci95"]
    return f"95% CI {lo:+.1f} .. {hi:+.1f}"


def _filters(bot, match=None, version=None, last=None):
    return SimpleNamespace(bot=bot, match=match, version=version, last=last, pos=None, vs=None)


def _hindsight(conn, f) -> dict:
    w = ic.Where()
    ic._common_filters(w, f, "d")
    base = (f"FROM decisions d JOIN matches m ON m.match_id = d.match_id {w.sql()}"
            + (" AND " if w.parts else " WHERE "))
    folds = conn.execute(
        f"SELECT count(*) n, sum((d.equity * (d.pot + d.owed) - d.owed) * 1.0 / m.big_blind) bb "
        f"{base} d.kind = 'fold' AND d.owed > 0 AND d.equity IS NOT NULL AND d.equity > d.pot_odds",
        w.params).fetchone()
    calls = conn.execute(
        f"SELECT count(*) n, sum((d.owed - d.equity * (d.pot + d.owed)) * 1.0 / m.big_blind) bb "
        f"{base} d.kind = 'call' AND d.equity IS NOT NULL AND d.equity < d.pot_odds",
        w.params).fetchone()
    checked = conn.execute(f"SELECT count(*) {base} d.equity IS NOT NULL", w.params).fetchone()[0]
    return {"ev_folds": {"n": folds["n"], "bb": round(folds["bb"] or 0, 1)},
            "bad_calls": {"n": calls["n"], "bb": round(calls["bb"] or 0, 1)},
            "decisions_with_equity": checked}


def leaks(conn, stats: dict, f) -> list:
    """[(cost_bb or None, text, command)] most costly first."""
    bot = f.bot
    scope = (f" --match {f.match}" if f.match else "") + (f" --version {f.version}" if f.version else "")
    out = []
    h = _hindsight(conn, f)
    if h["ev_folds"]["n"] >= 3 and h["ev_folds"]["bb"] >= 5:
        out.append((h["ev_folds"]["bb"],
                    f"{h['ev_folds']['n']} folds were +EV calls against the actual cards "
                    f"(~{h['ev_folds']['bb']:.0f} bb given up, hindsight)",
                    f"arena decisions --bot {bot}{scope} --action fold --equity-above 0.4 --sort pot"))
    if h["bad_calls"]["n"] >= 3 and h["bad_calls"]["bb"] >= 5:
        out.append((h["bad_calls"]["bb"],
                    f"{h['bad_calls']['n']} calls were -EV against the actual cards "
                    f"(~{h['bad_calls']['bb']:.0f} bb lost, hindsight)",
                    f"arena decisions --bot {bot}{scope} --action call --equity-below 0.2 --sort pot"))

    d = stats["decisions"]
    if d["errors"]:
        kinds = ", ".join(f"{k} x{v}" for k, v in d["errors"].items())
        out.append((None, f"decision errors: {kinds} (each one checked/folded)",
                    f"arena decisions --bot {bot}{scope} --error"))
    if d["n"] >= 20 and d["corrected"] / d["n"] > 0.02:
        out.append((None, f"{d['corrected']} replies ({d['corrected'] / d['n'] * 100:.0f}%) were "
                          f"illegal and corrected by the engine",
                    f"arena decisions --bot {bot}{scope} --corrected"))

    for street, v in stats["fold_to_bet"].items():
        if street != "preflop" and v["n"] >= 15 and v["rate"] is not None and v["rate"] >= 0.75:
            out.append((None, f"folds to {_pct(v['rate'])} of {street} bets (n={v['n']}): easy to bluff",
                        f"arena decisions --bot {bot}{scope} --street {street} --facing-bet"))
    hands = stats["hands"]
    if hands >= 50:
        if stats["vpip"] is not None and stats["vpip"] >= 0.15 and not stats["pfr"]:
            out.append((None, f"never raises preflop (VPIP {_pct(stats['vpip'])}, PFR 0%)", None))
        if stats["vpip"] is not None and stats["vpip"] < 0.08:
            out.append((None, f"plays only {_pct(stats['vpip'])} of hands (VPIP)", None))
        if stats["vpip"] is not None and stats["vpip"] > 0.6:
            out.append((None, f"plays {_pct(stats['vpip'])} of hands (VPIP)", None))
        if stats["af"] is not None and stats["af"] < 0.5:
            out.append((None, f"rarely bets or raises postflop (AF {stats['af']})", None))
    worst = min(((p, s) for p, s in stats["by_position"].items() if s["hands"] >= 30),
                key=lambda ps: ps[1]["bb_per_100"], default=None)
    if worst and worst[1]["ci95"] and worst[1]["ci95"][1] < 0:
        p, s = worst
        out.append((None, f"loses most in {p}: {s['bb_per_100']:+.1f} bb/100 over {s['hands']} hands",
                    f"arena hands --bot {bot}{scope} --pos {p}"))
    costed = sorted([l for l in out if l[0] is not None], key=lambda l: -l[0])
    return costed + [l for l in out if l[0] is None]


def costliest(conn, f, n=5) -> list:
    w = ic.Where()
    ic._common_filters(w, f, "hp")
    return conn.execute(
        f"SELECT hp.*, h.board, h.showdown FROM hand_players hp JOIN hands h "
        f"ON h.match_id = hp.match_id AND h.hand_num = hp.hand_num {w.sql()} "
        f"{'AND' if w.parts else 'WHERE'} hp.delta < 0 ORDER BY hp.delta ASC LIMIT ?",
        w.params + [n]).fetchall()


def _latest_tests(versions: set) -> list:
    out = []
    for p in reversed(pr.list_probes()):
        if p["kind"] == "test" and p["bot"].get("version") in versions:
            res = p["results"]
            failed = [r["spot"].split("/")[-1] for r in res if not r["passed"]]
            out.append(f"{p['id']}: {len(res) - len(failed)}/{len(res)} passed"
                       + (f" (failed: {', '.join(failed[:4])})" if failed else ""))
            if len(out) == 2:
                break
    return out


def _latest_compares(bot: str) -> list:
    out = []
    for c in reversed(cmp.list_compares()):
        if bot not in (c["a"]["id"], c["b"]["id"]):
            continue
        s = c["stats"]
        sign = 1 if c["a"]["id"] == bot else -1
        other = c["b"]["id"] if sign == 1 else c["a"]["id"]
        d = s["diff"]["bb_per_100"] * sign
        lo, hi = sorted(x * sign for x in s["diff"]["ci95"])
        verdict = ("better" if lo > 0 else "worse" if hi < 0 else "no significant difference")
        out.append(f"{c['id']}: vs {other} ({c['mode']}) {d:+.1f} bb/100 (CI {lo:+.1f} .. {hi:+.1f}): {verdict}")
        if len(out) == 3:
            break
    return out


# ---------------------------------------------------------------------------
# Briefs
# ---------------------------------------------------------------------------

def bot_brief(conn, f) -> dict:
    try:
        stats = ic.bot_stats(conn, f)
    except ic.IndexCliError as e:
        raise BriefError(str(e)) from None
    versions = set(stats["versions"])
    scope = f"match {f.match}" if f.match else f"{stats['matches']} match(es)"
    verdict = ""
    if stats["ci95"]:
        lo, hi = stats["ci95"]
        verdict = "winning" if lo > 0 else "losing" if hi < 0 else "not distinguishable from break-even"
    return {
        "kind": "bot", "bot": f.bot, "stats": stats,
        "header": f"brief: {f.bot} (version {', '.join(v or '?' for v in versions)}) · "
                  f"{stats['hands']:,} hands in {scope}",
        "result": f"{stats['bb_per_100']:+.1f} bb/100" + (f" ({_ci(stats)}): {verdict}" if verdict else ""),
        "style": (f"VPIP {_pct(stats['vpip'])} PFR {_pct(stats['pfr'])} 3-bet {_pct(stats['three_bet'])} "
                  f"AF {stats['af'] if stats['af'] is not None else '-'} WTSD {_pct(stats['wtsd'])} "
                  f"W$SD {_pct(stats['wsd'])}"),
        "leaks": leaks(conn, stats, f),
        "hands": costliest(conn, f),
        "tests": _latest_tests(versions),
        "compares": [] if f.match else _latest_compares(f.bot),
        "next": [f"arena stats {f.bot}" + (f" --match {f.match}" if f.match else ""),
                 f"arena hands --bot {f.bot}" + (f" --match {f.match}" if f.match else "") + " --lost-more 1000",
                 f"arena test BOT --suite basics", "arena compare NEW OLD"],
    }


def match_brief(conn, run: Run) -> dict:
    m = run.meta
    res = m.get("result") or {}
    bots = []
    for b in run.bot_ids():
        f = _filters(b, match=run.match_id)
        try:
            st = ic.bot_stats(conn, f)
        except ic.IndexCliError:
            continue
        bots.append({"bot": b, "stats": st, "leaks": leaks(conn, st, f)[:2]})
    bots.sort(key=lambda x: -x["stats"]["chips"])
    pots = conn.execute("SELECT * FROM hands WHERE match_id = ? ORDER BY pot DESC LIMIT 3",
                        (run.match_id,)).fetchall()
    cfg = m.get("config", {})
    return {
        "kind": "match", "match": run.match_id, "bots": bots, "pots": pots,
        "header": f"brief: match {run.match_id} · {res.get('n_hands', '?')} hands · seed {cfg.get('seed')} "
                  f"· blinds {cfg.get('small_blind')}/{cfg.get('big_blind')} · {m.get('status')}"
                  + (f" ({res['end_reason']})" if res.get("end_reason") else ""),
    }


def render(b: dict, max_lines: int) -> list:
    lines = [b["header"]]
    if b["kind"] == "bot":
        lines.append(f"result  {b['result']}")
        lines.append(f"style   {b['style']}")
        if b["leaks"]:
            lines.append("leaks (most costly first):")
            for _, text, cmd in b["leaks"]:
                lines.append(f"  - {text}" + (f"  -> {cmd}" if cmd else ""))
        else:
            lines.append("leaks: none flagged")
        if b["hands"]:
            lines.append("costliest hands (arena match hand REF):")
            rw = max(len(f"{r['match_id']}:{r['hand_num']}") for r in b["hands"])
            for r in b["hands"]:
                ref = f"{r['match_id']}:{r['hand_num']}"
                lines.append(f"  {ref:<{rw}}  {r['pos']:<4} {r['cards']}  "
                             f"{r['board'] or '-':<10}  {r['delta']:+,}"
                             + ("  showdown" if r["showdown"] else f"  folded {r['fold_street']}"
                                if r["fold_street"] else ""))
        for t in b["tests"]:
            lines.append(f"tests   {t}")
        for c in b["compares"]:
            lines.append(f"compare {c}")
        lines.append("next: " + " · ".join(b["next"]))
    else:
        lines.append("results:")
        for x in b["bots"]:
            s = x["stats"]
            lines.append(f"  {x['bot']:<16}{s['chips']:>+9,}  {s['bb_per_100']:+.1f} bb/100 ({_ci(s)})"
                         + (f"  errors {sum(s['decisions']['errors'].values())}"
                            if s["decisions"]["errors"] else ""))
        flagged = [(x["bot"], l) for x in b["bots"] for l in x["leaks"]]
        if flagged:
            lines.append("notable:")
            for bot, (_, text, cmd) in flagged:
                lines.append(f"  - {bot}: {text}" + (f"  -> {cmd}" if cmd else ""))
        if b["pots"]:
            lines.append("biggest pots: " + ", ".join(f"{p['match_id']}:{p['hand_num']} ({p['pot']:,})"
                                                       for p in b["pots"]))
        lines.append(f"next: arena brief BOT --match {b['match']} · arena match show {b['match']} · "
                     f"arena match hand {b['match']}:N")
    if len(lines) > max_lines:
        cut = len(lines) - (max_lines - 1)
        lines = lines[:max_lines - 1] + [f"(+{cut} more lines; raise --max-lines)"]
    return lines


def _json(b: dict) -> dict:
    out = dict(b)
    if b["kind"] == "bot":
        out["leaks"] = [{"cost_bb": c, "text": t, "command": cmd} for c, t, cmd in b["leaks"]]
        out["hands"] = [dict(r) for r in b["hands"]]
    else:
        out["bots"] = [{**x, "leaks": [{"cost_bb": c, "text": t, "command": cmd} for c, t, cmd in x["leaks"]]}
                       for x in b["bots"]]
        out["pots"] = [dict(r) for r in b["pots"]]
    return out


def cmd_brief(args):
    conn = ic._ready(args)
    target = args.target
    run = None
    if args.match:
        run = Run.open(args.match)
    elif target and not args.bot:
        try:
            run = Run.open(target)
        except FileNotFoundError:
            run = None
    if run is not None and not (args.bot or (target and target != run.match_id)):
        b = match_brief(conn, run)
    else:
        bot = args.bot or target
        if not bot:
            raise BriefError("give a bot id or a match id")
        b = bot_brief(conn, _filters(bot, match=run.match_id if run else None,
                                     version=args.version, last=args.last))
    if args.json:
        print(json.dumps(_json(b), indent=2, default=repr))
        return
    print("\n".join(render(b, args.max_lines)))


def add_parser(sub):
    import argparse
    p = sub.add_parser("brief", help="short summary of a bot or match, for an agent's context",
                       formatter_class=argparse.RawDescriptionHelpFormatter,
                       epilog="examples:\n"
                              "  arena brief mybot                 # across all its matches\n"
                              "  arena brief mybot --last 3 --max-lines 25\n"
                              "  arena brief 20261002-141530-ab12  # a match\n"
                              "  arena brief mybot --match 20261002-141530-ab12")
    p.add_argument("target", nargs="?", help="a bot id or a match id")
    p.add_argument("--bot", help="the bot (when TARGET is ambiguous)")
    p.add_argument("--match", help="limit to one match")
    p.add_argument("--version", help="only this bot version (hash prefix)")
    p.add_argument("--last", type=int, metavar="N", help="only the bot's last N matches")
    p.add_argument("--max-lines", type=int, default=40)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_brief)
