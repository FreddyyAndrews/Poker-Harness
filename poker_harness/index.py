"""
Index of stored runs: one SQLite file (runs/index.sqlite) for querying
across matches.

The index is derived data. It's built by replaying every hand from its
events (the source of truth) and joining each bot's decision records, so
it can always be deleted and rebuilt. Finished runs that aren't indexed
yet are picked up by ensure_index(), which the query commands call first.

A bot is identified by bot_id plus version (the hash of its code that
the match runner records), so stats can be split by version.

Tables (see SCHEMA below for every column):
  matches       one row per run
  seats         one row per bot per match: version, result, errors
  hands         pot, board, showdown, winners
  hand_players  one row per player dealt into a hand: position, cards,
                result, and style flags (vpip, pfr, 3-bet, saw flop, ...)
  decisions     one row per decision: situation (street, position, pot,
                amount owed, SPR, players in), what the bot did, errors,
                timing, and optionally its showdown equity at that moment
  logs          ctx.log entries, linked to decisions

Definitions:
  vpip          put chips in voluntarily preflop (call, bet or raise;
                posting a blind and checking the big blind don't count)
  pfr           bet or raised preflop
  three_bet     raised preflop when facing exactly one raise
                (three_bet_opp: faced exactly one raise at some point)
  kind          fold | check | call | bet | raise. An all-in counts as a
                call if it doesn't raise the bet, otherwise bet/raise;
                "bet" is the first aggression on a postflop street
  equity        the acting player's share of the pot at showdown against
                the actual hole cards of everyone still in, over all
                remaining boards (hindsight: uses cards the bot couldn't
                see). Flop, turn and river only; NULL preflop. In arena
                records (perspective "own") it's only set when every
                opponent still in the hand later showed their cards, and
                hand_players.cards is NULL for hands that weren't shown.
  delta_bb      chips won or lost in the hand, in big blinds
"""

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

from poker_harness.engine.game import PokerEngine
from poker_harness.equity import equity as compute_equity
from poker_harness.runs import Run, list_runs, runs_dir

INDEX_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY, created REAL, status TEXT, end_reason TEXT,
    n_hands INTEGER, seed INTEGER, small_blind INTEGER, big_blind INTEGER,
    starting_stack INTEGER, ranked INTEGER, duration_s REAL, run_dir TEXT,
    has_equity INTEGER, events_bytes INTEGER, indexed_at REAL,
    perspective TEXT  -- god (local/mock runs) or own (arena records: one bot's view)
);

CREATE TABLE IF NOT EXISTS seats (
    match_id TEXT, seat INTEGER, bot_id TEXT, version TEXT, path TEXT, kind TEXT,
    final_stack INTEGER, chip_delta INTEGER, errors INTEGER, restarts INTEGER,
    PRIMARY KEY (match_id, seat)
);

CREATE TABLE IF NOT EXISTS hands (
    match_id TEXT, hand_num INTEGER, dealer_seat INTEGER, players INTEGER,
    pot INTEGER, showdown INTEGER, board TEXT, last_street TEXT,
    winners TEXT, uncalled INTEGER,
    PRIMARY KEY (match_id, hand_num)
);

CREATE TABLE IF NOT EXISTS hand_players (
    match_id TEXT, hand_num INTEGER, seat INTEGER, bot_id TEXT, version TEXT,
    pos TEXT, start_stack INTEGER, cards TEXT, delta INTEGER, delta_bb REAL,
    vpip INTEGER, pfr INTEGER, three_bet INTEGER, three_bet_opp INTEGER,
    saw_flop INTEGER, saw_showdown INTEGER, won INTEGER, won_at_showdown INTEGER,
    fold_street TEXT, decisions INTEGER,
    PRIMARY KEY (match_id, hand_num, seat)
);

CREATE TABLE IF NOT EXISTS decisions (
    match_id TEXT, decision_id INTEGER, hand_num INTEGER, seq INTEGER,
    seat INTEGER, bot_id TEXT, version TEXT, street TEXT, pos TEXT,
    pot INTEGER, owed INTEGER, stack INTEGER, pot_odds REAL, spr REAL,
    players_in INTEGER, action TEXT, amount INTEGER, kind TEXT,
    response TEXT, corrected INTEGER, error TEXT, detail TEXT,
    bot_ms REAL, elapsed_ms REAL, n_logs INTEGER, equity REAL,
    PRIMARY KEY (match_id, decision_id)
);

CREATE TABLE IF NOT EXISTS logs (
    match_id TEXT, decision_id INTEGER, idx INTEGER, msg TEXT, data TEXT,
    PRIMARY KEY (match_id, decision_id, idx)
);

CREATE INDEX IF NOT EXISTS hp_bot ON hand_players (bot_id, version);
CREATE INDEX IF NOT EXISTS dec_bot ON decisions (bot_id, version, street);
CREATE INDEX IF NOT EXISTS dec_hand ON decisions (match_id, hand_num);
"""

TABLES = ["matches", "seats", "hands", "hand_players", "decisions", "logs"]


def index_path(root: Optional[Path] = None) -> Path:
    return (root or runs_dir()) / "index.sqlite"


def connect(root: Optional[Path] = None, readonly: bool = False) -> sqlite3.Connection:
    path = index_path(root)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM index_meta WHERE key='version'").fetchone()
        if row is None or int(row[0]) != INDEX_VERSION:
            for t in TABLES:
                conn.execute(f"DELETE FROM {t}")
            conn.execute("INSERT OR REPLACE INTO index_meta VALUES ('version', ?)",
                         (str(INDEX_VERSION),))
            conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def action_kind(action: str, total: int, bet_before: int, street: str) -> str:
    """fold | check | call | bet | raise (see the module docstring)."""
    if action in ("fold", "check", "call"):
        return action
    # raise / all_in: total is the player's street total after acting
    if total <= bet_before:
        return "call"
    if street != "preflop" and bet_before == 0:
        return "bet"
    return "raise"


def index_run(conn: sqlite3.Connection, run: Run, with_equity: bool = True) -> int:
    """(Re)index one run. Returns the number of hands indexed."""
    mid  = run.match_id
    meta = run.meta
    cfg  = meta.get("config", {})
    res  = meta.get("result") or {}
    for t in TABLES:
        conn.execute(f"DELETE FROM {t} WHERE match_id=?", (mid,))

    versions = {s["bot_id"]: s.get("version") for s in meta.get("seats", [])}
    for s in meta.get("seats", []):
        b = s["bot_id"]
        conn.execute("INSERT INTO seats VALUES (?,?,?,?,?,?,?,?,?,?)", (
            mid, s["seat"], b, s.get("version"), s.get("path"), s.get("kind"),
            res.get("final_stacks", {}).get(b), res.get("chip_delta", {}).get(b),
            res.get("error_counts", {}).get(b), res.get("restarts", {}).get(b)))

    records = {}
    for b in run.bot_ids():
        for d in run.decisions(b):
            records[d["decision_id"]] = d

    hands = {}
    for ev in run.events():
        if ev.get("hand_num") is not None:
            hands.setdefault(ev["hand_num"], []).append(ev)

    n = 0
    for hand_num in sorted(hands):
        evs = hands[hand_num]
        if any(e["type"] == "hand_start" for e in evs) and any(e["type"] == "hand_end" for e in evs):
            _index_hand(conn, mid, evs, records, versions, cfg, with_equity)
            n += 1

    conn.execute("INSERT INTO matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        mid, meta.get("created"), meta.get("status"), res.get("end_reason"),
        res.get("n_hands", n), cfg.get("seed"), cfg.get("small_blind"), cfg.get("big_blind"),
        cfg.get("starting_stack"), int(bool(cfg.get("ranked", True))), res.get("duration_s"),
        str(run.dir), int(with_equity), _size(run), time.time(), meta.get("perspective", "god")))
    conn.commit()
    return n


def _index_hand(conn, mid, evs, records, versions, cfg, with_equity):
    hs      = next(e for e in evs if e["type"] == "hand_start")
    end     = next(e for e in evs if e["type"] == "hand_end")
    actions = [e for e in evs if e["type"] == "action"]
    names   = hs["bot_ids"]
    bb      = hs["blinds"][1]
    pos     = {int(s): p for s, p in hs["positions"].items()}
    holes   = {int(s): c for s, c in hs["hole_cards"].items()}
    # arena records only know some hole cards (see arena_records.py)
    known   = set(hs["known_seats"]) if "known_seats" in hs else set(holes)

    eng = PokerEngine(hs["hand_id"], names, dealer_seat=hs["dealer_seat"],
                      starting_stacks=dict(zip(names, hs["stacks"])),
                      small_blind=hs["blinds"][0], big_blind=bb, hand_num=hs["hand_num"],
                      hole_cards=holes, board=hs["board_plan"])
    eng.start_hand()

    flags = {s: {"vpip": 0, "pfr": 0, "three_bet": 0, "three_bet_opp": 0,
                 "fold_street": None, "decisions": 0} for s in holes}
    pre_raises = 0

    for seq, ev in enumerate(actions):
        seat   = ev["seat"]
        p      = eng.players[seat]
        street = eng.street
        pot    = eng.pot
        owed   = max(0, eng.current_bet - p.bet_this_street)
        stack  = p.stack
        bet_before = eng.current_bet
        others = [o.stack + o.bet_this_street for o in eng.players
                  if o.in_hand and o.seat != seat]
        eff    = min(p.stack + p.bet_this_street, max(others)) if others else 0
        spr    = round(eff / pot, 3) if pot else None
        live   = [o.seat for o in eng.players if o.in_hand]

        eq = None
        if with_equity and street != "preflop" and len(live) >= 2 and set(live) <= known:
            board = "".join(str(c) for c in eng.community_cards)
            r = compute_equity(["".join(holes[s]) for s in live], board=board)
            eq = round(r["players"][live.index(seat)]["equity"], 4)

        total = ev["amount"] if ev["action"] in ("raise", "all_in") else 0
        kind  = action_kind(ev["action"], total, bet_before, street)

        f = flags[seat]
        f["decisions"] += 1
        if street == "preflop":
            if kind in ("call", "bet", "raise"):
                f["vpip"] = 1
            if kind in ("bet", "raise"):
                f["pfr"] = 1
            if pre_raises == 1:
                f["three_bet_opp"] = 1
                if kind == "raise":
                    f["three_bet"] = 1
            if kind in ("bet", "raise"):
                pre_raises += 1
        if kind == "fold":
            f["fold_street"] = street

        raw = {"action": ev["action"]}
        if ev["action"] == "raise":
            raw["amount"] = ev["amount"]
        eng.apply_action(seat, raw, strict=True)

        rec = records.get(ev.get("decision_id"), {})
        logs = rec.get("logs") or []
        conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            mid, ev.get("decision_id"), hs["hand_num"], seq, seat, names[seat],
            versions.get(names[seat]), street, pos.get(seat), pot, owed, stack,
            round(owed / (pot + owed), 4) if owed else 0.0, spr, len(live),
            ev["action"], ev["amount"], kind,
            json.dumps(rec.get("response")) if rec else None,
            int(bool(rec.get("corrected"))) if rec else None,
            rec.get("error"), rec.get("detail"), rec.get("bot_ms"), rec.get("elapsed_ms"),
            len(logs), eq))
        for i, entry in enumerate(logs):
            conn.execute("INSERT INTO logs VALUES (?,?,?,?,?)", (
                mid, ev.get("decision_id"), i, entry.get("msg"),
                json.dumps(entry.get("data") or {})))

    board    = end["board"]
    showdown = bool(end["showdown"])
    won_amt  = {}
    for w in end["winners"]:
        won_amt[w["seat"]] = won_amt.get(w["seat"], 0) + w["amount"]
    last_street = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}[len(board)]

    conn.execute("INSERT INTO hands VALUES (?,?,?,?,?,?,?,?,?,?)", (
        mid, hs["hand_num"], hs["dealer_seat"], len(holes), end["pot"], int(showdown),
        "".join(board), last_street, json.dumps(end["winners"]),
        (end.get("uncalled") or {}).get("amount", 0)))

    for seat in sorted(holes):
        f     = flags[seat]
        b     = names[seat]
        delta = end["delta"][b]
        in_at_end = f["fold_street"] is None
        saw_flop  = int(len(board) >= 3 and f["fold_street"] != "preflop")
        conn.execute("INSERT INTO hand_players VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            mid, hs["hand_num"], seat, b, versions.get(b), pos.get(seat),
            hs["stacks"][seat], "".join(holes[seat]) if seat in known else None, delta, round(delta / bb, 3),
            f["vpip"], f["pfr"], f["three_bet"], f["three_bet_opp"], saw_flop,
            int(showdown and in_at_end), int(won_amt.get(seat, 0) > 0),
            int(showdown and won_amt.get(seat, 0) > 0), f["fold_street"], f["decisions"]))


def _size(run) -> int:
    """Size of the run's log, to notice when it has grown since indexing."""
    for name in ("events.jsonl", "stream.jsonl"):
        f = run.dir / name
        if f.exists():
            return f.stat().st_size
    return 0


def ensure_index(root: Optional[Path] = None, with_equity: bool = True,
                 rebuild: bool = False, match_id: Optional[str] = None,
                 progress=None) -> dict:
    """Index finished runs that are new or changed since last indexed (or
    all of them with rebuild). Returns {"indexed": [...], "skipped": [...]}."""
    conn = connect(root)
    try:
        if rebuild:
            for t in TABLES:
                conn.execute(f"DELETE FROM {t}")
            conn.commit()
        known = {r["match_id"]: r for r in conn.execute(
            "SELECT match_id, events_bytes, has_equity FROM matches")}
        done, skipped = [], []
        for run in list_runs(root):
            if match_id and run.match_id != match_id:
                continue
            if run.meta.get("status") == "running":
                skipped.append(run.match_id)
                continue
            size = _size(run)
            row  = known.get(run.match_id)
            if row and row["events_bytes"] == size and (row["has_equity"] or not with_equity):
                continue
            if progress:
                progress(f"indexing {run.match_id}...")
            index_run(conn, run, with_equity=with_equity)
            done.append(run.match_id)
        return {"indexed": done, "skipped": skipped}
    finally:
        conn.close()


def stderr_progress(msg: str) -> None:
    print(msg, file=sys.stderr)
