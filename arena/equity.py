"""
Showdown equity for 2-9 hands, using eval7.

Each player is a fixed hand ("AsKh"), a range in eval7 syntax
("QQ+,AKs", "22+,A2s+"), or "??" for a random hand. Ties split equity.

Exact enumeration is used when every hand is fixed and the number of
board run-outs is small (always true from the flop on); otherwise a
seeded Monte Carlo.
"""

import itertools
import random
from dataclasses import dataclass
from math import comb

import eval7

EXACT_LIMIT = 50_000
DEFAULT_ITERS = 20_000

_DECK = [eval7.Card(r + s) for r in "23456789TJQKA" for s in "shdc"]


@dataclass
class _Player:
    label: str
    fixed: tuple = None      # (Card, Card)
    combos: list = None      # [((Card, Card), weight)] for a range
    random: bool = False


def _cards(text: str, what: str) -> list:
    text = text.replace(" ", "")
    if len(text) % 2:
        raise ValueError(f"{what}: can't parse {text!r}")
    try:
        return [eval7.Card(text[i:i + 2][0].upper() + text[i:i + 2][1].lower())
                for i in range(0, len(text), 2)]
    except Exception:
        raise ValueError(f"{what}: can't parse {text!r}") from None


def _parse_player(text: str) -> _Player:
    t = text.strip()
    if t in ("??", "random", "any"):
        return _Player(t, random=True)
    if len(t) == 4:
        try:
            c = _cards(t, "hand")
            if c[0] == c[1]:
                raise ValueError(f"hand {t!r} repeats a card")
            return _Player(t, fixed=tuple(c))
        except ValueError:
            pass   # maybe a range like "AKs+"? fall through
    try:
        combos = eval7.HandRange(t).hands
    except Exception:
        raise ValueError(f"can't parse hand or range {t!r}") from None
    if not combos:
        raise ValueError(f"range {t!r} is empty")
    return _Player(t, combos=combos)


def equity(hands: list, board: str = "", dead: str = "",
           iters: int = DEFAULT_ITERS, seed: int = 0) -> dict:
    """Returns {"method", "samples", "board", "players": [{hand, equity,
    win, tie}]}; equity/win/tie are fractions in [0, 1]."""
    if not 2 <= len(hands) <= 9:
        raise ValueError(f"need 2-9 hands, got {len(hands)}")
    players = [_parse_player(h) for h in hands]
    board_c = _cards(board, "board") if board else []
    dead_c  = _cards(dead, "dead") if dead else []
    if len(board_c) > 5:
        raise ValueError("board has more than 5 cards")

    known = board_c + dead_c + [c for p in players if p.fixed for c in p.fixed]
    if len(set(known)) != len(known):
        raise ValueError("a card appears twice across hands/board/dead")

    need  = 5 - len(board_c)
    deck  = [c for c in _DECK if c not in set(known)]
    n     = len(players)
    share = [0.0] * n
    wins  = [0] * n
    ties  = [0] * n

    def score(holes, full_board):
        vals = [eval7.evaluate(list(h) + full_board) for h in holes]
        best = max(vals)
        winners = [i for i, v in enumerate(vals) if v == best]
        for i in winners:
            share[i] += 1 / len(winners)
            if len(winners) == 1:
                wins[i] += 1
            else:
                ties[i] += 1

    all_fixed = all(p.fixed for p in players)
    if all_fixed and comb(len(deck), need) <= EXACT_LIMIT:
        method  = "exact"
        holes   = [p.fixed for p in players]
        samples = 0
        for extra in itertools.combinations(deck, need):
            score(holes, board_c + list(extra))
            samples += 1
    else:
        method  = "monte_carlo"
        rng     = random.Random(seed)
        samples = 0
        attempts = 0
        while samples < iters:
            attempts += 1
            if attempts > iters * 50:
                raise ValueError("ranges conflict too often to sample; check hands/board")
            used  = set(known)
            holes = []
            ok    = True
            for p in players:
                if p.fixed:
                    holes.append(p.fixed)
                    continue
                if p.combos:
                    (a, b), = rng.choices([h for h, _ in p.combos],
                                          weights=[w for _, w in p.combos])
                    if a in used or b in used:
                        ok = False
                        break
                    hole = (a, b)
                else:
                    pool = [c for c in deck if c not in used]
                    hole = tuple(rng.sample(pool, 2))
                used.update(hole)
                holes.append(hole)
            if not ok:
                continue
            pool = [c for c in deck if c not in used]
            score(holes, board_c + rng.sample(pool, need))
            samples += 1

    return {
        "method":  method,
        "samples": samples,
        "board":   [str(c) for c in board_c],
        "players": [
            {"hand": p.label, "equity": share[i] / samples,
             "win": wins[i] / samples, "tie": ties[i] / samples}
            for i, p in enumerate(players)
        ],
    }
