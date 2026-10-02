"""
Read a bridge record of an arena match (runs/arena/<match_id>/, written by
`arena connect`) as if it were a local run, so the local tools work on
production matches: `arena match show/hand/verify`, the index (`stats`,
`hands`, `decisions`, `brief`), `probe --from` and spot export.

A bridge record only holds what the arena sent our bot: our hole cards,
public actions, the board as it was dealt, and hands shown at showdown.
To replay the betting through the engine, the converted hand_start
fills the unknown cards with stand-ins, and says which are real:

  known_seats   seats whose hole cards are real (ours, plus any shown down)
  known_board   how many board cards are real (the ones that were dealt)

Everything downstream must respect those: the index stores no cards and
no equity for unknown seats, `arena match hand` shows them as ??, and spots
made from these hands leave them random. Betting, stacks and results
don't depend on the stand-ins (every hand that reaches showdown has all
its cards shown), so every hand still replays exactly.

The run id is "arena/<match_id>".
"""

import json
from pathlib import Path

PREFIX = "arena/"
_DECK = [r + s for r in "23456789TJQKA" for s in "shdc"]
_HAND_TYPES = ("hand_start", "blind", "street", "action", "decision_result", "seat_status", "hand_end")


class ArenaRecord:
    """A bridge record read as a Run (same attributes and methods)."""

    def __init__(self, path):
        self.dir = Path(path)
        raw_path = self.dir / "meta.json"
        if not raw_path.exists():
            raise FileNotFoundError(f"no arena record at {self.dir}")
        self.raw = json.loads(raw_path.read_text())
        if self.raw.get("kind") != "arena_match":
            raise FileNotFoundError(f"{self.dir} is not an arena match record")
        match = self.raw["match"]
        self.me = self.raw["me"]
        self.my_seat = match["your_seat"]
        self.names = [s["bot"]["name"] for s in sorted(match["seats"], key=lambda s: s["seat"])]
        self._events = None
        self._decisions = None
        self.meta = self._meta()

    @property
    def match_id(self) -> str:
        return PREFIX + self.raw["match"]["id"]

    def _meta(self) -> dict:
        match, raw = self.raw["match"], self.raw
        fmt = match["format"]
        end = raw.get("result") or {}
        status = {"finished": "complete", "left": "partial", "gone": "partial"}.get(raw.get("status"))
        if status is None:
            status = "running" if raw.get("finished") is None else "partial"
        errors = sum(1 for d in self._decision_rows() if d.get("error"))
        seats = []
        for i, name in enumerate(self.names):
            mine = name == self.me
            seats.append({"seat": i, "bot_id": name, "kind": "bot" if mine else "remote",
                          "path": raw["bot"]["path"] if mine else None,
                          "version": raw["bot"]["version"] if mine else None, "dir": name})
        result = None
        if end:
            stacks = end.get("stacks") or []
            result = {"n_hands": end.get("hands_played"), "end_reason": end.get("reason"),
                      "chip_delta": end.get("chip_delta", {}),
                      "final_stacks": {self.names[i]: s for i, s in enumerate(stacks)},
                      "error_counts": {self.me: errors}, "restarts": {}}
        return {
            "match_id": self.match_id, "kind": "arena_match", "perspective": "own", "me": self.me,
            "created": raw.get("started"), "status": status,
            "config": {"seed": None, "n_hands": fmt.get("hands"), "small_blind": fmt.get("small_blind", 50),
                       "big_blind": fmt.get("big_blind", 100), "starting_stack": fmt.get("stack", 10_000),
                       "reset_stacks": fmt.get("reset_stacks", False), "ranked": match.get("rated", False)},
            "seats": seats, "result": result,
        }

    # -- the Run interface ----------------------------------------------------------

    def events(self):
        if self._events is None:
            self._events = list(self._convert())
        return iter(self._events)

    def hand_events(self, hand_num: int) -> list:
        return [e for e in self.events() if e.get("hand_num") == hand_num]

    def hand_nums(self) -> list:
        return [e["hand_num"] for e in self.events() if e["type"] == "hand_start"]

    def bot_ids(self) -> list:
        return list(self.names)

    def decisions(self, bot_id: str):
        if bot_id != self.me:
            return iter(())                     # we only know our own bot's decisions
        if self._decisions is None:
            self.events()
        return iter(self._decisions)

    # -- conversion -------------------------------------------------------------------

    def _decision_rows(self) -> list:
        path = self.dir / "decisions.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    def _messages(self):
        """Stream messages in order, with reconnections merged: a match_full's
        `hand` replays the hand so far, so only messages not already seen are
        used, and its `pending` decide counts as a decide."""
        path = self.dir / "stream.jsonl"
        if not path.exists():
            return
        seen = []                                # this hand's messages so far
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            msg = json.loads(line)
            t = msg.get("type")
            if t == "match_full":
                hand = [h for h in msg.get("hand") or [] if h.get("type") in _HAND_TYPES]
                if hand and hand[0].get("type") == "hand_start":
                    same = seen and seen[0].get("hand_num") == hand[0].get("hand_num")
                    start = len(seen) if same else 0
                    if not same:
                        seen = []
                    for h in hand[start:]:
                        seen.append(h)
                        yield h
                if msg.get("pending"):
                    yield msg["pending"]
                continue
            if t == "hand_start":
                seen = []
            if t in _HAND_TYPES:
                seen.append(msg)
            yield msg

    def _convert(self):
        rows = {r["decision_id"]: r for r in self._decision_rows()}
        decisions, seq, hand, pending = [], 0, None, None
        names = self.names
        for msg in self._messages():
            t = msg.get("type")
            if t == "hand_start":
                hand = {"start": msg, "events": [], "ts": msg.get("received")}
                pending = None
            elif hand is None:
                if t == "match_end":
                    seq += 1
                    yield {"v": 1, "seq": seq, "ts": msg.get("received"), "match_id": self.match_id,
                           "hand_num": None, "type": "match_end", **_strip(msg)}
                continue
            elif t == "decide":
                if msg["hand_num"] == hand["start"]["hand_num"]:
                    pending = msg
            elif t == "blind":
                hand["events"].append({"type": "blind", "street": "preflop", "seat": msg["seat"],
                                       "bot_id": msg["bot"], "action": msg["kind"], "amount": msg["amount"]})
            elif t == "street":
                hand["events"].append({"type": "street_start", "street": msg["street"],
                                       "community_cards": msg["board"]})
            elif t == "action":
                ev = {"type": "action", "street": msg["street"], "seat": msg["seat"], "bot_id": msg["bot"],
                      "action": msg["action"], "amount": msg["amount"], "pot_after": msg["pot_after"],
                      "stacks": {names[i]: s for i, s in enumerate(msg["stacks"])}}
                if msg["seat"] == self.my_seat and pending is not None:
                    did = pending["decision_id"]
                    ev["decision_id"] = did
                    decisions.append(self._decision(did, pending, rows.get(did, {}), msg))
                    pending = None
                hand["events"].append(ev)
            elif t == "hand_end":
                for ev in self._hand_events(hand, msg):
                    seq += 1
                    yield {"v": 1, "seq": seq, "ts": hand["ts"], "match_id": self.match_id,
                           "hand_num": msg["hand_num"], **ev}
                hand = None
        self._decisions = decisions

    def _decision(self, did, decide, row, action) -> dict:
        state = dict(decide["state"])
        state["match_action_log_len"] = len(state.pop("match_action_log", []) or [])
        return {"decision_id": did, "hand_num": decide["hand_num"], "seat": self.my_seat, "bot_id": self.me,
                "street": state.get("street"), "state": state, "response": row.get("response"),
                "applied": {"action": action["action"], "amount": action["amount"]},
                "corrected": bool(row.get("corrected")), "error": row.get("error"),
                "detail": row.get("detail"), "logs": row.get("logs") or [],
                "elapsed_ms": row.get("elapsed_ms"), "bot_ms": row.get("bot_ms"),
                "restarted": bool(row.get("restarted"))}

    def _hand_events(self, hand, end) -> list:
        start, names = hand["start"], self.names
        known = {str(self.my_seat): list(start["your_cards"])} if start.get("your_cards") else {}
        for seat, cards in (end.get("revealed") or {}).items():
            known[str(seat)] = list(cards)
        board = list(end.get("board") or [])
        used = {c for cards in known.values() for c in cards} | set(board)
        spare = (c for c in _DECK if c not in used)
        holes = dict(known)
        for seat, stack in enumerate(start["stacks"]):
            if stack > 0 and str(seat) not in holes:
                holes[str(seat)] = [next(spare), next(spare)]
        board_plan = board + [next(spare) for _ in range(5 - len(board))]
        hs = {"type": "hand_start", "hand_id": start["hand_id"], "dealer_seat": start["dealer_seat"],
              "blinds": start["blinds"], "bot_ids": names, "stacks": start["stacks"],
              "positions": start["positions"], "hole_cards": holes, "board_plan": board_plan,
              "known_seats": sorted(int(s) for s in known), "known_board": len(board)}
        he = {"type": "hand_end", "showdown": end["showdown"], "pot": end["pot"], "board": board,
              "winners": [{"bot_id": w["bot"], "seat": w["seat"], "amount": w["amount"],
                           "pot_type": w["pot_type"]} for w in end["winners"]],
              "uncalled": ({"seat": end["uncalled"]["seat"], "bot_id": end["uncalled"]["bot"],
                            "amount": end["uncalled"]["amount"]} if end.get("uncalled") else None),
              "hand_strengths": {},
              "final_stacks": {names[i]: s for i, s in enumerate(end["stacks"])},
              "delta": {names[i]: d for i, d in enumerate(end["delta"])}}
        return [hs] + hand["events"] + [he]


def _strip(msg: dict) -> dict:
    return {k: v for k, v in msg.items() if k not in ("type", "received")}


def is_arena_ref(ref: str) -> bool:
    return str(ref).startswith(PREFIX)


def list_records(root: Path) -> list:
    d = Path(root) / "arena"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        try:
            out.append(ArenaRecord(p))
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            continue
    return out
