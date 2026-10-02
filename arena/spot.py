"""
Spots: poker situations set up on purpose, for testing bots.

A spot is a table (seats, stacks, blinds, button), any cards you want to
fix (hole cards and board; the rest come from the seed), and the actions
taken so far. Building a spot deals the hand and replays those actions in
strict mode, so every spot is a legal position reached by the real rules.

Notation (used by the CLI and in spot files):

  cards    "s0=AsKh s4=QdQc"  or  "BTN=AsKh BB=QdQc"    (seat or position)
  board    "Kd7c2s|9h|"       flop|turn|river; missing or ?? = random
           "Kd7c2s9h"         also fine: cards in deal order
  stacks   "10000"            every seat
           "10000,5000,0"     per seat (0 = busted, sits out)
           "10000 s3=2500"    default plus overrides
  blinds   "50/100"
  actions  "pre: s4 r300, s0 r1000, s4 c; flop: s4 x"
           streets separated by ';' (label optional, in order from preflop),
           actions by ','. Each action: optional seat (s4 or a position
           like BTN), then f fold | x check | c call | rN raise to N
           (bN is the same) | a all-in. The seat is optional; if it names a
           seat that isn't next, the seats in between check if they can and
           fold otherwise, so "pre: BTN r250, BB c" means folds to the button.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from arena.engine.game import (
    BIG_BLIND, MAX_PLAYERS, SMALL_BLIND, STARTING_STACK, STREETS,
    PokerEngine, next_button, seat_positions,
)

DEFAULT_PLAYERS = 6

STREET_LABELS = {
    "p": "preflop", "pre": "preflop", "preflop": "preflop",
    "flop": "flop", "turn": "turn", "river": "river",
}

_ACTION_WORDS = {
    "f": "fold", "fold": "fold",
    "x": "check", "k": "check", "check": "check",
    "c": "call", "call": "call",
    "a": "all_in", "allin": "all_in", "all_in": "all_in", "all-in": "all_in",
    "shove": "all_in", "jam": "all_in",
}
_RAISE_WORDS = {"r", "b", "raise", "bet"}
_ACTION_CODES = {"fold": "f", "check": "x", "call": "c", "all_in": "a"}

_CARD_RE = re.compile(r"[2-9TJQKA][shdc]|\?\?", re.IGNORECASE)


class SpotError(ValueError):
    """A spot that can't be parsed or doesn't replay legally."""


@dataclass
class ActionSpec:
    street: str
    action: str                  # fold | check | call | raise | all_in
    amount: Optional[int] = None  # raise-to total, for raise
    seat: Optional[int] = None   # checked against the seat to act, if given

    def code(self) -> str:
        if self.action == "raise":
            return f"r{self.amount}"
        return _ACTION_CODES[self.action]

    def notation(self) -> str:
        return self.code() if self.seat is None else f"s{self.seat} {self.code()}"

    def raw(self) -> dict:
        if self.action == "raise":
            return {"action": "raise", "amount": self.amount}
        return {"action": self.action}


@dataclass
class Spot:
    players: int = DEFAULT_PLAYERS
    button: int = 0
    stacks: list = field(default_factory=list)
    blinds: tuple = (SMALL_BLIND, BIG_BLIND)
    cards: dict = field(default_factory=dict)       # {seat: ["As", "Kh"]}
    board: list = field(default_factory=lambda: [None] * 5)
    actions: list = field(default_factory=list)     # [ActionSpec]
    to_act: Optional[int] = None
    seed: Optional[int] = None
    name: Optional[str] = None
    description: Optional[str] = None

    def __post_init__(self):
        if not self.stacks:
            self.stacks = [STARTING_STACK] * self.players
        if len(self.stacks) != self.players:
            raise SpotError(f"{len(self.stacks)} stacks for {self.players} players")
        if not 2 <= self.players <= MAX_PLAYERS:
            raise SpotError(f"players must be 2-{MAX_PLAYERS}, got {self.players}")
        self.board = (list(self.board) + [None] * 5)[:5]

    # -- positions ----------------------------------------------------------

    def positions(self) -> dict:
        try:
            return seat_positions(self.stacks, self.button)
        except ValueError as e:
            raise SpotError(str(e)) from None

    def seat_label(self, seat: int) -> str:
        pos = self.positions().get(seat)
        return f"s{seat}" + (f" ({pos})" if pos else "")

    # -- building -----------------------------------------------------------

    def to_engine(self, hand_id: str = "spot", strict: bool = True):
        """Deal the hand and replay the spot's actions.
        Returns (engine, state) where state is the next action_request or
        the hand_complete result."""
        ids = [f"s{i}" for i in range(self.players)]
        try:
            eng = PokerEngine(
                hand_id, ids,
                dealer_seat     = self.button,
                starting_stacks = dict(zip(ids, self.stacks)),
                seed            = self.seed,
                small_blind     = self.blinds[0],
                big_blind       = self.blinds[1],
                hole_cards      = self.cards,
                board           = self.board,
            )
        except (ValueError, AssertionError) as e:
            raise SpotError(str(e)) from None

        state = eng.start_hand()
        for i, a in enumerate(self.actions, 1):
            where = f"action {i} ({a.street}: {a.notation()})"
            if state["type"] != "action_request":
                raise SpotError(f"{where}: the hand is already over")
            if state["street"] != a.street:
                raise SpotError(
                    f"{where}: hand is on the {state['street']}, not the {a.street}")
            try:
                state = self._skip_to(eng, state, a, where)
            except SpotError:
                raise
            except ValueError as e:
                raise SpotError(f"{where}: {e}") from None
            seat = state["seat_to_act"]
            try:
                state = eng.apply_action(seat, a.raw(), strict=strict)
            except ValueError as e:
                raise SpotError(f"{where}: {e}") from None

        if self.to_act is not None:
            if state["type"] != "action_request":
                raise SpotError(
                    f"spot says {self.seat_label(self.to_act)} to act, but the hand is over")
            if state["seat_to_act"] != self.to_act:
                raise SpotError(
                    f"spot says {self.seat_label(self.to_act)} to act, but "
                    f"{self.seat_label(state['seat_to_act'])} is")
        return eng, state

    def _skip_to(self, eng, state, a: ActionSpec, where: str) -> dict:
        """If `a` names a seat that isn't next, the seats in between check
        if they can and fold otherwise ("folds to the button")."""
        while a.seat is not None and state["seat_to_act"] != a.seat:
            if a.seat not in eng.waiting_on:
                raise SpotError(
                    f"{where}: {self.seat_label(a.seat)} can't act now "
                    f"({eng.players[a.seat].state}, or already acted); "
                    f"{self.seat_label(state['seat_to_act'])} is to act")
            skip = {"action": "check"} if state["can_check"] else {"action": "fold"}
            state = eng.apply_action(state["seat_to_act"], skip, strict=True)
            if state["type"] != "action_request" or state["street"] != a.street:
                raise SpotError(
                    f"{where}: skipping seats to reach {self.seat_label(a.seat)} "
                    f"ended the {a.street}")
        return state

    def compact(self) -> "Spot":
        """A copy with the shortest action list that replays to the same
        hand: folds that the skip rule would fill in are dropped (e.g.
        folds before a later raise). Seats are always written."""
        full = self.to_engine()[0]
        expanded = self.with_actions_from(full)
        # replay once more to see, for each action, whether it was the
        # filler the skip rule would have produced
        base = Spot.from_dict({**expanded.to_dict(), "actions": None})
        eng, state = base.to_engine()
        filler = []
        for a in expanded.actions:
            # only implied folds are dropped; checks stay so the line shows
            # who acted
            filler.append(a.action == "fold" and not state["can_check"])
            state = eng.apply_action(state["seat_to_act"], a.raw(), strict=True)
        kept, kept_later_street = [], None
        for a, is_filler in reversed(list(zip(expanded.actions, filler))):
            if is_filler and kept_later_street == a.street:
                continue
            kept.append(a)
            kept_later_street = a.street
        out = Spot.from_dict(expanded.to_dict())
        out.actions = list(reversed(kept))
        out.to_act = self.to_act
        return out

    def with_actions_from(self, eng: PokerEngine) -> "Spot":
        """A copy of this spot whose actions are everything `eng` has
        applied (as normalised by the engine), with seats filled in."""
        acts = [ActionSpec(e["street"], e["action"],
                           e["amount"] if e["action"] == "raise" else None, e["seat"])
                for e in eng.events if e["type"] == "action"]
        d = self.to_dict()
        d["actions"] = actions_to_notation(acts)
        d.pop("to_act", None)
        return Spot.from_dict(d)

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict:
        d = {}
        if self.name:
            d["name"] = self.name
        if self.description:
            d["description"] = self.description
        d["players"] = self.players
        d["button"]  = self.button
        d["blinds"]  = f"{self.blinds[0]}/{self.blinds[1]}"
        d["stacks"]  = (self.stacks[0] if len(set(self.stacks)) == 1
                        else ",".join(str(s) for s in self.stacks))
        if self.cards:
            d["cards"] = {f"s{s}": "".join(c) for s, c in sorted(self.cards.items())}
        if any(c is not None for c in self.board):
            d["board"] = board_to_notation(self.board)
        if self.actions:
            d["actions"] = actions_to_notation(self.actions)
        if self.to_act is not None:
            d["to_act"] = f"s{self.to_act}"
        if self.seed is not None:
            d["seed"] = self.seed
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Spot":
        d = dict(d)
        unknown = set(d) - {"name", "description", "players", "button", "blinds",
                            "stacks", "cards", "board", "actions", "to_act", "seed"}
        if unknown:
            raise SpotError(f"unknown spot fields: {sorted(unknown)}")

        stacks_raw = d.get("stacks")
        players    = d.get("players")
        if isinstance(stacks_raw, list):
            stacks_raw = ",".join(str(s) for s in stacks_raw)
        if players is None:
            players = (len(parse_stack_list(str(stacks_raw)))
                       if stacks_raw is not None and "," in str(stacks_raw)
                       else DEFAULT_PLAYERS)
        players = int(players)
        stacks  = parse_stacks(str(stacks_raw), players) if stacks_raw is not None else None

        blinds = d.get("blinds", (SMALL_BLIND, BIG_BLIND))
        blinds = parse_blinds(blinds) if isinstance(blinds, str) else tuple(int(b) for b in blinds)

        spot = cls(players=players, stacks=stacks or [], blinds=blinds,
                   seed=d.get("seed"), name=d.get("name"),
                   description=d.get("description"))

        button = d.get("button")
        spot.button = (int(button) if button is not None
                       else next_button(spot.stacks))

        cards = d.get("cards") or {}
        if isinstance(cards, str):
            spot.cards = parse_cards(cards, spot)
        else:
            spot.cards = {resolve_seat(str(k), spot): parse_hole(v if isinstance(v, str) else "".join(v))
                          for k, v in cards.items()}

        board = d.get("board")
        if board is not None:
            spot.board = parse_board(board if isinstance(board, str)
                                     else "".join(c or "??" for c in board))

        actions = d.get("actions")
        if actions:
            if isinstance(actions, dict):
                actions = "; ".join(f"{k}: {v if isinstance(v, str) else ', '.join(v)}"
                                    for k, v in actions.items())
            elif isinstance(actions, list):
                actions = "; ".join(actions)
            spot.actions = parse_actions(str(actions), spot)

        if d.get("to_act") is not None:
            spot.to_act = resolve_seat(str(d["to_act"]), spot)
        return spot

    @classmethod
    def load(cls, path) -> "Spot":
        path = Path(path)
        text = path.read_text()
        if path.suffix == ".json":
            data = json.loads(text)
        else:
            import yaml
            data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise SpotError(f"{path}: expected a mapping of spot fields")
        spot = cls.from_dict(data)
        if spot.name is None:
            spot.name = path.stem
        return spot

    def save(self, path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".json":
            path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        else:
            import yaml
            path.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False))


# ---------------------------------------------------------------------------
# Notation parsers
# ---------------------------------------------------------------------------

def resolve_seat(token: str, spot: Spot) -> int:
    """'s3', '3' or a position name like 'BTN' -> seat number."""
    t = token.strip()
    m = re.fullmatch(r"s?(\d+)", t, re.IGNORECASE)
    if m:
        seat = int(m.group(1))
        if not 0 <= seat < spot.players:
            raise SpotError(f"no seat {t} at a {spot.players}-seat table")
        return seat
    by_name = {name: seat for seat, name in spot.positions().items()}
    if len(by_name) == 2:
        by_name["SB"] = by_name["BTN"]       # heads-up: button is the SB
    seat = by_name.get(t.upper())
    if seat is None:
        raise SpotError(f"unknown seat or position {t!r}; positions here: "
                        + ", ".join(f"{n}=s{s}" for n, s in by_name.items()))
    return seat


def parse_hole(text: str) -> list:
    cards = _CARD_RE.findall(text.replace(" ", ""))
    if len(cards) != 2 or "".join(cards).lower() != text.replace(" ", "").lower() \
            or "??" in cards:
        raise SpotError(f"hole cards must be two cards like AsKh, got {text!r}")
    return [_norm_card(c) for c in cards]


def _norm_card(c: str) -> str:
    return c[0].upper() + c[1].lower()


def parse_cards(text: str, spot: Spot) -> dict:
    """'s0=AsKh BTN=QdQc' -> {0: ['As','Kh'], 3: ['Qd','Qc']}"""
    out = {}
    for item in re.split(r"[,\s]+", text.strip()):
        if not item:
            continue
        if "=" not in item:
            raise SpotError(f"cards: expected seat=cards like s0=AsKh, got {item!r}")
        seat_tok, hole = item.split("=", 1)
        seat = resolve_seat(seat_tok, spot)
        if seat in out:
            raise SpotError(f"cards: seat s{seat} given twice")
        out[seat] = parse_hole(hole)
    return out


def parse_board(text: str) -> list:
    """'Kd7c2s|9h|' -> ['Kd','7c','2s','9h',None]; '??' and gaps are random."""
    text = text.replace(" ", "")
    if "|" in text:
        segs = text.split("|")
        if len(segs) > 3:
            raise SpotError(f"board: at most flop|turn|river, got {text!r}")
        sizes, board = (3, 1, 1), []
        for seg, size in zip(segs + [""] * (3 - len(segs)), sizes):
            cards = _board_cards(seg)
            if len(cards) > size:
                raise SpotError(f"board: {seg!r} has more than {size} card(s)")
            board += cards + [None] * (size - len(cards))
    else:
        board = _board_cards(text)
        if len(board) > 5:
            raise SpotError(f"board: more than 5 cards in {text!r}")
        board += [None] * (5 - len(board))
    return board


def _board_cards(seg: str) -> list:
    cards = _CARD_RE.findall(seg)
    if "".join(cards).lower() != seg.lower():
        raise SpotError(f"board: can't parse {seg!r}")
    return [None if c == "??" else _norm_card(c) for c in cards]


def parse_stack_list(text: str) -> list:
    return [int(x) for x in text.split(",") if x.strip()]


def parse_stacks(text: str, players: int) -> list:
    """'10000' | '10000,5000,0' | '10000 s3=2500'"""
    items = [i for i in re.split(r"\s+", text.strip()) if i]
    base, overrides = None, {}
    for item in items:
        if "=" in item:
            k, v = item.split("=", 1)
            m = re.fullmatch(r"s?(\d+)", k, re.IGNORECASE)
            if not m:
                raise SpotError(f"stacks: overrides use seat numbers (s3=2500), got {item!r}")
            overrides[int(m.group(1))] = _int(v, "stacks")
        elif base is None:
            base = [_int(x, "stacks") for x in item.split(",") if x]
        else:
            raise SpotError(f"stacks: can't parse {text!r}")
    if base is None:
        base = [STARTING_STACK]
    stacks = base * players if len(base) == 1 else base
    if len(stacks) != players:
        raise SpotError(f"stacks: {len(stacks)} values for {players} players")
    for seat, v in overrides.items():
        if not 0 <= seat < players:
            raise SpotError(f"stacks: no seat s{seat}")
        stacks[seat] = v
    if any(s < 0 for s in stacks):
        raise SpotError("stacks can't be negative")
    return stacks


def parse_blinds(text: str) -> tuple:
    m = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", str(text))
    if not m:
        raise SpotError(f"blinds: expected SB/BB like 50/100, got {text!r}")
    return int(m.group(1)), int(m.group(2))


def parse_action_token(text: str, spot: Spot, street: str) -> ActionSpec:
    """'s4 r300' | 'BTN c' | 'x' -> ActionSpec"""
    toks = text.split()
    if not toks:
        raise SpotError("empty action")
    seat = None
    if len(toks) > 1 and not _is_action_word(toks[0]):
        seat = resolve_seat(toks[0], spot)
        toks = toks[1:]
    word = toks[0].lower()
    rest = toks[1:]
    if word in _ACTION_WORDS and not rest:
        return ActionSpec(street, _ACTION_WORDS[word], None, seat)
    m = re.fullmatch(r"(r|b|raise|bet)(\d*)", word)
    if m:
        num = m.group(2) or (rest.pop(0) if rest else "")
        if rest or not num.isdigit():
            raise SpotError(f"action {text!r}: raise needs a total, like r300")
        return ActionSpec(street, "raise", int(num), seat)
    raise SpotError(f"can't parse action {text!r}; use f, x, c, rN or a")


def _is_action_word(tok: str) -> bool:
    t = tok.lower()
    return t in _ACTION_WORDS or bool(re.fullmatch(r"(r|b|raise|bet)\d*", t))


def parse_actions(text: str, spot: Spot) -> list:
    out = []
    street_idx = -1
    for section in [s for s in text.split(";")]:
        section = section.strip()
        if not section:
            continue
        m = re.match(r"(\w+)\s*:\s*(.*)$", section, re.DOTALL)
        if m and m.group(1).lower() in STREET_LABELS:
            street = STREET_LABELS[m.group(1).lower()]
            idx = STREETS.index(street)
            if idx <= street_idx:
                raise SpotError(f"actions: {street} comes after {STREETS[street_idx]}")
            street_idx, body = idx, m.group(2)
        else:
            street_idx += 1
            if street_idx >= len(STREETS):
                raise SpotError("actions: more than four streets")
            street, body = STREETS[street_idx], section
        for item in body.split(","):
            if item.strip():
                out.append(parse_action_token(item.strip(), spot, street))
    return out


def actions_to_notation(actions: list) -> str:
    sections, cur, items = [], None, []
    for a in actions:
        if a.street != cur:
            if cur is not None:
                sections.append(f"{_short(cur)}: {', '.join(items)}")
            cur, items = a.street, []
        items.append(a.notation())
    if cur is not None:
        sections.append(f"{_short(cur)}: {', '.join(items)}")
    return "; ".join(sections)


def _short(street: str) -> str:
    return "pre" if street == "preflop" else street


def board_to_notation(board: list) -> str:
    c = [x or "??" for x in board]
    return "".join(c[:3]) + "|" + c[3] + "|" + c[4]


def _int(v: str, what: str) -> int:
    try:
        return int(v)
    except ValueError:
        raise SpotError(f"{what}: {v!r} is not a number") from None
