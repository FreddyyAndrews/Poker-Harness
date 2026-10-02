"""Arena records (the bridge's own view of production matches) read by the
local tools. A real match is played against the mock arena, which keeps a
god-view record of it; the bridge's own-view record must agree with it on
everything public and reveal nothing that wasn't shown."""
import asyncio
import json
import shutil

import pytest

pytest.importorskip("fastapi")

from poker_harness import index as idx  # noqa: E402
from poker_harness.arena_records import ArenaRecord  # noqa: E402
from poker_harness.cli import index_cmds as ic  # noqa: E402
from poker_harness.replay import verify_run  # noqa: E402
from poker_harness.runs import Run, list_runs  # noqa: E402
from test_mock import FAST, Server  # noqa: E402

# calls through the flop; on the turn and river folds to bets in even
# hands and calls in odd ones, so some hands end without a showdown
SOMETIMES_FOLDS = """
    def decide(state, ctx):
        ctx.log("seen", street=state["street"])
        if state["can_check"]:
            return {"action": "check"}
        late = state["street"] in ("turn", "river")
        return {"action": "fold" if late and state["hand_num"] % 2 == 0 else "call"}
"""


@pytest.fixture(scope="module")
def played(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("records")

    async def go():
        async with Server(tmp, runs_root=tmp / "server-runs") as srv:
            srv.bot("mybot")
            bridge = srv.bridge("mybot", src=SOMETIMES_FOLDS, max_matches=1,
                                records={"dir": str(tmp / "runs" / "arena")},
                                matchmaking={"enabled": True, "interval_s": 0.1,
                                             "opponents": ["house-aggro"],
                                             "formats": [{"seats": 2, "hands": 40, "reset_stacks": True,
                                                          "clock": FAST}]})
            await bridge.run()
            return next(iter(srv.arena.matches))
    mid = asyncio.run(asyncio.wait_for(go(), 60))
    own = Run.open(f"arena/{mid}", tmp / "runs")
    god = Run.open(mid, tmp / "server-runs")
    return tmp, mid, own, god


def by_hand(run, kind):
    out = {}
    for e in run.events():
        if e["type"] == kind:
            out.setdefault(e["hand_num"], []).append(e)
    return out


# ---------------------------------------------------------------------------
# The converted record
# ---------------------------------------------------------------------------

def test_record_opens_as_a_run_and_replays_exactly(played):
    tmp, mid, own, god = played
    assert isinstance(own, ArenaRecord) and own.match_id == f"arena/{mid}"
    assert own.meta["perspective"] == "own" and own.meta["status"] == "complete"
    assert own.hand_nums() == god.hand_nums()
    assert verify_run(own) == {}


def test_public_facts_match_the_god_view(played):
    tmp, mid, own, god = played
    pub = lambda evs: [(e["street"], e["seat"], e["action"], e["amount"]) for e in evs]
    o_act, g_act = by_hand(own, "action"), by_hand(god, "action")
    assert {h: pub(v) for h, v in o_act.items()} == {h: pub(v) for h, v in g_act.items()}
    for h, [oe] in by_hand(own, "hand_end").items():
        [ge] = by_hand(god, "hand_end")[h]
        for key in ("final_stacks", "delta", "winners", "uncalled", "showdown", "board", "pot"):
            assert oe[key] == ge[key], (h, key)
    assert own.meta["result"]["chip_delta"] == god.meta["result"]["chip_delta"]


def test_only_shown_cards_are_known(played):
    tmp, mid, own, god = played
    me = own.my_seat
    ends = {h: e for h, [e] in by_hand(own, "hand_end").items()}
    showdowns = 0
    for h, [hs] in by_hand(own, "hand_start").items():
        [ghs] = by_hand(god, "hand_start")[h]
        expected = {me} | ({0, 1} if ends[h]["showdown"] else set())
        assert set(hs["known_seats"]) == expected
        for seat in hs["known_seats"]:
            assert hs["hole_cards"][str(seat)] == ghs["hole_cards"][str(seat)]
        assert hs["board_plan"][:hs["known_board"]] == ghs["board_plan"][:hs["known_board"]]
        showdowns += ends[h]["showdown"]
    assert 0 < showdowns < len(ends)                      # both kinds of hand were played


def test_decisions_are_what_the_bot_saw(played):
    tmp, mid, own, god = played
    o = list(own.decisions("mybot"))
    g = list(god.decisions("mybot"))
    assert len(o) == len(g) and list(own.decisions("house-aggro")) == []
    for a, b in zip(o, g):
        assert a["state"] == b["state"]
        assert a["applied"]["action"] == b["applied"]["action"]
        assert a["logs"] == b["logs"]


def test_reconnects_are_merged(played, tmp_path):
    tmp, mid, own, god = played
    src = own.dir
    lines = (src / "stream.jsonl").read_text().splitlines()
    msgs = [json.loads(l) for l in lines]
    # pick a hand with a decision; pretend the stream dropped after its first
    # two messages and came back with match_full replaying the hand
    start = next(i for i, m in enumerate(msgs) if m["type"] == "hand_start" and m["hand_num"] == 5)
    decide = next(i for i in range(start, len(msgs)) if msgs[i]["type"] == "decide")
    hand = [m for m in msgs[start:decide] if m["type"] != "decide"]
    full = {"type": "match_full", "match": json.loads((src / "meta.json").read_text())["match"],
            "hands_played": 5, "stacks": [10000, 10000], "bank_s": 1.0, "hand": hand,
            "pending": msgs[decide]}
    new = lines[:start + 2] + [json.dumps(full)] + lines[decide + 1:]
    copy = tmp_path / "copy"
    shutil.copytree(src, copy)
    (copy / "stream.jsonl").write_text("\n".join(new) + "\n")
    assert list(ArenaRecord(copy).events()) == list(own.events())


# ---------------------------------------------------------------------------
# Index and stats
# ---------------------------------------------------------------------------

def stats(root, bot):
    idx.ensure_index(root)
    conn = idx.connect(root, readonly=True)
    return conn, ic.bot_stats(conn, ic.Where and type("A", (), {
        "bot": bot, "match": None, "version": None, "last": None, "pos": None, "vs": None})())


def test_stats_from_the_own_view_equal_the_god_view(played):
    tmp, mid, own, god = played
    oc, o = stats(tmp / "runs", "mybot")
    gc, g = stats(tmp / "server-runs", "mybot")
    for key in ("hands", "bb_per_100", "ci95", "chips", "vpip", "pfr", "three_bet", "wtsd", "wsd",
                "af", "fold_to_bet", "by_position"):
        assert o[key] == g[key], key
    assert oc.execute("SELECT perspective FROM matches").fetchone()[0] == "own"


def test_equity_only_where_cards_were_shown(played):
    tmp, mid, own, god = played
    oc, _ = stats(tmp / "runs", "mybot")
    gc, _ = stats(tmp / "server-runs", "mybot")
    own_eq = dict(oc.execute("SELECT hand_num || ':' || seq, equity FROM decisions WHERE equity IS NOT NULL"))
    god_eq = dict(gc.execute("SELECT hand_num || ':' || seq, equity FROM decisions WHERE equity IS NOT NULL"))
    assert own_eq and len(own_eq) < len(god_eq)
    assert all(god_eq[k] == v for k, v in own_eq.items())
    hidden = oc.execute("""SELECT count(*) FROM hand_players hp JOIN hands h USING (match_id, hand_num)
                           WHERE hp.bot_id = 'house-aggro' AND h.showdown = 0 AND hp.cards IS NOT NULL""")
    assert hidden.fetchone()[0] == 0


def test_god_view_wins_when_both_records_exist(played, tmp_path):
    tmp, mid, own, god = played
    shutil.copytree(tmp / "runs", tmp_path / "runs")
    assert [r.match_id for r in list_runs(tmp_path / "runs")] == [f"arena/{mid}"]
    shutil.copytree(god.dir, tmp_path / "runs" / mid)
    assert [r.match_id for r in list_runs(tmp_path / "runs")] == [mid]
