"""
Replay hands from a run's events.

Each hand_start event records the full deal (every seat's hole cards and
the board plan), and each action event records the action the engine
applied. So any hand can be rebuilt exactly by dealing the same cards and
replaying the actions in strict mode, and any point in a hand can become
a spot for the CLI.
"""

from typing import Optional

from poker_harness.engine.game import PokerEngine
from poker_harness.spot import ActionSpec, Spot


class ReplayError(Exception):
    pass


def _split(hand_events: list):
    starts = [e for e in hand_events if e["type"] == "hand_start"]
    if len(starts) != 1:
        raise ReplayError(f"expected one hand_start, found {len(starts)}")
    actions = [e for e in hand_events if e["type"] == "action"]
    end = next((e for e in hand_events if e["type"] == "hand_end"), None)
    return starts[0], actions, end


def _raw(ev: dict) -> dict:
    if ev["action"] == "raise":
        return {"action": "raise", "amount": ev["amount"]}
    return {"action": ev["action"]}


def replay_hand(hand_events: list, upto: Optional[int] = None):
    """Rebuild a hand, applying the first `upto` actions (all by default).
    Returns (engine, state)."""
    hs, actions, _ = _split(hand_events)
    engine = PokerEngine(
        hs["hand_id"], hs["bot_ids"],
        dealer_seat     = hs["dealer_seat"],
        starting_stacks = dict(zip(hs["bot_ids"], hs["stacks"])),
        small_blind     = hs["blinds"][0],
        big_blind       = hs["blinds"][1],
        hand_num        = hs["hand_num"],
        hole_cards      = {int(s): c for s, c in hs["hole_cards"].items()},
        board           = hs["board_plan"],
    )
    state = engine.start_hand()
    for i, ev in enumerate(actions[:upto]):
        if state["type"] != "action_request":
            raise ReplayError(f"action {i}: hand already over")
        if state["seat_to_act"] != ev["seat"]:
            raise ReplayError(f"action {i}: seat {ev['seat']} acted but seat "
                              f"{state['seat_to_act']} was to act")
        state = engine.apply_action(ev["seat"], _raw(ev), strict=True)
    return engine, state


def verify_hand(hand_events: list) -> list:
    """Replay a whole hand; return a list of differences from the record."""
    _, actions, end = _split(hand_events)
    try:
        engine, state = replay_hand(hand_events)
    except Exception as e:
        return [f"replay failed: {e}"]
    problems = []
    if state["type"] != "hand_complete":
        problems.append("replayed hand didn't finish")
        return problems
    if end is None:
        return ["no hand_end event"]
    for key in ("final_stacks", "winners", "uncalled", "board", "showdown"):
        got = state["community_cards"] if key == "board" else state[key]
        if got != end[key]:
            problems.append(f"{key}: recorded {end[key]}, replayed {got}")
    return problems


def verify_run(run) -> dict:
    """{hand_num: [problems]} for every hand that doesn't replay exactly."""
    hands, bad = {}, {}
    for ev in run.events():
        if ev.get("hand_num") is not None:
            hands.setdefault(ev["hand_num"], []).append(ev)
    for num, evs in hands.items():
        problems = verify_hand(evs)
        if problems:
            bad[num] = problems
    return bad


def hand_to_spot(hand_events: list, upto: Optional[int] = None) -> Spot:
    """The hand as a spot (god view: all cards fixed), stopped after the
    first `upto` actions, or the whole hand."""
    hs, actions, _ = _split(hand_events)
    names = hs["bot_ids"]
    spot = Spot(
        players = len(names),
        button  = hs["dealer_seat"],
        stacks  = list(hs["stacks"]),
        blinds  = tuple(hs["blinds"]),
        cards   = {int(s): c for s, c in hs["hole_cards"].items()},
        board   = list(hs["board_plan"]),
        actions = [ActionSpec(ev["street"], ev["action"],
                              ev["amount"] if ev["action"] == "raise" else None, ev["seat"])
                   for ev in actions[:upto]],
    )
    spot.description = (f"from match {hs['match_id']} hand {hs['hand_num']}"
                        + (f" after {upto} action(s)" if upto is not None else "")
                        + " (seats: " + ", ".join(f"s{i}={n}" for i, n in enumerate(names)) + ")")
    return spot
