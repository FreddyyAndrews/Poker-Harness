"""
House bots for the mock arena: always online, always accept challenges,
and fill tables. Built-in policies run in-process; any bot.py can be
added as a house bot too (it runs in its own process like a local match).
"""

import random

from poker_harness.seats import CallbackSeat, SubprocessBotSeat

_STRONG = set("AKQJT")


def _check_or_call(state):
    return {"action": "check" if state["can_check"] else "call"}


def caller(state):
    """Never folds, never raises."""
    return _check_or_call(state)


def tight(state):
    """Raises good starting hands, calls small bets with pairs or an ace,
    otherwise check/fold."""
    cards = state["your_cards"]
    ranks = sorted((c[0] for c in cards), key="23456789TJQKA".index, reverse=True)
    pair = ranks[0] == ranks[1]
    premium = pair and ranks[0] in _STRONG or ranks == ["A", "K"]
    legal = state["legal_actions"]
    if state["street"] == "preflop" and premium and legal["can_raise"]:
        target = max(legal["min_raise_to"], 3 * state["big_blind"])
        return {"action": "raise", "amount": min(target, legal["max_raise_to"])}
    if state["can_check"]:
        return {"action": "check"}
    small = state["amount_owed"] <= state["pot"] * 0.35
    if (pair or "A" in ranks) and small:
        return {"action": "call"}
    return {"action": "fold"}


def aggro(state):
    """Bets or raises whenever it can open the betting; calls otherwise."""
    legal = state["legal_actions"]
    if state["can_check"] and legal["can_raise"]:
        size = max(legal["min_raise_to"], state["pot"] * 2 // 3)
        return {"action": "raise", "amount": min(size, legal["max_raise_to"])}
    return _check_or_call(state)


class _Random:
    """Random legal actions, from a fixed seed so it's reproducible."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)

    def __call__(self, state):
        legal = state["legal_actions"]
        options = [{"action": "fold"}, _check_or_call(state)]
        if legal["can_raise"]:
            options.append({"action": "raise",
                            "amount": self.rng.randint(legal["min_raise_to"], legal["max_raise_to"])})
        return self.rng.choice(options)


POLICIES = {
    "house-caller": lambda: caller,
    "house-tight": lambda: tight,
    "house-aggro": lambda: aggro,
    "house-random": lambda: _Random(7),
}


def make_house_seat(name: str, spec: str = None):
    """A seat for house bot `name`: a built-in policy, or a bot path."""
    if spec:
        return SubprocessBotSeat(name, spec, timeout=10.0)
    return CallbackSeat(name, POLICIES[name]())
