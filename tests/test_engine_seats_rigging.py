"""Phase 1a engine behaviour: fixed seats, blinds, legal_actions, strict
mode, rigged deals, run-outs. Plus fuzzers for chip conservation."""
import random

import eval7
import pytest

from arena.engine.game import IllegalActionError, PokerEngine, next_button


def make(stacks, dealer=0, **kw):
    ids = [f"s{i}" for i in range(len(stacks))]
    return PokerEngine("t", ids, dealer_seat=dealer,
                       starting_stacks=dict(zip(ids, stacks)), **kw)


def play_out(eng, state, pick):
    """Drive a hand to completion; pick(state) -> raw action. Returns
    (result, seats asked to act)."""
    asked = []
    while state["type"] == "action_request":
        asked.append(state["seat_to_act"])
        state = eng.apply_action(state["seat_to_act"], pick(state))
        assert len(asked) < 500
    return state, asked


def check_or_call(state):
    return {"action": "check" if state["can_check"] else "call"}


# ---------------------------------------------------------------------------
# Fixed seats
# ---------------------------------------------------------------------------

def test_next_button_skips_busted_and_wraps():
    assert next_button([100, 100, 100]) == 0
    assert next_button([0, 100, 100]) == 1
    assert next_button([100, 0, 100], prev=0) == 2
    assert next_button([100, 100, 0], prev=1) == 0
    with pytest.raises(ValueError):
        next_button([0, 0])


def test_busted_seat_sits_out():
    eng = make([10_000, 0, 10_000, 10_000], dealer=0)
    state = eng.start_hand()
    assert eng.players[1].hole_cards == []
    assert eng.players[1].state == "busted"
    # blinds skip the busted seat: SB=2, BB=3, UTG=0
    assert eng.players[2].bet_this_street == 50
    assert eng.players[3].bet_this_street == 100
    assert state["seat_to_act"] == 0
    result, asked = play_out(eng, state, check_or_call)
    assert 1 not in asked
    assert result["final_stacks"]["s1"] == 0


def test_heads_up_rules_on_larger_table():
    eng = make([0, 0, 10_000, 0, 0, 10_000], dealer=5)
    state = eng.start_hand()
    assert eng.players[5].bet_this_street == 50    # dealer is SB
    assert eng.players[2].bet_this_street == 100
    assert state["seat_to_act"] == 5               # SB first preflop
    state = eng.apply_action(5, {"action": "call"})
    state = eng.apply_action(2, {"action": "check"})
    assert state["street"] == "flop"
    assert state["seat_to_act"] == 2               # BB first postflop


def test_invalid_table_setup():
    with pytest.raises(ValueError, match="Dealer seat"):
        make([0, 100, 100], dealer=0)
    with pytest.raises(ValueError, match="at least 2"):
        make([0, 100, 0])
    with pytest.raises(AssertionError, match="Duplicate"):
        PokerEngine("t", ["a", "a"])


# ---------------------------------------------------------------------------
# Blinds, state fields, legal_actions
# ---------------------------------------------------------------------------

def test_custom_blinds_and_state_fields():
    eng = make([5000] * 3, dealer=1, small_blind=25, big_blind=50, hand_num=7)
    state = eng.start_hand()
    assert state["pot"] == 75
    assert state["min_raise_to"] == 100
    assert state["dealer_seat"] == 1
    assert state["hand_num"] == 7
    assert (state["small_blind"], state["big_blind"]) == (25, 50)
    assert state["legal_actions"] == {
        "can_fold": True, "can_check": False, "call_amount": 50,
        "can_raise": True, "min_raise_to": 100, "max_raise_to": 5000,
    }


def test_legal_actions_short_stack_only_all_in_raise():
    # UTG (seat 0) has 150 facing the 100 BB: can only raise all-in to 150
    eng = make([150, 10_000, 10_000], dealer=0)
    state = eng.start_hand()
    assert state["seat_to_act"] == 0
    legal = state["legal_actions"]
    assert legal["can_raise"] and legal["min_raise_to"] == legal["max_raise_to"] == 150


def test_cannot_raise_when_every_opponent_is_all_in():
    eng = make([10_000, 10_000], dealer=0)
    eng.start_hand()
    state = eng.apply_action(0, {"action": "all_in"})
    legal = state["legal_actions"]
    assert legal["can_raise"] is False
    assert legal["call_amount"] == 9_900
    # lenient: raise becomes a call
    result = eng.apply_action(1, {"action": "raise", "amount": 20_000})
    assert result["type"] == "hand_complete"
    assert eng.action_log[-1]["action"] == "call"


# ---------------------------------------------------------------------------
# Strict mode and turn order
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, msg", [
    ({"action": "check"}, "owes"),
    ({"action": "raise", "amount": 150}, "outside"),
    ({"action": "raise", "amount": 20_000}, "outside"),
    ({"action": "raise", "amount": "lots"}, "integer"),
    ({"action": "yolo"}, "unknown"),
])
def test_strict_rejects_illegal(raw, msg):
    eng = make([10_000] * 3, dealer=0)
    state = eng.start_hand()
    with pytest.raises(IllegalActionError, match=msg):
        eng.apply_action(state["seat_to_act"], raw, strict=True)


def test_strict_rejects_call_with_nothing_owed():
    eng = make([10_000] * 2, dealer=0)
    eng.start_hand()
    eng.apply_action(0, {"action": "call"}, strict=True)
    with pytest.raises(IllegalActionError, match="use check"):
        eng.apply_action(1, {"action": "call"}, strict=True)


def test_strict_raise_to_max_is_all_in():
    eng = make([10_000] * 2, dealer=0)
    eng.start_hand()
    eng.apply_action(0, {"action": "raise", "amount": 10_000}, strict=True)
    assert eng.action_log[-1] == {"seat": 0, "action": "all_in", "amount": 10_000}


def test_out_of_turn_and_after_complete():
    eng = make([10_000] * 3, dealer=0)
    state = eng.start_hand()
    wrong = (state["seat_to_act"] + 1) % 3
    with pytest.raises(IllegalActionError, match="out of turn"):
        eng.apply_action(wrong, {"action": "fold"})
    result, _ = play_out(eng, state, lambda s: {"action": "fold"})
    with pytest.raises(IllegalActionError, match="complete"):
        eng.apply_action(0, {"action": "fold"})


# ---------------------------------------------------------------------------
# Rigged deals
# ---------------------------------------------------------------------------

def test_rigged_hole_cards_and_board():
    eng = make([10_000] * 2, dealer=0,
               hole_cards={0: ["As", "Ah"], 1: ["Ks", "Kh"]},
               board=["2c", "7d", "9s", "Jc", "3h"])
    assert eng.rigged
    state = eng.start_hand()
    assert state["your_cards"] == ["As", "Ah"]
    result, _ = play_out(eng, state, check_or_call)
    assert result["community_cards"] == ["2c", "7d", "9s", "Jc", "3h"]
    assert result["revealed_cards"] == {"s0": ["As", "Ah"], "s1": ["Ks", "Kh"]}
    assert result["winners"][0]["seat"] == 0


def test_partial_board_and_seeded_fill():
    kw = dict(hole_cards={1: ["Qd", "Qc"]}, board=["Kd", None, "2s"], seed=42)
    results = []
    for _ in range(2):
        eng = make([10_000] * 3, dealer=0, **kw)
        state = eng.start_hand()
        result, _ = play_out(eng, state, check_or_call)
        results.append(result)
    board = results[0]["community_cards"]
    assert board[0] == "Kd" and board[2] == "2s"
    assert results[0]["revealed_cards"]["s1"] == ["Qd", "Qc"]
    assert results[0]["community_cards"] == results[1]["community_cards"]
    assert results[0]["revealed_cards"] == results[1]["revealed_cards"]


def test_unrigged_seeded_deal_matches_upstream_order():
    # upstream: shuffle a fresh deck with Random(seed), deal 2 per seat in
    # seat order, then the board
    deck = [eval7.Card(r + s) for r in "23456789TJQKA" for s in "shdc"]
    random.Random(99).shuffle(deck)
    eng = make([10_000] * 3, dealer=0, seed=99)
    eng.start_hand()
    assert [str(c) for c in eng.players[0].hole_cards] == [str(c) for c in deck[0:2]]
    assert [str(c) for c in eng.players[2].hole_cards] == [str(c) for c in deck[4:6]]
    assert [str(c) for c in eng._board_plan] == [str(c) for c in deck[6:11]]
    assert not eng.rigged


@pytest.mark.parametrize("kw, msg", [
    (dict(hole_cards={0: ["As", "Ah"]}, board=["As"]), "Duplicate"),
    (dict(hole_cards={0: ["As", "Zz"]}), "Bad card"),
    (dict(hole_cards={0: ["As"]}), "exactly 2"),
    (dict(hole_cards={5: ["As", "Ah"]}), "No seat"),
    (dict(board=["2c"] * 6), "max is 5"),
])
def test_rigging_errors(kw, msg):
    with pytest.raises(ValueError, match=msg):
        make([10_000] * 2, **kw)


def test_cannot_rig_sitting_out_seat():
    with pytest.raises(ValueError, match="sitting out"):
        make([10_000, 0, 10_000], hole_cards={1: ["As", "Ah"]})


# ---------------------------------------------------------------------------
# Run-outs: nobody is asked to act when no betting is possible
# ---------------------------------------------------------------------------

def test_all_in_preflop_runs_out_with_street_events():
    eng = make([10_000] * 2, dealer=0)
    eng.start_hand()
    eng.apply_action(0, {"action": "all_in"})
    result = eng.apply_action(1, {"action": "call"})
    assert result["type"] == "hand_complete"
    assert len(result["community_cards"]) == 5
    streets = [e["street"] for e in result["events"] if e["type"] == "street_start"]
    assert streets == ["preflop", "flop", "turn", "river"]


def test_lone_active_player_owing_nothing_is_not_asked():
    # UTG (seat 0) all-in for exactly the BB; SB folds; BB owes nothing and
    # nobody could respond to a raise, so the board runs out.
    eng = make([100, 10_000, 10_000], dealer=0)
    state = eng.start_hand()
    assert state["seat_to_act"] == 0
    state = eng.apply_action(0, {"action": "all_in"})
    assert state["seat_to_act"] == 1
    result = eng.apply_action(1, {"action": "fold"})
    assert result["type"] == "hand_complete"


def test_short_blind_all_in_runs_out_immediately():
    eng = make([30, 10_000], dealer=0)     # dealer=SB posts 30, all-in
    result = eng.start_hand()
    assert result["type"] == "hand_complete"
    assert sum(result["final_stacks"].values()) == 10_030


# ---------------------------------------------------------------------------
# Fuzzers (the engine asserts chip conservation at the end of every hand)
# ---------------------------------------------------------------------------

def _random_table(rng):
    n = rng.randint(2, 9)
    while True:
        stacks = [rng.choice([0, 0, 1, 30, 75, 100, 150, 500, 2_000, 10_000, 25_000])
                  for _ in range(n)]
        if sum(1 for s in stacks if s > 0) >= 2:
            break
    dealer = next_button(stacks, rng.randrange(n))
    return make(stacks, dealer=dealer, seed=rng.randrange(10**9))


def _sample_legal(rng, state):
    legal = state["legal_actions"]
    options = [{"action": "fold"}]
    if legal["can_check"]:
        options.append({"action": "check"})
    else:
        options.append({"action": "call"})
    if legal["can_raise"]:
        lo, hi = legal["min_raise_to"], legal["max_raise_to"]
        options.append({"action": "raise", "amount": rng.randint(lo, hi)})
        options.append({"action": "all_in"})
    return rng.choice(options)


def _check_request(eng, state):
    p = eng.players[state["seat_to_act"]]
    assert p.is_active, f"asked {p.state} seat {p.seat} to act"
    assert not p.sitting_out


def test_fuzz_strict_legal_actions_are_accepted():
    rng = random.Random(1)
    for _ in range(2_000):
        eng = _random_table(rng)
        state = eng.start_hand()
        while state["type"] == "action_request":
            _check_request(eng, state)
            state = eng.apply_action(state["seat_to_act"],
                                     _sample_legal(rng, state), strict=True)
        total = sum(p.stack for p in eng.players)
        assert total == sum(eng._starting_stacks.values())


def test_fuzz_lenient_junk_actions():
    rng = random.Random(2)
    junk = [{"action": "raise", "amount": -5}, {"action": "raise", "amount": 10**9},
            {"action": "check"}, {"action": "call"}, {"action": "all_in"},
            {"action": "fold"}, {"action": "nonsense"}, {}, None,
            {"action": "raise", "amount": "x"}]
    for _ in range(2_000):
        eng = _random_table(rng)
        state = eng.start_hand()
        while state["type"] == "action_request":
            _check_request(eng, state)
            state = eng.apply_action(state["seat_to_act"], rng.choice(junk))


# ---------------------------------------------------------------------------
# Uncalled bets
# ---------------------------------------------------------------------------

def test_uncalled_raise_is_returned_not_won():
    eng = make([10_000] * 3, dealer=0)
    eng.start_hand()
    eng.apply_action(0, {"action": "raise", "amount": 250})
    eng.apply_action(1, {"action": "fold"})
    result = eng.apply_action(2, {"action": "fold"})
    assert result["uncalled"] == {"seat": 0, "bot_id": "s0", "amount": 150}
    assert result["winners"] == [{"bot_id": "s0", "seat": 0, "amount": 250, "pot_type": "main"}]
    assert result["pot"] == 250
    assert result["final_stacks"]["s0"] == 10_150
    types = [e["type"] for e in result["events"]]
    assert types.index("uncalled_bet_returned") < types.index("uncontested_win")


def test_uncalled_excess_over_short_all_in_is_returned():
    eng = make([30, 10_000], dealer=0)       # SB all-in for 30 posting
    result = eng.start_hand()
    assert result["uncalled"] == {"seat": 1, "bot_id": "s1", "amount": 70}
    assert [w["pot_type"] for w in result["winners"]] == ["main"]
    assert sum(w["amount"] for w in result["winners"]) == 60


def test_fully_called_pot_returns_nothing():
    eng = make([10_000] * 2, dealer=0)
    eng.start_hand()
    eng.apply_action(0, {"action": "call"})
    result = eng.apply_action(1, {"action": "check"})
    while result["type"] == "action_request":
        result = eng.apply_action(result["seat_to_act"], {"action": "check"})
    assert result["uncalled"] is None
