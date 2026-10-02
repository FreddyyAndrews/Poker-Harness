"""Spot notation, replay into the engine, and serialisation."""
import random

import pytest

from poker_harness.engine.game import next_button, seat_positions
from poker_harness.spot import (
    Spot, SpotError, parse_actions, parse_board, parse_stacks,
)


def spot(**kw):
    kw.setdefault("players", 6)
    kw.setdefault("button", 3)
    return Spot.from_dict(kw)


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def test_positions_by_table_size():
    assert seat_positions([1] * 6, 3) == {3: "BTN", 4: "SB", 5: "BB", 0: "UTG", 1: "HJ", 2: "CO"}
    assert list(seat_positions([1] * 9, 0).values()) == [
        "BTN", "SB", "BB", "UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO"]
    assert seat_positions([1, 1, 1], 0) == {0: "BTN", 1: "SB", 2: "BB"}
    # heads-up, busted seats skipped
    assert seat_positions([1, 0, 1], 2) == {2: "BTN", 0: "BB"}


def test_position_and_seat_tokens_resolve():
    s = spot(cards="BTN=AsKh BB=QdQc s0=2c2d")
    assert s.cards == {3: ["As", "Kh"], 5: ["Qd", "Qc"], 0: ["2c", "2d"]}
    hu = Spot.from_dict({"players": 2, "button": 1, "cards": "SB=AsAh"})
    assert hu.cards == {1: ["As", "Ah"]}         # heads-up SB is the button


# ---------------------------------------------------------------------------
# Notation parsers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, board", [
    ("Kd7c2s|9h|", ["Kd", "7c", "2s", "9h", None]),
    ("Kd7c2s9h", ["Kd", "7c", "2s", "9h", None]),
    ("Kd??2s||3c", ["Kd", None, "2s", None, "3c"]),
    ("kd7C2S", ["Kd", "7c", "2s", None, None]),
    ("", [None] * 5),
])
def test_parse_board(text, board):
    assert parse_board(text) == board


@pytest.mark.parametrize("text", ["Kd7c2s5h|9h", "Kd7c|9h8h|", "Kd7x", "Kd7c2s9h3c4c"])
def test_parse_board_errors(text):
    with pytest.raises(SpotError):
        parse_board(text)


def test_parse_stacks():
    assert parse_stacks("5000", 3) == [5000] * 3
    assert parse_stacks("100,200,0", 3) == [100, 200, 0]
    assert parse_stacks("5000 s1=250", 3) == [5000, 250, 5000]
    with pytest.raises(SpotError):
        parse_stacks("1,2", 3)


def test_parse_actions_labels_and_defaults():
    s = spot()
    acts = parse_actions("pre: BTN r250, BB c; flop: BB x, BTN b300", s)
    assert [(a.street, a.seat, a.action, a.amount) for a in acts] == [
        ("preflop", 3, "raise", 250, ), ("preflop", 5, "call", None),
        ("flop", 5, "check", None), ("flop", 3, "raise", 300)]
    # unlabeled sections go in street order; seat optional
    acts = parse_actions("c, c, c, c, c, x; x, x", s)
    assert [a.street for a in acts] == ["preflop"] * 6 + ["flop"] * 2


@pytest.mark.parametrize("text", ["pre: BTN zz", "flop: x; pre: x", "pre: r", "pre: s9 c"])
def test_parse_actions_errors(text):
    with pytest.raises(SpotError):
        parse_actions(text, spot())


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def test_replay_reaches_the_described_position():
    s = spot(cards="BTN=AsKh BB=QdQc", board="Kd7c2s|9h|",
             actions="pre: BTN r250, BB r900, BTN c; flop: BB r600", to_act="BTN", seed=1)
    eng, state = s.to_engine()
    assert state["street"] == "flop"
    assert state["seat_to_act"] == 3
    assert state["your_cards"] == ["As", "Kh"]
    assert state["community_cards"] == ["Kd", "7c", "2s"]
    assert state["amount_owed"] == 600
    assert eng.board_plan[3] == "9h"
    # skipped seats folded
    assert [p.state for p in eng.players] == ["folded"] * 3 + ["active", "folded", "active"]


def test_skip_rule_checks_when_it_can():
    s = spot(actions="pre: BTN c, BB x; flop: BTN r200")   # SB folds, BB checks
    eng, state = s.to_engine()
    flop = [e for e in eng.events if e["type"] == "action" and e["street"] == "flop"]
    assert [(e["seat"], e["action"]) for e in flop] == [(5, "check"), (3, "raise")]


@pytest.mark.parametrize("kw, msg", [
    (dict(actions="pre: BTN r250, CO c"), "can't act now"),
    (dict(actions="pre: x"), "can't check"),
    (dict(actions="pre: r150"), "outside"),
    (dict(actions="pre: c, c, c, c, c, x; turn: x"), "on the flop, not the turn"),
    (dict(actions="pre: BTN r250", to_act="BB"), "spot says s5 \\(BB\\) to act, but s4 \\(SB\\) is"),
    (dict(actions="pre: f, f, f, f, f, x"), "already over"),
    (dict(cards="BTN=AsKh BB=AsQc"), "Duplicate"),
])
def test_replay_errors(kw, msg):
    with pytest.raises(SpotError, match=msg):
        spot(**kw).to_engine()


def test_strict_replay_does_not_correct_actions():
    with pytest.raises(SpotError):
        spot(actions="pre: r150").to_engine(strict=True)
    eng, _ = spot(actions="pre: r150").to_engine(strict=False)
    assert eng.action_log[-1]["amount"] == 200


# ---------------------------------------------------------------------------
# Serialisation and compaction
# ---------------------------------------------------------------------------

def test_dict_round_trip_and_files(tmp_path):
    s = spot(cards="BTN=AsKh", board="Kd7c2s", stacks="10000 s2=500", blinds="25/50",
             actions="pre: BTN r150, BB c", seed=3, name="x", description="d")
    assert Spot.from_dict(s.to_dict()).to_dict() == s.to_dict()
    for suffix in (".yaml", ".json"):
        path = tmp_path / ("spot" + suffix)
        s.save(path)
        assert Spot.load(path).to_dict() == s.to_dict()


def test_unknown_fields_rejected():
    with pytest.raises(SpotError, match="unknown"):
        Spot.from_dict({"players": 2, "stack": 100})


def test_compact_drops_only_implied_folds_and_round_trips():
    s = spot(actions="pre: s0 f, s1 f, s2 f, s3 r250, s4 f, s5 c; flop: s5 x, s3 x", seed=4)
    c = s.compact()
    assert c.to_dict()["actions"] == "pre: s3 r250, s5 c; flop: s5 x, s3 x"
    assert c.to_engine()[0].action_log == s.to_engine()[0].action_log


def test_fuzz_compact_and_expand_round_trip():
    """Random legal hands: the expanded and compacted action lines both
    replay to exactly the same hand."""
    rng = random.Random(5)
    for _ in range(300):
        n = rng.randint(2, 9)
        stacks = [rng.choice([0, 300, 2_000, 10_000]) for _ in range(n)]
        if sum(1 for x in stacks if x) < 2:
            continue
        base = Spot(players=n, button=next_button(stacks, rng.randrange(n)),
                    stacks=stacks, seed=rng.randrange(10**6))
        eng, state = base.to_engine()
        steps = rng.randint(0, 12)
        while state["type"] == "action_request" and steps:
            legal = state["legal_actions"]
            opts = [{"action": "fold"}, {"action": "check" if legal["can_check"] else "call"}]
            if legal["can_raise"]:
                opts.append({"action": "raise",
                             "amount": rng.randint(legal["min_raise_to"], legal["max_raise_to"])})
            state = eng.apply_action(state["seat_to_act"], rng.choice(opts), strict=True)
            steps -= 1
        full = base.with_actions_from(eng)
        compact = full.compact()
        assert full.to_engine()[0].action_log == eng.action_log
        assert compact.to_engine()[0].action_log == eng.action_log
        assert Spot.from_dict(compact.to_dict()).to_engine()[0].action_log == eng.action_log
