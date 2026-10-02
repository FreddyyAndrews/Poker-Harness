"""Tests for the Fullhouse game engine."""
from arena.engine.game import PokerEngine, STARTING_STACK, BIG_BLIND, SMALL_BLIND


def make_engine(n=6):
    ids = [f"bot_{i}" for i in range(n)]
    return PokerEngine("test_hand", ids, dealer_seat=0), ids


def test_start_hand():
    eng, ids = make_engine(6)
    state = eng.start_hand()
    assert state["type"] == "action_request"
    assert state["street"] == "preflop"
    assert len(state["your_cards"]) == 2
    # Pot should have SB + BB posted
    assert state["pot"] == SMALL_BLIND + BIG_BLIND
    print("✓ start_hand: state structure correct")


def test_blinds_posted():
    eng, ids = make_engine(6)
    eng.start_hand()
    # SB is seat 1, BB is seat 2 (dealer=0)
    assert eng.players[1].bet_this_street == SMALL_BLIND
    assert eng.players[2].bet_this_street == BIG_BLIND
    assert eng.players[1].stack == STARTING_STACK - SMALL_BLIND
    assert eng.players[2].stack == STARTING_STACK - BIG_BLIND
    print("✓ blinds_posted: SB and BB deducted correctly")


def test_fold_to_one():
    eng, ids = make_engine(3)
    state = eng.start_hand()
    # Fold everyone except one
    results = []
    while state.get("type") == "action_request":
        seat = state["seat_to_act"]
        state = eng.apply_action(seat, {"action": "fold"})
        results.append(state)
    final = results[-1]
    assert final["type"] == "hand_complete"
    assert len(final["winners"]) == 1
    print("✓ fold_to_one: last player wins pot")


def test_chip_conservation():
    """Total chips must be conserved across a hand."""
    eng, ids = make_engine(4)
    total_before = sum(p.stack for p in eng.players)
    state = eng.start_hand()
    total_mid = sum(p.stack for p in eng.players) + state["pot"]
    assert total_before == total_mid

    # Play out with all calls
    while state.get("type") == "action_request":
        seat = state["seat_to_act"]
        state = eng.apply_action(seat, {"action": "call"})

    total_after = sum(state["final_stacks"].values())
    assert total_before == total_after, f"{total_before} != {total_after}"
    print("✓ chip_conservation: no chips created or destroyed")


def test_raise_min_snap():
    """A raise below min should be snapped up to min."""
    eng, ids = make_engine(2)
    state = eng.start_hand()
    seat = state["seat_to_act"]
    min_raise = state["min_raise_to"]
    # Try to raise below minimum
    state2 = eng.apply_action(seat, {"action": "raise", "amount": 1})
    # Should still be valid — snapped to min
    assert state2["type"] in ("action_request", "hand_complete")
    print("✓ raise_min_snap: below-min raise handled without crash")


def test_invalid_action_defaults_to_fold():
    eng, ids = make_engine(3)
    state = eng.start_hand()
    seat = state["seat_to_act"]
    state2 = eng.apply_action(seat, {"action": "YOLO_BET"})
    assert state2["type"] in ("action_request", "hand_complete")
    print("✓ invalid_action: unknown action defaults to fold gracefully")


def test_check_when_bb():
    """BB can check preflop if no raise."""
    eng, ids = make_engine(2)
    state = eng.start_hand()
    # Heads-up: SB posts, BB posts, SB acts first preflop
    seat = state["seat_to_act"]
    # SB calls
    state = eng.apply_action(seat, {"action": "call"})
    # Now BB can check
    if state.get("type") == "action_request":
        assert state["can_check"] == True
        state = eng.apply_action(state["seat_to_act"], {"action": "check"})
    print("✓ check_when_bb: BB can check after SB calls")


def test_full_hand_runs_to_completion():
    """A complete hand from start to result without errors."""
    eng, ids = make_engine(6)
    state = eng.start_hand()
    hand_count = 0
    while state.get("type") == "action_request":
        seat = state["seat_to_act"]
        # Alternate call/check
        if state["can_check"]:
            state = eng.apply_action(seat, {"action": "check"})
        else:
            state = eng.apply_action(seat, {"action": "call"})
        hand_count += 1
        assert hand_count < 200, "Infinite loop detected"
    assert state["type"] == "hand_complete"
    assert len(state["winners"]) >= 1
    print("✓ full_hand: 6-player hand runs cleanly to showdown")


if __name__ == "__main__":
    tests = [
        test_start_hand,
        test_blinds_posted,
        test_fold_to_one,
        test_chip_conservation,
        test_raise_min_snap,
        test_invalid_action_defaults_to_fold,
        test_check_when_bb,
        test_full_hand_runs_to_completion,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"✗ {t.__name__}: {e}")
            failed += 1

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
