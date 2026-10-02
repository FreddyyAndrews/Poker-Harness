"""
No-Limit Texas Hold'em engine for one hand, 2-9 seats, using eval7.

Forked from the Fullhouse Hackathon engine v2.0. On top of upstream:
  - Fixed seats: a seat with no chips sits out (no cards, no blinds, never
    acts) instead of being removed, so seat numbers are stable for a match
  - Blinds are passed in per hand instead of being module constants
  - legal_actions() for the seat to act, also included in every state
  - Strict mode: illegal actions raise IllegalActionError instead of being
    corrected (for replaying spots and recorded hands)
  - Rigged deals: fix any hole cards and board cards; the rest of the deck
    is shuffled from the seed
  - Acting out of turn or after the hand is over raises an error
  - When no further betting is possible (everyone left is all-in, or one
    player who owes nothing), the board runs out without asking anyone
  - Every street dealt during a run-out gets a street_start event
  - Uncalled bets are returned to the bettor before the pot is awarded
    (an uncalled_bet_returned event), instead of being reported as a pot
    the bettor "won"
"""

import eval7
import random
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

SMALL_BLIND    = 50
BIG_BLIND      = 100
STARTING_STACK = 10_000
MAX_PLAYERS    = 9

ACTIONS = ("fold", "check", "call", "raise", "all_in")
STREETS = ("preflop", "flop", "turn", "river")


class IllegalActionError(ValueError):
    """An action that is not legal right now (strict mode), or out of turn."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Player:
    seat: int
    bot_id: str
    stack: int
    hole_cards: list = field(default_factory=list)
    is_folded: bool = False
    is_all_in: bool = False
    sitting_out: bool = False   # had no chips when the hand started
    bet_this_street: int = 0
    total_invested: int = 0     # cumulative chips put in this hand (for side pots)

    @property
    def in_hand(self) -> bool:
        """Still contesting the pot (may be all-in)."""
        return not self.is_folded and not self.sitting_out

    @property
    def is_active(self) -> bool:
        """Can still make betting decisions."""
        return self.in_hand and not self.is_all_in and self.stack > 0

    @property
    def state(self) -> str:
        if self.sitting_out:
            return "busted"
        if self.is_folded:
            return "folded"
        if self.is_all_in:
            return "all_in"
        return "active"

    def to_public_dict(self) -> dict:
        return {
            "seat": self.seat,
            "bot_id": self.bot_id,
            "stack": self.stack,
            "state": self.state,
            "is_folded": self.is_folded,
            "is_all_in": self.is_all_in,
            "bet_this_street": self.bet_this_street,
            "hole_cards": None,      # hidden until showdown
        }


@dataclass
class Action:
    seat: int
    action: str
    amount: int = 0

    def to_dict(self) -> dict:
        return {"seat": self.seat, "action": self.action, "amount": self.amount}


def next_button(stacks: list, prev: Optional[int] = None) -> int:
    """Button for the next hand: the first seat with chips after `prev`
    (or seat 0 onwards for the first hand)."""
    n = len(stacks)
    start = 0 if prev is None else prev + 1
    for offset in range(n):
        s = (start + offset) % n
        if stacks[s] > 0:
            return s
    raise ValueError("No seat has chips")


# Names for the seats between the big blind and the button, by how many
# there are (UTG acts first preflop, CO is just before the button).
_MIDDLE_POSITIONS = {
    0: [],
    1: ["UTG"],
    2: ["UTG", "CO"],
    3: ["UTG", "HJ", "CO"],
    4: ["UTG", "LJ", "HJ", "CO"],
    5: ["UTG", "UTG+1", "LJ", "HJ", "CO"],
    6: ["UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO"],
}


def seat_positions(stacks: list, button: int) -> dict:
    """{seat: position name} for seats with chips, e.g. BTN, SB, BB, UTG,
    HJ, CO. Heads-up the button is also the small blind and is named BTN."""
    n    = len(stacks)
    live = [(button + i) % n for i in range(n) if stacks[(button + i) % n] > 0]
    if button not in live:
        raise ValueError(f"Button seat {button} has no chips")
    if len(live) < 2:
        raise ValueError("Need at least 2 seats with chips")
    if len(live) == 2:
        return {live[0]: "BTN", live[1]: "BB"}
    names = ["BTN", "SB", "BB"] + _MIDDLE_POSITIONS[len(live) - 3]
    return dict(zip(live, names))


def lenient_action(raw, *, owed: int, can_raise: bool, min_raise_to: int,
                   stack: int, bet: int) -> tuple:
    """The lenient rules for turning any reply into a legal action, as
    (action, amount): unknown actions fold, check/call become whichever is
    legal, raises below the minimum go up to it, raises the player can't
    cover become all-in, and raises nobody could answer become a call.
    Shared by the engine and normalize_action, so they can't drift."""
    raw = raw if isinstance(raw, dict) else {}
    act = str(raw.get("action", "fold")).lower().strip()
    try:
        amount = int(raw.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0

    if act not in ACTIONS:
        return "fold", 0

    def check_or_call():
        return ("check", 0) if owed == 0 else ("call", owed)

    if act in ("check", "call"):
        return check_or_call()
    if not can_raise:
        # nobody left who could respond, or can't cover more than a call
        return check_or_call()
    if act == "raise":
        amount = max(amount, min_raise_to)
        if amount - bet >= stack:
            return "all_in", stack + bet
        return "raise", amount
    if act == "all_in":
        return "all_in", stack + bet
    return "fold", 0


def normalize_action(state: dict, raw) -> dict:
    """What the engine will do with `raw` in this action_request `state`
    (lenient rules), as {"action", "amount"}, without needing the engine."""
    act, amount = lenient_action(
        raw, owed=state["amount_owed"], can_raise=state["legal_actions"]["can_raise"],
        min_raise_to=state["min_raise_to"], stack=state["your_stack"],
        bet=state["your_bet_this_street"])
    return {"action": act, "amount": amount}


def _parse_card(text) -> eval7.Card:
    try:
        return eval7.Card(str(text))
    except Exception:
        raise ValueError(f"Bad card: {text!r}") from None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class PokerEngine:
    def __init__(
        self,
        hand_id: str,
        bot_ids: list,
        dealer_seat: int = 0,
        starting_stacks: Optional[dict] = None,
        seed: Optional[int] = None,
        small_blind: int = SMALL_BLIND,
        big_blind: int = BIG_BLIND,
        hand_num: int = 0,
        hole_cards: Optional[dict] = None,
        board: Optional[list] = None,
    ):
        """
        hole_cards: optional {seat: ["As", "Kh"]} to rig some seats' cards.
        board:      optional list of up to 5 board cards; None entries (or a
                    short list) leave those cards random.
        Unrigged cards are drawn from the rest of the deck, shuffled by `seed`.
        """
        assert 2 <= len(bot_ids) <= MAX_PLAYERS, \
            f"Need 2-{MAX_PLAYERS} seats, got {len(bot_ids)}"
        assert len(set(bot_ids)) == len(bot_ids), f"Duplicate bot_ids: {bot_ids}"
        assert 0 < small_blind <= big_blind, "Need 0 < small_blind <= big_blind"

        stacks = starting_stacks or {}
        self.players = [
            Player(seat=i, bot_id=bid, stack=stacks.get(bid, STARTING_STACK))
            for i, bid in enumerate(bot_ids)
        ]
        for p in self.players:
            p.sitting_out = p.stack <= 0

        self.n           = len(self.players)
        self._live       = [p.seat for p in self.players if not p.sitting_out]
        if len(self._live) < 2:
            raise ValueError("Need at least 2 seats with chips")

        self.dealer_seat = dealer_seat % self.n
        if self.players[self.dealer_seat].sitting_out:
            raise ValueError(f"Dealer seat {self.dealer_seat} has no chips")

        self.hand_id     = hand_id
        self.hand_num    = hand_num
        self.seed        = seed
        self.small_blind = small_blind
        self.big_blind   = big_blind

        self.pot             = 0
        self.community_cards = []          # list of eval7.Card
        self.street          = "preflop"
        self.action_log      = []          # flat dicts (backwards-compat for bots)
        self.events          = []          # rich event log for replay
        self.current_bet     = 0
        self.min_raise       = big_blind

        # Short all-in: only reopen action if raise >= last full raise size
        self._last_aggression_size = big_blind

        self._needs_to_act    = set()
        self._starting_stacks = {}         # snapshot before hand starts
        self.to_act: Optional[int] = None  # seat we're waiting on
        self.is_complete      = False
        self.uncalled: Optional[dict] = None   # {seat, bot_id, amount} if returned

        self._hole_plan, self._board_plan = self._plan_deal(hole_cards, board)
        self.rigged = bool(hole_cards) or any(c is not None for c in (board or []))

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def start_hand(self) -> dict:
        self._snapshot_stacks()
        self._post_blinds()
        self._deal_hole_cards()

        # Everyone must act preflop, including BB (BB gets the option)
        self._needs_to_act = {p.seat for p in self.players if p.is_active}

        self._emit("street_start", {"street": "preflop", "community_cards": []})

        if not self._action_needed():
            self._needs_to_act.clear()
            return self._advance_street()

        utg = self._utg_seat()
        first = utg if utg in self._needs_to_act else self._next_actor(utg)
        return self._build_state(first)

    def apply_action(self, seat: int, raw: dict, strict: bool = False) -> dict:
        """Apply `raw` for `seat`. Lenient by default: illegal actions are
        corrected (raises snapped to legal sizes, unknown actions fold).
        With strict=True, illegal actions raise IllegalActionError."""
        if self.is_complete:
            raise IllegalActionError(f"[{self.hand_id}] Hand is already complete")
        if seat != self.to_act:
            raise IllegalActionError(
                f"[{self.hand_id}] Seat {seat} acted out of turn; waiting on seat {self.to_act}")

        action = self._validate_strict(seat, raw) if strict else self._validate(seat, raw)
        self.action_log.append(action.to_dict())
        self._needs_to_act.discard(seat)
        p = self.players[seat]

        if action.action == "fold":
            p.is_folded = True
            self._emit_action(seat, "fold", 0)

        elif action.action == "check":
            self._emit_action(seat, "check", 0)

        elif action.action == "call":
            owed = self.current_bet - p.bet_this_street
            paid = min(owed, p.stack)
            self._put_in(seat, paid)
            if p.stack == 0:
                p.is_all_in = True
            self._emit_action(seat, "call", paid)

        elif action.action == "raise":
            prev_bet   = self.current_bet
            chips_in   = action.amount - p.bet_this_street
            self._put_in(seat, min(chips_in, p.stack))
            if p.stack == 0:
                p.is_all_in = True
            raise_size = self.current_bet - prev_bet
            self._handle_aggression(seat, raise_size)
            self._emit_action(seat, "raise", action.amount)

        elif action.action == "all_in":
            prev_bet = self.current_bet
            self._put_in(seat, p.stack)
            p.is_all_in = True
            raise_size = self.current_bet - prev_bet
            self._handle_aggression(seat, raise_size)
            self._emit_action(seat, "all_in", p.bet_this_street)

        # Everyone folded except one?
        remaining = [pl for pl in self.players if pl.in_hand]
        if len(remaining) == 1:
            return self._award_uncontested(remaining[0])

        return self._advance_if_street_over(seat)

    def preview(self, raw: dict, seat: Optional[int] = None) -> dict:
        """The action a reply would turn into (lenient rules, as in a live
        match), without applying it: {"action", "amount"}."""
        seat = self.to_act if seat is None else seat
        a = self._validate(seat, raw)
        return {"action": a.action, "amount": a.amount}

    def legal_actions(self, seat: Optional[int] = None) -> dict:
        """What `seat` (default: the seat to act) may do right now.
        Raise amounts are totals for the street, like the "raise" action."""
        seat = self.to_act if seat is None else seat
        p    = self.players[seat]
        owed = max(0, self.current_bet - p.bet_this_street)
        max_to = p.stack + p.bet_this_street
        opponents_can_act = any(
            pl.is_active for pl in self.players if pl.seat != seat)
        can_raise = p.stack > owed and opponents_can_act
        return {
            "can_fold":     True,
            "can_check":    owed == 0,
            "call_amount":  min(owed, p.stack),
            "can_raise":    can_raise,
            # if a full min-raise is unaffordable, all-in is the only raise
            "min_raise_to": min(self.current_bet + self.min_raise, max_to) if can_raise else None,
            "max_raise_to": max_to if can_raise else None,
        }

    @property
    def board_plan(self) -> list:
        """All five board cards this hand will deal, including undealt ones
        (god view only; never show this to a bot)."""
        return [str(c) for c in self._board_plan]

    @property
    def hole_plan(self) -> dict:
        """{seat: ["As", "Kh"]} for every seat dealt in (god view only)."""
        return {seat: [str(c) for c in cards] for seat, cards in self._hole_plan.items()}

    @property
    def waiting_on(self) -> frozenset:
        """Seats that still have to act on this street."""
        return frozenset(s for s in self._needs_to_act if self.players[s].is_active)

    def positions(self) -> dict:
        """{seat: position name} for this hand's seats with chips."""
        return seat_positions([0 if p.sitting_out else 1 for p in self.players],
                              self.dealer_seat)

    # -----------------------------------------------------------------------
    # Seat helpers (sitting-out seats are skipped everywhere)
    # -----------------------------------------------------------------------

    @property
    def _is_heads_up(self) -> bool:
        return len(self._live) == 2

    def _next_live(self, seat: int) -> int:
        for offset in range(1, self.n + 1):
            s = (seat + offset) % self.n
            if not self.players[s].sitting_out:
                return s
        raise AssertionError("no live seats")

    def _sb_seat(self) -> int:
        """Heads-up: dealer IS the small blind."""
        if self._is_heads_up:
            return self.dealer_seat
        return self._next_live(self.dealer_seat)

    def _bb_seat(self) -> int:
        return self._next_live(self._sb_seat())

    def _utg_seat(self) -> int:
        """
        Preflop first actor.
        Heads-up: SB (dealer) acts first.
        Normal:   first active seat after BB.
        """
        if self._is_heads_up:
            return self._sb_seat()
        bb = self._bb_seat()
        for offset in range(1, self.n + 1):
            s = (bb + offset) % self.n
            if self.players[s].is_active:
                return s
        return bb  # fallback

    def _first_postflop_actor(self) -> Optional[int]:
        """
        Postflop: first active player left of dealer.
        Heads-up: BB (non-dealer) acts first postflop.
        """
        if self._is_heads_up:
            for seat in [self._bb_seat(), self._sb_seat()]:
                if self.players[seat].is_active:
                    return seat
            return None
        for offset in range(1, self.n + 1):
            s = (self.dealer_seat + offset) % self.n
            if self.players[s].is_active:
                return s
        return None

    # -----------------------------------------------------------------------
    # Action flow
    # -----------------------------------------------------------------------

    def _action_needed(self) -> bool:
        """Is there any betting decision left to make on this street?
        No if nobody can act, or only one player can and owes nothing
        (nobody could respond to a bet)."""
        active = [p for p in self.players if p.is_active]
        if not active:
            return False
        if len(active) == 1:
            p = active[0]
            return self.current_bet - p.bet_this_street > 0
        return True

    def _handle_aggression(self, seat: int, raise_size: int):
        """
        If raise_size >= last full raise: reopen action for everyone except aggressor.
        If short all-in (raise_size < last full raise): do NOT reopen.
        """
        if raise_size >= self._last_aggression_size:
            self._last_aggression_size = raise_size
            self.min_raise = raise_size
            self._needs_to_act = {
                p.seat for p in self.players
                if p.is_active and p.seat != seat
            }
        # else: short all-in, _needs_to_act unchanged

    def _next_actor(self, from_seat: int) -> Optional[int]:
        if not self._needs_to_act:
            return None
        for offset in range(1, self.n + 1):
            s = (from_seat + offset) % self.n
            if s in self._needs_to_act and self.players[s].is_active:
                return s
        return None

    def _advance_if_street_over(self, last_seat: int) -> dict:
        if not self._action_needed():
            self._needs_to_act.clear()
        nxt = self._next_actor(last_seat)
        if nxt is not None:
            return self._build_state(nxt)
        return self._advance_street()

    def _advance_street(self) -> dict:
        """Deal the next street. If nobody can bet on it, keep dealing
        (running out the board) until showdown."""
        while True:
            for p in self.players:
                p.bet_this_street = 0
            self.current_bet           = 0
            self.min_raise             = self.big_blind
            self._last_aggression_size = self.big_blind

            if self.street == "river":
                return self._showdown()

            if self.street == "preflop":
                self.street = "flop"
            elif self.street == "flop":
                self.street = "turn"
            else:
                self.street = "river"
            self._deal_board()

            self._emit("street_start", {
                "street":          self.street,
                "community_cards": [str(c) for c in self.community_cards],
            })

            if self._action_needed():
                self._needs_to_act = {p.seat for p in self.players if p.is_active}
                return self._build_state(self._first_postflop_actor())

    # -----------------------------------------------------------------------
    # Chips
    # -----------------------------------------------------------------------

    def _snapshot_stacks(self):
        self._starting_stacks = {p.bot_id: p.stack for p in self.players}

    def _post_blinds(self):
        sb, bb       = self._sb_seat(), self._bb_seat()
        sb_amount    = min(self.small_blind, self.players[sb].stack)
        bb_amount    = min(self.big_blind,   self.players[bb].stack)
        self._put_in(sb, sb_amount)
        self._put_in(bb, bb_amount)
        self.current_bet           = max(self.current_bet, bb_amount)
        self.min_raise             = self.big_blind
        self._last_aggression_size = self.big_blind
        if self.players[sb].stack == 0:
            self.players[sb].is_all_in = True
        if self.players[bb].stack == 0:
            self.players[bb].is_all_in = True
        self.action_log.append({"seat": sb, "action": "small_blind", "amount": sb_amount})
        self.action_log.append({"seat": bb, "action": "big_blind",   "amount": bb_amount})
        self._emit("blind", {"seat": sb, "bot_id": self.players[sb].bot_id,
                             "action": "small_blind", "amount": sb_amount})
        self._emit("blind", {"seat": bb, "bot_id": self.players[bb].bot_id,
                             "action": "big_blind",   "amount": bb_amount})

    def _put_in(self, seat: int, amount: int):
        amount = max(0, min(amount, self.players[seat].stack))
        self.players[seat].stack           -= amount
        self.players[seat].bet_this_street += amount
        self.players[seat].total_invested  += amount
        self.pot                           += amount
        if self.players[seat].bet_this_street > self.current_bet:
            self.current_bet = self.players[seat].bet_this_street

    # -----------------------------------------------------------------------
    # Deck
    # -----------------------------------------------------------------------

    def _plan_deal(self, hole_cards: Optional[dict], board: Optional[list]):
        """Decide every card up front: rigged cards where given, the rest
        drawn from the remaining deck. Draw order (each live seat's two
        cards in seat order, then five board cards) matches upstream, so an
        unrigged seeded hand deals the same cards as before."""
        hole_cards = {int(s): list(cs) for s, cs in (hole_cards or {}).items()}
        board      = list(board or [])
        if len(board) > 5:
            raise ValueError(f"Board has {len(board)} cards; max is 5")
        board += [None] * (5 - len(board))

        fixed = []
        for seat, cards in hole_cards.items():
            if not 0 <= seat < self.n:
                raise ValueError(f"No seat {seat}")
            if self.players[seat].sitting_out:
                raise ValueError(f"Seat {seat} is sitting out and gets no cards")
            if len(cards) != 2:
                raise ValueError(f"Seat {seat} needs exactly 2 hole cards, got {cards}")
            fixed += cards
        fixed += [c for c in board if c is not None]

        fixed_cards = [_parse_card(c) for c in fixed]
        if len(set(fixed_cards)) != len(fixed_cards):
            dupes = sorted({str(c) for c in fixed_cards if fixed_cards.count(c) > 1})
            raise ValueError(f"Duplicate cards: {dupes}")

        ranks = "23456789TJQKA"
        suits = "shdc"
        deck  = [eval7.Card(r + s) for r in ranks for s in suits]
        deck  = [c for c in deck if c not in fixed_cards]
        rng   = random.Random(self.seed) if self.seed is not None else random
        rng.shuffle(deck)
        draw  = iter(deck)

        hole_plan = {}
        for seat in self._live:
            if seat in hole_cards:
                hole_plan[seat] = [_parse_card(c) for c in hole_cards[seat]]
            else:
                hole_plan[seat] = [next(draw), next(draw)]
        board_plan = [_parse_card(c) if c is not None else next(draw) for c in board]
        return hole_plan, board_plan

    def _deal_hole_cards(self):
        for seat, cards in self._hole_plan.items():
            self.players[seat].hole_cards = list(cards)

    def _deal_board(self):
        n = 3 if not self.community_cards else 1
        k = len(self.community_cards)
        self.community_cards += self._board_plan[k:k + n]

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_raw(raw) -> tuple:
        raw = raw if isinstance(raw, dict) else {}
        act = str(raw.get("action", "fold")).lower().strip()
        try:
            amount = int(raw.get("amount") or 0)
        except (TypeError, ValueError):
            amount = None
        return act, amount

    def _validate(self, seat: int, raw: dict) -> Action:
        """Lenient: always returns a legal action, correcting if needed."""
        p = self.players[seat]
        act, amount = lenient_action(
            raw, owed=self.current_bet - p.bet_this_street,
            can_raise=self.legal_actions(seat)["can_raise"],
            min_raise_to=self.current_bet + self.min_raise,
            stack=p.stack, bet=p.bet_this_street)
        return Action(seat, act, amount)

    def _validate_strict(self, seat: int, raw: dict) -> Action:
        """Strict: returns the action as given, or raises IllegalActionError."""
        p     = self.players[seat]
        legal = self.legal_actions(seat)
        act, amount = self._parse_raw(raw)

        def illegal(msg):
            return IllegalActionError(f"[{self.hand_id}] Seat {seat} {act}: {msg}")

        if act not in ACTIONS:
            raise illegal(f"unknown action; expected one of {ACTIONS}")

        owed = self.current_bet - p.bet_this_street

        if act == "fold":
            return Action(seat, "fold")

        if act == "check":
            if owed > 0:
                raise illegal(f"can't check, owes {owed}")
            return Action(seat, "check")

        if act == "call":
            if owed == 0:
                raise illegal("nothing to call; use check")
            return Action(seat, "call", owed)

        if act == "all_in":
            if not legal["can_raise"] and p.stack > owed:
                raise illegal("can't raise here; call or fold")
            return Action(seat, "all_in", p.stack + p.bet_this_street)

        # raise
        if not legal["can_raise"]:
            raise illegal("can't raise here")
        if amount is None:
            raise illegal("amount must be an integer")
        lo, hi = legal["min_raise_to"], legal["max_raise_to"]
        if amount == hi:
            return Action(seat, "all_in", hi)
        if not lo <= amount < hi:
            raise illegal(f"raise to {amount} outside [{lo}, {hi}]")
        return Action(seat, "raise", amount)

    # -----------------------------------------------------------------------
    # Resolution
    # -----------------------------------------------------------------------

    def _return_uncalled(self):
        """Give the top bettor back whatever nobody else matched: it was
        never contested, so it isn't part of any pot."""
        by_invested = sorted(self.players, key=lambda p: -p.total_invested)
        top, second = by_invested[0], by_invested[1]
        excess = top.total_invested - second.total_invested
        if excess <= 0:
            return
        top.stack          += excess
        top.total_invested -= excess
        top.bet_this_street = max(0, top.bet_this_street - excess)
        self.pot           -= excess
        self.uncalled = {"seat": top.seat, "bot_id": top.bot_id, "amount": excess}
        self._emit("uncalled_bet_returned", dict(self.uncalled))

    def _showdown(self) -> dict:
        self._return_uncalled()
        contenders = [p for p in self.players if p.in_hand]
        if len(contenders) == 1:
            return self._award_uncontested(contenders[0])

        scored = [
            (eval7.evaluate(p.hole_cards + self.community_cards), p)
            for p in contenders
        ]

        # Hand strength labels
        hand_strengths = {}
        for score, p in scored:
            try:
                hand_strengths[p.bot_id] = str(eval7.handtype(score))
            except Exception:
                hand_strengths[p.bot_id] = "unknown"

        # Side-pot resolution
        side_pots   = self._compute_side_pots()
        winners_log = []

        for pot_info in side_pots:
            eligible_ids    = {p.bot_id for p in pot_info["eligible"]}
            eligible_scored = [(s, p) for s, p in scored if p.bot_id in eligible_ids]
            best            = max(s for s, _ in eligible_scored)
            pot_winners     = [p for s, p in eligible_scored if s == best]

            split     = pot_info["amount"] // len(pot_winners)
            remainder = pot_info["amount"] % len(pot_winners)
            for i, w in enumerate(pot_winners):
                award = split + (remainder if i == 0 else 0)
                w.stack += award
                winners_log.append({
                    "bot_id":   w.bot_id,
                    "seat":     w.seat,
                    "amount":   award,
                    "pot_type": "main" if pot_info is side_pots[0] else "side",
                })

        revealed = {p.bot_id: [str(c) for c in p.hole_cards] for _, p in scored}

        self._emit("showdown", {
            "community_cards": [str(c) for c in self.community_cards],
            "revealed":        revealed,
            "hand_strengths":  hand_strengths,
            "winners":         winners_log,
        })

        self._check_invariants()
        return self._build_result(winners_log, showdown=True,
                                  revealed=revealed, hand_strengths=hand_strengths)

    def _award_uncontested(self, winner: Player) -> dict:
        self._return_uncalled()
        winner.stack += self.pot
        result = [{"bot_id": winner.bot_id, "seat": winner.seat,
                   "amount": self.pot, "pot_type": "main"}]
        self._emit("uncontested_win", {"bot_id": winner.bot_id, "amount": self.pot})
        self._check_invariants()
        return self._build_result(result, showdown=False)

    def _compute_side_pots(self) -> list:
        """
        Build side pots based on total_invested per player.
        Returns list of {amount, eligible} sorted smallest to largest.
        """
        in_players = [p for p in self.players if p.in_hand]
        if not in_players:
            return [{"amount": self.pot, "eligible": []}]

        levels   = sorted(set(p.total_invested for p in self.players
                               if p.total_invested > 0))
        pots     = []
        prev_lvl = 0

        for lvl in levels:
            per_player   = lvl - prev_lvl
            contributors = [p for p in self.players if p.total_invested >= lvl]
            pot_amount   = per_player * len(contributors)
            eligible     = [p for p in in_players if p.total_invested >= lvl]
            if pot_amount > 0 and eligible:
                pots.append({"amount": pot_amount, "eligible": eligible})
            prev_lvl = lvl

        if not pots:
            return [{"amount": self.pot, "eligible": in_players}]

        # Absorb any rounding residual
        total = sum(p["amount"] for p in pots)
        if total != self.pot:
            pots[-1]["amount"] += self.pot - total

        return pots

    # -----------------------------------------------------------------------
    # Invariants
    # -----------------------------------------------------------------------

    def _check_invariants(self):
        total_start = sum(self._starting_stacks.values())
        total_now   = sum(p.stack for p in self.players)
        if total_now != total_start:
            raise AssertionError(
                f"[{self.hand_id}] Chip invariant violated: "
                f"started={total_start}, now={total_now}. "
                f"Stacks: {[(p.bot_id, p.stack) for p in self.players]}"
            )

    # -----------------------------------------------------------------------
    # Events
    # -----------------------------------------------------------------------

    def _emit(self, event_type: str, data: dict):
        self.events.append({
            "type":   event_type,
            "street": self.street,
            "pot":    self.pot,
            **data,
        })

    def _emit_action(self, seat: int, action: str, amount: int):
        p = self.players[seat]
        self._emit("action", {
            "seat":        seat,
            "bot_id":      p.bot_id,
            "action":      action,
            "amount":      amount,
            "pot_after":   self.pot,
            "stack_after": p.stack,
            "stacks":      {pl.bot_id: pl.stack for pl in self.players},
        })

    # -----------------------------------------------------------------------
    # Serialisation
    # -----------------------------------------------------------------------

    def _build_state(self, seat: int) -> dict:
        self.to_act = seat
        p    = self.players[seat]
        owed = max(0, self.current_bet - p.bet_this_street)
        return {
            "type":                  "action_request",
            "hand_id":               self.hand_id,
            "hand_num":              self.hand_num,
            "street":                self.street,
            "seat_to_act":           seat,
            "dealer_seat":           self.dealer_seat,
            "small_blind":           self.small_blind,
            "big_blind":             self.big_blind,
            "pot":                   self.pot,
            "community_cards":       [str(c) for c in self.community_cards],
            "current_bet":           self.current_bet,
            "min_raise_to":          self.current_bet + self.min_raise,
            "amount_owed":           owed,
            "can_check":             owed == 0,
            "legal_actions":         self.legal_actions(seat),
            "your_cards":            [str(c) for c in p.hole_cards],
            "your_stack":            p.stack,
            "your_bet_this_street":  p.bet_this_street,
            "players":               [pl.to_public_dict() for pl in self.players],
            "action_log":            list(self.action_log),
        }

    def _build_result(
        self,
        winners: list,
        showdown: bool,
        revealed: Optional[dict] = None,
        hand_strengths: Optional[dict] = None,
    ) -> dict:
        self.to_act      = None
        self.is_complete = True
        return {
            "type":            "hand_complete",
            "hand_id":         self.hand_id,
            "hand_num":        self.hand_num,
            "street":          self.street,
            "dealer_seat":     self.dealer_seat,
            "pot":             self.pot,
            "community_cards": [str(c) for c in self.community_cards],
            "winners":         winners,
            "showdown":        showdown,
            "revealed_cards":  revealed or {},
            "hand_strengths":  hand_strengths or {},
            "action_log":      list(self.action_log),
            "events":          list(self.events),
            "uncalled":        self.uncalled,
            "final_stacks":    {p.bot_id: p.stack for p in self.players},
        }
