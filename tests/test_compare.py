"""Duplicate comparisons (arena/compare.py), reset-stack matches and the CLI."""
import asyncio
import textwrap

import pytest

from arena import compare as cmp
from arena.match import MatchConfig, MatchRunner
from arena.runs import Run, RunWriter
from arena.seats import CallbackSeat

CALLER = """
    def decide(state):
        return {"action": "check" if state["can_check"] else "call"}
"""

# raises with pairs and aces, otherwise check/call: deterministic and
# better than calling everything
PAIRS = """
    def decide(state):
        cards = state["your_cards"]
        legal = state["legal_actions"]
        strong = cards[0][0] == cards[1][0] or "A" in (cards[0][0], cards[1][0])
        if strong and legal["can_raise"]:
            return {"action": "raise", "amount": legal["min_raise_to"] * 2}
        return {"action": "check" if state["can_check"] else "call"}
"""


@pytest.fixture
def bots(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_RUNS", str(tmp_path / "runs"))
    out = {}
    for name, src in (("caller", CALLER), ("pairs", PAIRS)):
        d = tmp_path / name
        d.mkdir()
        (d / "bot.py").write_text(textwrap.dedent(src))
        out[name] = str(d / "bot.py")
    return out


# ---------------------------------------------------------------------------
# reset_stacks
# ---------------------------------------------------------------------------

def test_reset_stacks_keeps_hands_independent(tmp_path):
    def shove(s):
        return {"action": "all_in"}

    def call(s):
        return "x" if s["can_check"] else "c"

    seats = {"a": CallbackSeat("a", shove), "b": CallbackSeat("b", call)}
    r = asyncio.run(MatchRunner("rs", seats, MatchConfig(n_hands=30, seed=1, reset_stacks=True),
                                writer=RunWriter("rs", tmp_path), keep_hands=True).run())
    assert r["n_hands"] == 30 and r["end_reason"] == "hands_complete"
    assert r["chip_delta"]["a"] == sum(h["delta"]["a"] for h in r["hands"])
    assert r["chip_delta"]["a"] == -r["chip_delta"]["b"]
    starts = {tuple(e["stacks"]) for e in Run.open("rs", tmp_path).events() if e["type"] == "hand_start"}
    assert starts == {(10_000, 10_000)}


# ---------------------------------------------------------------------------
# plan / analyse
# ---------------------------------------------------------------------------

def test_plan_rotates_every_player_through_every_seat():
    p = cmp.plan("x/a.py", "x/b.py", [])
    assert p["mode"] == "head-to-head"
    assert [t[2] for t in p["tables"]] == [["a", "b"], ["b", "a"]]
    p = cmp.plan("x/a.py", "x/b.py", ["f/f1.py", "f/f2.py"])
    assert p["mode"] == "field"
    a_tables = [t[2] for t in p["tables"] if t[0] == "A"]
    assert a_tables == [["a", "f1", "f2"], ["f1", "f2", "a"], ["f2", "a", "f1"]]
    for seat in range(3):
        assert {t[seat] for t in a_tables} == {"a", "f1", "f2"}
    b_tables = [t[2] for t in p["tables"] if t[0] == "B"]
    assert [t.index("b") for t in b_tables] == [t.index("a") for t in a_tables]
    assert len(cmp.plan("a.py", "b.py", ["c.py"], duplicate=False)["tables"]) == 2
    with pytest.raises(cmp.CompareError, match="at most 7"):
        cmp.plan("a.py", "b.py", [f"f{i}.py" for i in range(8)])


def test_analyse_head_to_head():
    p = {"mode": "head-to-head", "a": "a", "b": "b"}
    # every deal: A wins 100 in rotation 0 and loses 100 in rotation 1 (pure card luck)
    luck = {("h2h", 0): [{"a": 100, "b": -100}] * 50, ("h2h", 1): [{"a": -100, "b": 100}] * 50}
    s = cmp.analyse(p, luck, 100)
    assert s["diff"]["bb_per_100"] == 0 and s["diff"]["se"] == 0
    assert s["naive"]["se"] > 0 and s["verdict"] == "no_difference"
    # A wins 1bb more than it loses on every deal -> +50 bb/100 (averaged over 2 rotations)
    edge = {("h2h", 0): [{"a": 100, "b": -100}, {"a": 200, "b": -200}] * 20,
            ("h2h", 1): [{"a": 0, "b": 0}, {"a": -100, "b": 100}] * 20}
    s = cmp.analyse(p, edge, 100)
    assert s["a"]["bb_per_100"] == 50 and s["verdict"] == "a_better"


def test_analyse_field_pairs_the_same_deals():
    p = {"mode": "field", "a": "a", "b": "b"}
    deals = [300, -500, 800, -100]                 # card luck per deal, shared by A and B
    data = {("A", 0): [{"a": d + 10} for d in deals], ("B", 0): [{"b": d} for d in deals]}
    s = cmp.analyse(p, data, 100)
    assert s["diff"]["bb_per_100"] == 10 and s["diff"]["se"] == pytest.approx(0, abs=1e-9)
    assert s["naive"]["se"] > 100 and s["verdict"] == "a_better"


# ---------------------------------------------------------------------------
# run_compare
# ---------------------------------------------------------------------------

def test_identical_deterministic_bots_cancel_exactly(bots):
    info = asyncio.run(cmp.run_compare(bots["pairs"], bots["pairs"], [], hands=60, seed=4))
    assert info["stats"]["diff"]["bb_per_100"] == 0 and info["stats"]["diff"]["se"] == 0
    assert info["stats"]["verdict"] == "no_difference"


def test_rotations_deal_the_same_cards_to_the_same_seats(bots):
    info = asyncio.run(cmp.run_compare(bots["pairs"], bots["caller"], [], hands=20, seed=9))
    r0, r1 = (Run.open(m) for m in info["matches"])
    h0 = [e for e in r0.events() if e["type"] == "hand_start"]
    h1 = [e for e in r1.events() if e["type"] == "hand_start"]
    assert [h["hole_cards"] for h in h0] == [h["hole_cards"] for h in h1]
    assert [h["dealer_seat"] for h in h0] == [h["dealer_seat"] for h in h1]
    assert h0[0]["bot_ids"] == list(reversed(h1[0]["bot_ids"]))
    assert r0.meta["labels"]["compare"] == info["id"] and r0.meta["config"]["reset_stacks"]


def test_better_bot_wins_against_a_field(bots):
    info = asyncio.run(cmp.run_compare(bots["pairs"], bots["caller"], [bots["caller"]],
                                       hands=150, seed=3))
    s = info["stats"]
    assert info["mode"] == "field" and len(info["matches"]) == 4
    assert s["diff"]["se"] < s["naive"]["se"]
