"""Probes and sweeps, with bots whose answers are known in advance."""
import asyncio
import textwrap

import pytest

from arena import probe as pr
from arena.match import MatchConfig, MatchRunner, make_bot_seats
from arena.runs import Run, RunWriter
from arena.spot import Spot

# folds to anything over 500, raises to pot when it can check, else calls
THRESHOLD = """
    def decide(state, ctx):
        ctx.log("owed", owed=state["amount_owed"])
        if state["can_check"]:
            return {"action": "raise", "amount": state["pot"]}
        if state["amount_owed"] > 500:
            return {"action": "fold"}
        return {"action": "call"}
"""

# remembers how many decisions it has made, and what it was told
ADAPTIVE = """
    SEEN = 0
    def decide(state, ctx):
        global SEEN
        SEEN += 1
        log = state["match_action_log"]
        ctx.log("seen", n=SEEN, log_len=len(log), last=log[-1] if log else None)
        return {"action": "check" if state["can_check"] else "call"}
"""

JUNK = """
    def decide(state):
        return {"action": "raise", "amount": 1}
"""

FACING_600 = dict(players=6, button=3, cards="BTN=AsKh BB=QdQc", board="Kd7c2s|9h|",
                  actions="pre: BTN r250, BB r900, BTN c; flop: BB r600", to_act="BTN", seed=1)


@pytest.fixture
def bot(tmp_path):
    def make(name, src):
        p = tmp_path / f"{name}.py"
        p.write_text(textwrap.dedent(src))
        return str(p)
    return make


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("ARENA_SPOTS", str(tmp_path / "spots"))
    return tmp_path


def probe(path, target, n=3, **kw):
    return asyncio.run(pr.run_probe(path, target, n, **kw))


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def test_probe_spot_folds_big_bets(bot):
    target = pr.target_from_spot(Spot.from_dict(FACING_600))
    res = probe(bot("t", THRESHOLD), target, n=4)
    s = pr.summarize(res["samples"], target)
    assert s["freq"] == {"fold": 1.0} and s["n"] == 4
    assert res["samples"][0]["logs"] == [{"msg": "owed", "data": {"owed": 600}}]
    assert target.state["match_action_log"] == []


def test_probe_reports_applied_action_and_sizes(bot):
    spot = Spot.from_dict({**FACING_600, "actions": "pre: BTN r250, BB c; flop: BB x", "to_act": "BTN"})
    target = pr.target_from_spot(spot)
    s = pr.summarize(probe(bot("t", THRESHOLD), target, n=2)["samples"], target)
    assert s["counts"] == {"bet": 2}
    assert s["sizes"] == [{"to": 550, "count": 2, "bb": 5.5, "pot": 1.0}]
    # a too-small raise is reported as the min raise the engine would apply
    junk = pr.summarize(probe(bot("j", JUNK), target, n=1)["samples"], target)
    assert junk["sizes"][0]["to"] == 100


def test_probe_spot_must_have_someone_to_act():
    with pytest.raises(pr.ProbeError, match="over"):
        pr.target_from_spot(Spot.from_dict({"players": 2, "actions": "pre: f"}))


def test_probe_errors_are_reported(bot):
    target = pr.target_from_spot(Spot.from_dict(FACING_600))
    res = probe(bot("slow", "import time\ndef decide(s):\n    time.sleep(2)\n"), target, n=1,
                timeout=0.2)
    assert res["samples"][0]["error"] == "timeout"
    assert res["samples"][0]["kind"] == "fold"


@pytest.fixture
def adaptive_match(bot, env):
    paths = {"adaptive": bot("adaptive", ADAPTIVE), "t": bot("t", THRESHOLD)}
    asyncio.run(MatchRunner("m", make_bot_seats(paths), MatchConfig(n_hands=12, seed=3),
                            writer=RunWriter("m", env / "runs")).run())
    run = Run.open("m", env / "runs")
    # a late decision by the adaptive bot
    rec = [d for d in run.decisions("adaptive")][-1]
    return run, rec, paths


def test_from_match_sends_exactly_what_the_bot_saw(adaptive_match):
    run, rec, paths = adaptive_match
    actions = [e for e in run.hand_events(rec["hand_num"]) if e["type"] == "action"]
    at = next(i for i, e in enumerate(actions) if e["decision_id"] == rec["decision_id"])
    target = pr.target_from_run(run, rec["hand_num"], at=at)
    sent = dict(target.state)
    assert len(sent.pop("match_action_log")) == rec["state"]["match_action_log_len"]
    assert sent == {k: v for k, v in rec["state"].items() if k != "match_action_log_len"}
    # the rebuilt match log matches what the bot logged in the match
    res = probe(paths["adaptive"], target, n=1)
    logged, probed = rec["logs"][0]["data"], res["samples"][0]["logs"][0]["data"]
    assert (probed["log_len"], probed["last"]) == (logged["log_len"], logged["last"])
    assert probed["n"] == 1                     # cold: the bot remembers nothing


def test_warm_restores_what_the_bot_knew(adaptive_match):
    run, rec, paths = adaptive_match
    actions = [e for e in run.hand_events(rec["hand_num"]) if e["type"] == "action"]
    at = next(i for i, e in enumerate(actions) if e["decision_id"] == rec["decision_id"])
    target = pr.target_from_run(run, rec["hand_num"], at=at, warm=True)
    res = probe(paths["adaptive"], target, n=1)
    assert res["warm"]["states"] == rec["logs"][0]["data"]["n"] - 1
    assert res["samples"][0]["logs"][0]["data"]["n"] == rec["logs"][0]["data"]["n"]


def test_fresh_process_per_sample(bot):
    target = pr.target_from_spot(Spot.from_dict(FACING_600))
    path = bot("a", ADAPTIVE)
    same = [s["logs"][0]["data"]["n"] for s in probe(path, target, n=3)["samples"]]
    fresh = [s["logs"][0]["data"]["n"] for s in probe(path, target, n=3, fresh=True)["samples"]]
    assert same == [1, 2, 3] and fresh == [1, 1, 1]


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------

def test_sweep_finds_the_flip(bot):
    spot = Spot.from_dict(FACING_600)
    variants = pr.expand_vary(spot, ["bet=100..1100:200"])
    assert [l for l, _ in variants] == ["bet 100", "bet 300", "bet 500", "bet 700", "bet 900", "bet 1100"]
    rows = asyncio.run(pr.run_sweep(bot("t", THRESHOLD), variants, 2))
    modal = [r["summary"]["modal"] for r in rows]
    assert modal == ["call", "call", "call", "fold", "fold", "fold"]


def test_sweep_ranges_group_by_hand_class_and_skip_conflicts(bot):
    spot = Spot.from_dict(FACING_600)
    variants = pr.expand_vary(spot, ["cards.BTN=AKs,QQ"])
    rows = pr.group_rows(asyncio.run(pr.run_sweep(bot("t", THRESHOLD), variants, 1)))
    by = {r["label"]: r for r in rows}
    assert by["AKs"]["variants"] == 4
    # QdQc is the BB's hand, so combos using those cards are invalid
    assert by["QQ"]["variants"] == 6 and by["QQ"]["invalid"]
    assert len(by["QQ"]["samples"]) == 6 - len(by["QQ"]["invalid"])


def test_sweep_board_and_stack_axes():
    spot = Spot.from_dict(FACING_600)
    turns = pr.expand_vary(spot, ["turn=*"])
    assert len(turns) == 52 and turns[0][1].board[3] == "2s"
    combo = pr.expand_vary(spot, ["stack.BB=1000,5000", "bet=300,600"])
    assert [l for l, _ in combo] == ["s5 stack 1000 · bet 300", "s5 stack 1000 · bet 600",
                                     "s5 stack 5000 · bet 300", "s5 stack 5000 · bet 600"]


@pytest.mark.parametrize("vary, msg", [
    (["bet=1..2"], None), (["color=red"], "can't vary"), (["nonsense"], "FIELD=VALUES"),
    (["turn=*", "river=*"], "max-variants"), (["bet=9..1"], "backwards"),
])
def test_expand_vary_errors(vary, msg):
    spot = Spot.from_dict(FACING_600)
    if msg is None:
        assert len(pr.expand_vary(spot, vary)) == 2
        return
    with pytest.raises(pr.ProbeError, match=msg):
        pr.expand_vary(spot, vary, max_variants=300)


def test_bet_axis_needs_a_raise():
    with pytest.raises(pr.ProbeError, match="no raise"):
        pr.expand_vary(Spot.from_dict({"players": 2, "actions": "pre: c"}), ["bet=1,2"])
