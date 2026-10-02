"""The run index and the query commands, checked against matches whose
stats are known from the seats' fixed behaviour."""
import asyncio
import sqlite3

import pytest

from arena import index as idx
from arena.equity import equity
from arena.match import MatchConfig, MatchRunner, make_bot_seats
from arena.runs import Run, RunWriter
from arena.seats import CallbackSeat


def raiser(state):
    """Min-raises the first time it acts preflop; then checks/calls."""
    legal = state["legal_actions"]
    mine = [a for a in state["action_log"] if a["seat"] == state["seat_to_act"]
            and a["action"] not in ("small_blind", "big_blind")]
    if state["street"] == "preflop" and not mine and legal["can_raise"]:
        return {"action": "raise", "amount": legal["min_raise_to"]}
    return "x" if state["can_check"] else "c"


def caller(state):
    return "x" if state["can_check"] else "c"


def folder(state):
    return "x" if state["can_check"] else "f"


def junk(state):
    return {"action": "raise", "amount": 1}


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "runs"
    monkeypatch.setenv("ARENA_RUNS", str(r))
    monkeypatch.setenv("ARENA_SPOTS", str(tmp_path / "spots"))
    return r


def play(root, match_id, seats, n_hands=30, seed=1):
    runner = MatchRunner(match_id, seats, MatchConfig(n_hands=n_hands, seed=seed),
                         writer=RunWriter(match_id, root))
    return asyncio.run(runner.run())


def three(root, match_id="m1", **kw):
    seats = {"raiser": CallbackSeat("raiser", raiser),
             "caller": CallbackSeat("caller", caller),
             "folder": CallbackSeat("folder", folder)}
    return play(root, match_id, seats, **kw)


def q(conn, sql, *params):
    return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Building the index
# ---------------------------------------------------------------------------

def test_style_flags_match_known_behaviour(root):
    result = three(root)
    idx.ensure_index(root)
    c = idx.connect(root, readonly=True)
    by_bot = {r["bot_id"]: r for r in q(c, """
        SELECT bot_id, count(*) n, sum(vpip) vpip, sum(pfr) pfr, sum(three_bet) tb,
               sum(three_bet_opp) tbo, sum(saw_flop) sf, sum(saw_showdown) ss, sum(delta) d
        FROM hand_players GROUP BY bot_id""")}
    assert {b: r["n"] for b, r in by_bot.items()} == {"raiser": 30, "caller": 30, "folder": 30}
    f, cl, r = by_bot["folder"], by_bot["caller"], by_bot["raiser"]
    assert (f["vpip"], f["pfr"], f["sf"]) == (0, 0, 0)
    assert (r["vpip"], r["pfr"]) == (30, 30)
    assert (cl["vpip"], cl["pfr"], cl["tbo"], cl["tb"]) == (30, 0, 30, 0)
    assert r["tbo"] == 0
    # raiser and caller see every flop and every showdown (nobody folds postflop)
    assert r["sf"] == r["ss"] == cl["sf"] == cl["ss"] == 30
    # chips agree with the match result
    for b in ("raiser", "caller", "folder"):
        assert by_bot[b]["d"] == result["chip_delta"][b]


def test_each_hand_balances_and_rows_link(root):
    three(root, n_hands=10)
    idx.ensure_index(root)
    c = idx.connect(root, readonly=True)
    assert all(r[0] == 0 for r in q(c, "SELECT sum(delta) FROM hand_players GROUP BY match_id, hand_num"))
    n_actions = sum(1 for e in Run.open("m1", root).events() if e["type"] == "action")
    assert q(c, "SELECT count(*) FROM decisions")[0][0] == n_actions
    assert q(c, "SELECT count(*) FROM decisions WHERE response IS NULL")[0][0] == 0


def test_decision_equity_matches_equity_calculator(root):
    three(root, n_hands=5)
    idx.ensure_index(root)
    c = idx.connect(root, readonly=True)
    assert q(c, "SELECT count(*) FROM decisions WHERE street='preflop' AND equity IS NOT NULL")[0][0] == 0
    d = q(c, "SELECT * FROM decisions WHERE street='flop' ORDER BY match_id, decision_id LIMIT 1")[0]
    run = Run.open("m1", root)
    hs = next(e for e in run.hand_events(d["hand_num"]) if e["type"] == "hand_start")
    live = [int(s) for s in sorted(hs["hole_cards"], key=int)
            if q(c, "SELECT fold_street FROM hand_players WHERE hand_num=? AND seat=?",
                 d["hand_num"], int(s))[0][0] != "preflop"]
    board = "".join(hs["board_plan"][:3])
    r = equity(["".join(hs["hole_cards"][str(s)]) for s in live], board=board)
    assert d["equity"] == round(r["players"][live.index(d["seat"])]["equity"], 4)


def test_kinds():
    assert idx._kind("raise", 300, 100, "preflop") == "raise"
    assert idx._kind("raise", 200, 0, "flop") == "bet"
    assert idx._kind("all_in", 80, 100, "flop") == "call"     # all-in for less
    assert idx._kind("all_in", 900, 100, "turn") == "raise"
    assert idx._kind("check", 0, 0, "river") == "check"


def test_incremental_rebuild_and_no_equity(root):
    three(root, "a", n_hands=5)
    assert idx.ensure_index(root)["indexed"] == ["a"]
    assert idx.ensure_index(root)["indexed"] == []
    three(root, "b", n_hands=5)
    assert idx.ensure_index(root)["indexed"] == ["b"]
    assert sorted(idx.ensure_index(root, rebuild=True)["indexed"]) == ["a", "b"]
    idx.ensure_index(root, rebuild=True, with_equity=False)
    c = idx.connect(root, readonly=True)
    assert q(c, "SELECT count(*) FROM decisions WHERE equity IS NOT NULL")[0][0] == 0
    # asking for equity again re-indexes the runs that lack it
    assert sorted(idx.ensure_index(root)["indexed"]) == ["a", "b"]


def test_running_matches_are_skipped(root):
    w = RunWriter("live", root)
    w.write_meta({"created": 0, "status": "running", "config": {}, "seats": []})
    assert idx.ensure_index(root)["skipped"] == ["live"]


def test_index_version_change_clears_tables(root):
    three(root, n_hands=3)
    idx.ensure_index(root)
    conn = sqlite3.connect(idx.index_path(root))
    conn.execute("UPDATE index_meta SET value='0' WHERE key='version'")
    conn.commit()
    conn.close()
    c = idx.connect(root)
    assert q(c, "SELECT count(*) FROM matches")[0][0] == 0
    c.close()
    assert idx.ensure_index(root)["indexed"] == ["m1"]
