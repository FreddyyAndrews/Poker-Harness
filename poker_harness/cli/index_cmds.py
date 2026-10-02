"""`arena index | stats | hands | decisions | sql`: queries across runs.

Every query command first indexes any finished runs that aren't indexed
yet (see poker_harness/index.py), so results are always current.
"""

import json
import math
import statistics
import sys

from poker_harness import index as idx

STREETS = ["preflop", "flop", "turn", "river"]


class IndexCliError(Exception):
    pass


def _ready(args):
    idx.ensure_index(with_equity=not getattr(args, "no_equity", False),
                     progress=None if getattr(args, "json", False) else idx.stderr_progress)
    return idx.connect(readonly=True)


def _pct(x):
    return "-" if x is None else f"{x * 100:.0f}%"


def _ratio(num, den):
    return None if not den else num / den


def _chips(n):
    return f"{n:,}"


def _ref(row):
    return f"{row['match_id']}:{row['hand_num']}"


class Where:
    """Builds a WHERE clause from optional filters."""

    def __init__(self):
        self.parts, self.params = [], []

    def add(self, sql, *params):
        self.parts.append(sql)
        self.params.extend(params)

    def sql(self):
        return ("WHERE " + " AND ".join(self.parts)) if self.parts else ""


def _common_filters(w: Where, args, alias: str):
    a = alias
    if getattr(args, "bot", None):
        w.add(f"{a}.bot_id = ?", args.bot)
    if getattr(args, "version", None):
        w.add(f"{a}.version LIKE ?", args.version + "%")
    if getattr(args, "match", None):
        w.add(f"{a}.match_id = ?", args.match)
    if getattr(args, "pos", None):
        w.add(f"{a}.pos = ?", args.pos.upper())
    if getattr(args, "vs", None):
        w.add(f"EXISTS (SELECT 1 FROM hand_players o WHERE o.match_id = {a}.match_id "
              f"AND o.hand_num = {a}.hand_num AND o.bot_id = ?)", args.vs)
    if getattr(args, "last", None):
        w.add(f"{a}.match_id IN (SELECT m.match_id FROM matches m JOIN seats s "
              f"ON s.match_id = m.match_id WHERE s.bot_id = ? ORDER BY m.created DESC LIMIT ?)",
              args.bot, args.last)


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

def cmd_index(args):
    r = idx.ensure_index(with_equity=not args.no_equity, rebuild=args.rebuild,
                         match_id=args.match, progress=None if args.json else idx.stderr_progress)
    conn = idx.connect(readonly=True)
    counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in idx.TABLES}
    data = {**r, "counts": counts, "path": str(idx.index_path())}
    if args.json:
        print(json.dumps(data, indent=2))
        return
    print(f"indexed {len(r['indexed'])} run(s)"
          + (f", skipped {len(r['skipped'])} still running" if r["skipped"] else "")
          + f" · {counts['matches']} matches, {counts['hands']:,} hands, "
            f"{counts['decisions']:,} decisions · {idx.index_path()}")


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def bot_stats(conn, args) -> dict:
    w = Where()
    _common_filters(w, args, "hp")
    rows = conn.execute(f"SELECT * FROM hand_players hp {w.sql()}", w.params).fetchall()
    if not rows:
        raise IndexCliError(f"no hands for bot {args.bot!r} with those filters "
                            "(arena stats needs a bot id as shown by arena match show)")

    def summarize(rs):
        n = len(rs)
        deltas = [r["delta_bb"] for r in rs]
        mean = sum(deltas) / n
        sd = statistics.pstdev(deltas) if n > 1 else 0.0
        se = sd / math.sqrt(n) if n > 1 else None
        return {
            "hands": n,
            "bb_per_100": round(mean * 100, 2),
            "ci95": None if se is None else [round((mean - 1.96 * se) * 100, 2),
                                             round((mean + 1.96 * se) * 100, 2)],
            "chips": sum(r["delta"] for r in rs),
            "vpip": _ratio(sum(r["vpip"] for r in rs), n),
            "pfr": _ratio(sum(r["pfr"] for r in rs), n),
            "three_bet": _ratio(sum(r["three_bet"] for r in rs), sum(r["three_bet_opp"] for r in rs)),
            "three_bet_opps": sum(r["three_bet_opp"] for r in rs),
            "wtsd": _ratio(sum(r["saw_showdown"] for r in rs), sum(r["saw_flop"] for r in rs)),
            "wsd": _ratio(sum(r["won_at_showdown"] for r in rs), sum(r["saw_showdown"] for r in rs)),
        }

    out = summarize(rows)
    out["bot_id"] = args.bot
    out["matches"] = len({r["match_id"] for r in rows})
    vers = {}
    for r in rows:
        vers.setdefault(r["version"], set()).add(r["match_id"])
    out["versions"] = {v: len(m) for v, m in vers.items()}

    by_pos = {}
    for r in rows:
        by_pos.setdefault(r["pos"], []).append(r)
    order = ["UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN", "SB", "BB"]
    out["by_position"] = {p: summarize(by_pos[p]) for p in order if p in by_pos}

    wd = Where()
    _common_filters(wd, args, "d")
    decs = conn.execute(f"SELECT street, kind, owed, error, corrected, bot_ms FROM decisions d {wd.sql()}",
                        wd.params).fetchall()
    post = [d for d in decs if d["street"] != "preflop"]
    aggr = sum(1 for d in post if d["kind"] in ("bet", "raise"))
    calls = sum(1 for d in post if d["kind"] == "call")
    out["af"] = None if not calls else round(aggr / calls, 2)
    out["fold_to_bet"] = {}
    for s in STREETS:
        facing = [d for d in decs if d["street"] == s and d["owed"] > 0]
        out["fold_to_bet"][s] = {"rate": _ratio(sum(d["kind"] == "fold" for d in facing), len(facing)),
                                 "n": len(facing)}
    errors = {}
    for d in decs:
        if d["error"]:
            errors[d["error"]] = errors.get(d["error"], 0) + 1
    ms = [d["bot_ms"] for d in decs if d["bot_ms"] is not None]
    out["decisions"] = {
        "n": len(decs), "errors": errors,
        "corrected": sum(1 for d in decs if d["corrected"]),
        "bot_ms_median": round(statistics.median(ms), 2) if ms else None,
        "bot_ms_max": max(ms) if ms else None,
    }
    return out


def cmd_stats(args):
    conn = _ready(args)
    s = bot_stats(conn, args)
    if args.json:
        print(json.dumps(s, indent=2))
        return
    vers = ", ".join(f"{v or '?'} ({n} match{'es' if n != 1 else ''})" for v, n in s["versions"].items())
    ci = s["ci95"]
    lines = [
        f"{s['bot_id']} · {s['hands']:,} hands in {s['matches']} match(es) · version {vers}",
        f"win rate   {s['bb_per_100']:+.1f} bb/100"
        + (f"  (95% CI {ci[0]:+.1f} .. {ci[1]:+.1f})" if ci else "")
        + f"   chips {s['chips']:+,}",
        f"style      VPIP {_pct(s['vpip'])}  PFR {_pct(s['pfr'])}  "
        f"3-bet {_pct(s['three_bet'])} (of {s['three_bet_opps']})  AF {s['af'] if s['af'] is not None else '-'}  "
        f"WTSD {_pct(s['wtsd'])}  W$SD {_pct(s['wsd'])}",
        "folds to a bet  " + "  ".join(
            f"{st} {_pct(v['rate'])} (n={v['n']})" for st, v in s["fold_to_bet"].items() if v["n"]),
        f"  {'pos':<7}{'hands':>7}{'bb/100':>9}{'VPIP':>7}{'PFR':>6}",
    ]
    for pos, p in s["by_position"].items():
        lines.append(f"  {pos:<7}{p['hands']:>7}{p['bb_per_100']:>+9.1f}{_pct(p['vpip']):>7}{_pct(p['pfr']):>6}")
    d = s["decisions"]
    errs = ", ".join(f"{k} x{v}" for k, v in d["errors"].items()) or "none"
    lines.append(f"decisions  {d['n']:,} · errors: {errs} · corrected {d['corrected']} · "
                 f"decide() median {d['bot_ms_median']}ms max {d['bot_ms_max']}ms")
    if ci and ci[0] < 0 < ci[1]:
        lines.append("note: the confidence interval includes 0; the win rate isn't "
                     "distinguishable from break-even yet")
    print("\n".join(lines))


# ---------------------------------------------------------------------------
# hands
# ---------------------------------------------------------------------------

def cmd_hands(args):
    conn = _ready(args)
    w = Where()
    _common_filters(w, args, "hp")
    if args.showdown:
        w.add("h.showdown = 1")
    if args.no_showdown:
        w.add("h.showdown = 0")
    if args.street:
        w.add("h.last_street IN (%s)" % ",".join("?" * len(STREETS[STREETS.index(args.street):])),
              *STREETS[STREETS.index(args.street):])
    if args.lost_more is not None:
        w.add("hp.delta <= ?", -args.lost_more)
    if args.won_more is not None:
        w.add("hp.delta >= ?", args.won_more)
    if args.cards:
        w.add("hp.cards LIKE ?", f"%{args.cards}%")
    order = {"loss": "hp.delta ASC", "win": "hp.delta DESC", "pot": "h.pot DESC",
             "time": "hp.match_id, hp.hand_num"}[args.sort]
    rows = conn.execute(
        f"SELECT hp.*, h.pot, h.board, h.showdown, h.last_street FROM hand_players hp "
        f"JOIN hands h ON h.match_id = hp.match_id AND h.hand_num = hp.hand_num "
        f"{w.sql()} ORDER BY {order} LIMIT ?", w.params + [args.limit]).fetchall()
    data = [{"ref": _ref(r), **{k: r[k] for k in r.keys()}} for r in rows]
    if args.json:
        print(json.dumps(data, indent=2))
        return
    if not rows:
        print("no matching hands")
        return
    rw = max(len(_ref(r)) for r in rows)
    bw = max(len(r["bot_id"]) for r in rows)
    for r in rows:
        sd = "showdown" if r["showdown"] else (f"folded {r['fold_street']}" if r["fold_street"] else "no showdown")
        print(f"{_ref(r):<{rw}}  {r['pos'] or '-':<5} {r['bot_id']:<{bw}}  {r['cards'] or '????'}  "
              f"{r['board'] or '-':<10}  pot {_chips(r['pot']):>7}  {sd:<14} {r['delta']:>+8,}")
    print(f"({len(rows)} shown; open one with: arena match hand REF)")


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------

def cmd_decisions(args):
    conn = _ready(args)
    w = Where()
    _common_filters(w, args, "d")
    if args.hand:
        mid, _, hn = args.hand.partition(":")
        if not hn.isdigit():
            raise IndexCliError("--hand takes MATCH:HAND, e.g. six1:26")
        w.add("d.match_id = ? AND d.hand_num = ?", mid, int(hn))
    if args.street:
        w.add("d.street = ?", args.street)
    if args.action:
        w.add("d.kind = ?", args.action)
    if args.facing_bet:
        w.add("d.owed > 0")
    if args.error is not None:
        if args.error == "any":
            w.add("d.error IS NOT NULL")
        else:
            w.add("d.error = ?", args.error)
    if args.corrected:
        w.add("d.corrected = 1")
    if args.log_contains:
        w.add("EXISTS (SELECT 1 FROM logs l WHERE l.match_id = d.match_id AND "
              "l.decision_id = d.decision_id AND (l.msg LIKE ? OR l.data LIKE ?))",
              f"%{args.log_contains}%", f"%{args.log_contains}%")
    if args.equity_below is not None:
        w.add("d.equity < ?", args.equity_below)
    if args.equity_above is not None:
        w.add("d.equity > ?", args.equity_above)
    if (args.equity_below is not None or args.equity_above is not None):
        missing = conn.execute("SELECT count(*) FROM matches WHERE has_equity = 0").fetchone()[0]
        if missing:
            print(f"note: {missing} match(es) were indexed without equity; "
                  "run `arena index --rebuild` to include them", file=sys.stderr)
        own = conn.execute("SELECT count(*) FROM matches WHERE perspective = 'own'").fetchone()[0]
        if own:
            print(f"note: {own} arena match(es) are from your bot's own view; equity is only "
                  "known where every opponent showed down", file=sys.stderr)
    order = {"time": "d.match_id, d.decision_id", "equity": "d.equity ASC",
             "pot": "d.pot DESC"}[args.sort]
    rows = conn.execute(f"SELECT d.* FROM decisions d {w.sql()} ORDER BY {order} LIMIT ?",
                        w.params + [args.limit]).fetchall()
    logs = {}
    for r in rows:
        logs[(r["match_id"], r["decision_id"])] = [
            {"msg": l["msg"], "data": json.loads(l["data"])} for l in conn.execute(
                "SELECT msg, data FROM logs WHERE match_id=? AND decision_id=? ORDER BY idx",
                (r["match_id"], r["decision_id"]))]
    data = [{"ref": _ref(r), **{k: r[k] for k in r.keys()},
             "logs": logs[(r["match_id"], r["decision_id"])]} for r in rows]
    if args.json:
        print(json.dumps(data, indent=2))
        return
    if not rows:
        print("no matching decisions")
        return
    rw = max(len(_ref(r)) for r in rows)
    bw = max(len(r["bot_id"]) for r in rows)
    for r, d in zip(rows, data):
        what = r["kind"]
        if r["kind"] in ("bet", "raise"):
            what += f" to {_chips(r['amount'])}"
        elif r["kind"] == "call":
            what += f" {_chips(r['amount'])}"
        situation = (f"facing {_chips(r['owed'])} into {_chips(r['pot'])} ({r['pot_odds'] * 100:.0f}%)"
                     if r["owed"] else f"pot {_chips(r['pot'])}")
        bits = [f"{_ref(r):<{rw}} #{r['seq']:<2} {r['street']:<7} {r['pos'] or '-':<5} "
                f"{r['bot_id']:<{bw}}  {what:<16} {situation}"]
        if r["equity"] is not None:
            bits.append(f"eq {r['equity'] * 100:.0f}%")
        if r["error"]:
            bits.append(f"ERROR {r['error']}")
        if r["corrected"]:
            bits.append(f"corrected from {r['response']}")
        line = "  ".join(bits)
        if d["logs"]:
            first = d["logs"][0]
            note = (first["msg"] or "") + (" " + json.dumps(first["data"]) if first["data"] else "")
            line += f"  log: {note.strip()[:100]}"
        print(line)
    print(f"({len(rows)} shown; context: arena match hand REF --at N)")


# ---------------------------------------------------------------------------
# sql
# ---------------------------------------------------------------------------

def cmd_sql(args):
    if args.schema:
        print(idx.__doc__.strip() + "\n" + idx.SCHEMA.strip())
        return
    if not args.query:
        raise IndexCliError("give a query, or --schema to see the tables")
    conn = _ready(args)
    try:
        cur = conn.execute(args.query)
    except Exception as e:      # sqlite errors, including writes to the read-only db
        raise IndexCliError(f"sql: {e}") from None
    cols = [c[0] for c in cur.description or []]
    rows = cur.fetchmany(args.limit + 1)
    more = len(rows) > args.limit
    rows = rows[:args.limit]
    if args.json:
        print(json.dumps([dict(zip(cols, r)) for r in rows], indent=2))
        return
    text = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [min(40, max([len(c)] + [len(t[i]) for t in text])) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for t in text:
        print("  ".join(v[:40].ljust(w) for v, w in zip(t, widths)))
    print(f"({len(rows)} row(s){', more not shown; raise --limit' if more else ''})")


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def _filters(p, with_bot=True):
    if with_bot:
        p.add_argument("--bot", help="only this bot (its id, as in arena match show)")
    p.add_argument("--version", help="bot version hash (prefix ok)")
    p.add_argument("--match", help="only this match")
    p.add_argument("--pos", help="position: BTN, SB, BB, UTG, HJ, CO, ...")
    p.add_argument("--vs", metavar="BOT", help="only hands where this bot was also dealt in")
    p.add_argument("--json", action="store_true")


def add_parsers(sub):
    p = sub.add_parser("index", help="index stored runs for queries (done automatically)")
    p.add_argument("--rebuild", action="store_true", help="re-index every run")
    p.add_argument("--no-equity", action="store_true", help="skip per-decision equity (faster)")
    p.add_argument("--match", help="only (re)index this match")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("stats", help="win rate with confidence interval, style stats, leaks")
    p.add_argument("bot", help="the bot's id, as shown by arena match show")
    _filters(p, with_bot=False)
    p.add_argument("--last", type=int, metavar="N", help="only the bot's last N matches")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("hands", help="find hands, e.g. --bot X --lost-more 2000 --showdown")
    _filters(p)
    p.add_argument("--showdown", action="store_true")
    p.add_argument("--no-showdown", action="store_true")
    p.add_argument("--street", choices=STREETS, help="the hand reached this street")
    p.add_argument("--lost-more", type=int, metavar="CHIPS")
    p.add_argument("--won-more", type=int, metavar="CHIPS")
    p.add_argument("--cards", help="hole cards contain, e.g. As or AsAh")
    p.add_argument("--sort", choices=["loss", "win", "pot", "time"], default="loss")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_hands)

    p = sub.add_parser("decisions", help="find decisions, with the bot's notes",
                       epilog="equity = share of the pot against the actual hole cards "
                              "still in (hindsight), flop/turn/river only")
    _filters(p)
    p.add_argument("--hand", metavar="MATCH:HAND")
    p.add_argument("--street", choices=STREETS)
    p.add_argument("--action", choices=["fold", "check", "call", "bet", "raise"])
    p.add_argument("--facing-bet", action="store_true", help="only decisions with chips owed")
    p.add_argument("--error", nargs="?", const="any",
                   help="only failed decisions (optionally of one type: timeout, crashed, ...)")
    p.add_argument("--corrected", action="store_true", help="only replies the engine corrected")
    p.add_argument("--log-contains", metavar="TEXT")
    p.add_argument("--equity-below", type=float, metavar="F")
    p.add_argument("--equity-above", type=float, metavar="F")
    p.add_argument("--sort", choices=["time", "equity", "pot"], default="time")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_decisions)

    p = sub.add_parser("sql", help="read-only SQL over the index (--schema for tables)")
    p.add_argument("query", nargs="?")
    p.add_argument("--schema", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_sql)
