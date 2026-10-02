"""
MatchRunner: plays a multi-hand match between 2-9 seats and records it.

    runner = MatchRunner("m1", {"shark": seat_a, "mybot": seat_b},
                         MatchConfig(n_hands=400, seed=7), writer=RunWriter("m1"))
    result = await runner.run()

- Seats are fixed for the whole match; a seat with no chips sits out.
  The button moves to the next seat with chips each hand.
- Every seat decides through the Seat interface, with the seat's own time
  limit. Failed decisions check/fold (see poker_harness/seats.py).
- With a RunWriter, everything is written to runs/<match_id>/ as it
  happens (see poker_harness/runs.py for the layout and event types). `on_event`
  receives every event too, for live viewers.
- A match always has a seed (one is picked if not given), so its deals
  can be reproduced, and every hand can be replayed from its events.
- With reset_stacks, every hand starts from the starting stacks; results
  are the running total of each hand's result.
"""

import asyncio
import hashlib
import os
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

import poker_harness
from poker_harness.engine.game import PokerEngine, next_button
from poker_harness.runs import RunWriter, make_event
from poker_harness.seats import Seat, SubprocessBotSeat


@dataclass
class MatchConfig:
    n_hands: int = 400
    small_blind: int = 50
    big_blind: int = 100
    starting_stack: int = 10_000
    stacks: dict = field(default_factory=dict)   # per-bot starting stack overrides
    seed: Optional[int] = None
    match_log_entries: int = 200                  # state["match_action_log"] length
    ranked: bool = True
    # Every hand starts from the starting stacks (chips won or lost are
    # tallied, not carried over), so hands are independent. Used for
    # duplicate evaluation; see poker_harness/compare.py.
    reset_stacks: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def hand_seed(match_seed: int, hand_num: int) -> int:
    return match_seed * 1000003 + hand_num


def bot_ids_for_paths(paths: list) -> list:
    """Readable, unique bot ids for bot paths: the file stem, or the
    directory name for bots/<name>/bot.py, with _2, _3... for repeats."""
    ids = []
    for i, path in enumerate(paths):
        p = Path(path)
        if p.suffix in (".py", ".zip"):
            base = p.stem
            if base == "bot":
                base = p.parent.name or f"bot_{i}"
        else:
            base = p.name or f"bot_{i}"
        base = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in base) or f"bot_{i}"
        bot_id, n = base, 2
        while bot_id in ids:
            bot_id, n = f"{base}_{n}", n + 1
        ids.append(bot_id)
    return ids


def bot_fingerprint(path) -> Optional[str]:
    """Short hash identifying a bot version: bot.py's bytes plus the names
    and sizes of files in data/."""
    p = Path(path)
    bot_py = p / "bot.py" if p.is_dir() else p
    if not bot_py.is_file() or bot_py.suffix != ".py":
        return None
    h = hashlib.sha256(bot_py.read_bytes())
    data = bot_py.parent / "data"
    if data.is_dir():
        for f in sorted(data.rglob("*")):
            if f.is_file():
                h.update(f"{f.relative_to(data)}:{f.stat().st_size}".encode())
    return h.hexdigest()[:12]


def _corrected(raw, applied: dict) -> bool:
    if not isinstance(raw, dict):
        return True
    act = str(raw.get("action", "")).lower().strip()
    if act != applied["action"]:
        return True
    if act == "raise":
        try:
            return int(raw.get("amount") or 0) != applied["amount"]
        except (TypeError, ValueError):
            return True
    return False


class MatchRunner:
    def __init__(
        self,
        match_id: str,
        seats: dict,                       # {bot_id: Seat}, in seat order
        config: Optional[MatchConfig] = None,
        writer: Optional[RunWriter] = None,
        on_event: Optional[Callable] = None,
        labels: Optional[dict] = None,     # extra info stored in meta.json
        keep_hands: bool = False,          # keep hand_complete results in memory
        verbose: bool = False,
    ):
        if not 2 <= len(seats) <= 9:
            raise ValueError(f"need 2-9 seats, got {len(seats)}")
        self.match_id   = match_id
        self.seats      = dict(seats)
        self.bot_ids    = list(seats)
        self.config     = config or MatchConfig()
        self.writer     = writer
        self.on_event   = on_event
        self.keep_hands = keep_hands
        self.verbose    = verbose

        if self.config.seed is None:
            self.config.seed = random.randrange(1, 10**9)
        self.labels   = labels or {}
        self.stacks   = {b: self.config.stacks.get(b, self.config.starting_stack)
                         for b in self.bot_ids}
        self.errors   = {b: [] for b in self.bot_ids}
        self.totals   = {b: 0 for b in self.bot_ids}     # reset_stacks running results
        self.restarts = {b: 0 for b in self.bot_ids}
        self.hands    = []
        self.hands_played  = 0
        self._seq          = 0
        self._decision_id  = 0
        self._hand_num     = None
        self._match_log    = []

    # -- events -------------------------------------------------------------

    def _emit(self, type_: str, **data) -> dict:
        for k in ("v", "seq", "ts", "match_id", "hand_num", "type"):
            data.pop(k, None)               # envelope fields are set here
        ev = make_event(self._seq, self.match_id, type_, self._hand_num, **data)
        self._seq += 1
        if self.writer:
            self.writer.write_event(ev)
        if self.on_event:
            self.on_event(ev)
        return ev

    def _seat_event_hook(self, bot_id: str, previous: Optional[Callable]):
        def hook(ev):
            data = {k: v for k, v in ev.items() if k not in ("type", "ts", "seat_id")}
            if ev["type"] == "bot_restart":
                self.restarts[bot_id] += 1
            self._emit(ev["type"], bot_id=bot_id, **data)
            if previous:
                previous(ev)
        return hook

    def _flush_engine_events(self, engine, start: int, decision_id=None) -> int:
        for ev in engine.events[start:]:
            data = dict(ev)
            if data["type"] == "action" and decision_id is not None:
                data["decision_id"] = decision_id
                decision_id = None
            self._emit(data.pop("type"), **data)
        return len(engine.events)

    # -- running ------------------------------------------------------------

    def _seat_meta(self) -> list:
        out = []
        for i, (bid, seat) in enumerate(self.seats.items()):
            path = getattr(seat, "bot_path", None)
            out.append({
                "seat": i, "bot_id": bid, "kind": seat.kind, "path": path,
                "dir": self.writer.dir_name(bid) if self.writer else None,
                "version": bot_fingerprint(path) if path else None,
                "timeout": getattr(seat, "timeout", None),
                "docker": bool(getattr(seat, "docker_image", None)),
            })
        return out

    def _write_meta(self, status: str, started: float, result: Optional[dict] = None):
        if not self.writer:
            return
        meta = {
            "created": started, "status": status,
            "harness_version": poker_harness.__version__,
            "config": self.config.to_dict(), "seats": self._seat_meta(),
            "labels": self.labels,
        }
        if result is not None:
            meta["result"] = {k: v for k, v in result.items() if k != "hands"}
        self.writer.write_meta(meta)

    async def run(self) -> dict:
        started = time.time()
        for bid, seat in self.seats.items():
            if hasattr(seat, "on_event"):
                seat.on_event = self._seat_event_hook(bid, seat.on_event)
            if self.writer and isinstance(seat, SubprocessBotSeat) and seat.stderr_path is None:
                seat.stderr_path = self.writer.stderr_path(bid)

        self._write_meta("running", started)
        self._emit("match_start", config=self.config.to_dict(), seats=self._seat_meta(),
                   harness_version=poker_harness.__version__)

        reason, error = "hands_complete", None
        try:
            await asyncio.gather(*(s.start() for s in self.seats.values()))
            for bid, seat in self.seats.items():
                if getattr(seat, "status", "ready") not in ("ready", "new"):
                    self.errors[bid].append(f"{seat.status}: {seat.status_detail}")

            dealer = None
            for hand_num in range(self.config.n_hands):
                if sum(1 for b in self.bot_ids if self.stacks[b] > 0) < 2:
                    reason = "one_player_left"
                    break
                dealer = next_button([self.stacks[b] for b in self.bot_ids], dealer)
                await self._play_hand(hand_num, dealer)
        except BaseException as e:
            reason, error = "error", f"{type(e).__name__}: {e}"
            raise
        finally:
            self._hand_num = None
            await asyncio.gather(*(s.close() for s in self.seats.values()),
                                 return_exceptions=True)
            result = self._result(started, reason, error)
            self._emit("match_end", **{k: v for k, v in result.items()
                                       if k not in ("hands", "run_dir", "bot_events")})
            self._write_meta("error" if error else "complete", started, result)
            if self.writer:
                self.writer.close()
        return result

    async def _play_hand(self, hand_num: int, dealer: int) -> None:
        cfg = self.config
        self._hand_num = hand_num
        engine = PokerEngine(
            f"{self.match_id}_h{hand_num:04d}", self.bot_ids,
            dealer_seat     = dealer,
            starting_stacks = dict(self.stacks),
            seed            = hand_seed(cfg.seed, hand_num),
            small_blind     = cfg.small_blind,
            big_blind       = cfg.big_blind,
            hand_num        = hand_num,
        )
        self._emit("hand_start",
                   hand_id     = engine.hand_id,
                   dealer_seat = dealer,
                   blinds      = [cfg.small_blind, cfg.big_blind],
                   bot_ids     = self.bot_ids,
                   stacks      = [self.stacks[b] for b in self.bot_ids],
                   positions   = {str(s): p for s, p in engine.positions().items()},
                   hole_cards  = {str(s): c for s, c in engine.hole_plan.items()},
                   board_plan  = engine.board_plan)

        state = engine.start_hand()
        seen  = self._flush_engine_events(engine, 0)
        steps = 0
        while state["type"] == "action_request":
            seat   = state["seat_to_act"]
            bot_id = self.bot_ids[seat]
            self._decision_id += 1
            did = self._decision_id

            sent = dict(state)
            sent["match_action_log"] = self._match_log[-cfg.match_log_entries:]
            decision = await self.seats[bot_id].act(sent)

            state   = engine.apply_action(seat, decision.action)
            applied = engine.action_log[-1]
            seen    = self._flush_engine_events(engine, seen, decision_id=did)

            if decision.error:
                self.errors[bot_id].append(decision.error)
            if self.writer:
                record_state = {k: v for k, v in sent.items() if k != "match_action_log"}
                record_state["match_action_log_len"] = len(sent["match_action_log"])
                self.writer.decision(bot_id, {
                    "decision_id": did, "hand_num": hand_num, "seat": seat,
                    "bot_id": bot_id, "street": sent["street"], "state": record_state,
                    "response": decision.action,
                    "applied": {"action": applied["action"], "amount": applied["amount"]},
                    "corrected": decision.error is None and _corrected(decision.action, applied),
                    **{k: v for k, v in decision.to_dict().items() if k != "action"},
                })
            if self.verbose:
                note = f" ({decision.error})" if decision.error else ""
                print(f"  [{bot_id}] {applied['action']} {applied['amount'] or ''}{note}",
                      file=sys.stderr)

            self._match_log.append({"hand_num": hand_num, "seat": seat, "bot_id": bot_id,
                                    "action": applied["action"], "amount": applied["amount"]})
            steps += 1
            if steps > 1000:
                raise RuntimeError(f"hand {engine.hand_id} exceeded 1000 actions")

        for bid, s in state["final_stacks"].items():
            delta = s - self.stacks[bid]
            if self.config.reset_stacks:
                self.totals[bid] += delta
            else:
                self.stacks[bid] = s
            state.setdefault("delta", {})[bid] = delta
        self._emit("hand_end",
                   showdown       = state["showdown"],
                   pot            = state["pot"],
                   board          = state["community_cards"],
                   winners        = state["winners"],
                   uncalled       = state["uncalled"],
                   hand_strengths = state["hand_strengths"],
                   final_stacks   = state["final_stacks"],
                   delta          = state["delta"])
        self.hands_played += 1
        if self.keep_hands:
            self.hands.append({"hand_num": hand_num, "hand_id": engine.hand_id, **state})

    def _result(self, started: float, reason: str, error: Optional[str]) -> dict:
        start = {b: self.config.stacks.get(b, self.config.starting_stack) for b in self.bot_ids}
        return {
            "match_id":     self.match_id,
            "bot_ids":      self.bot_ids,
            "seed":         self.config.seed,
            "n_hands":      self.hands_played,
            "duration_s":   round(time.time() - started, 2),
            "end_reason":   reason,
            "error":        error,
            "final_stacks": {b: start[b] + self.totals[b] for b in self.bot_ids}
                            if self.config.reset_stacks else dict(self.stacks),
            "chip_delta":   {b: self.totals[b] for b in self.bot_ids}
                            if self.config.reset_stacks
                            else {b: self.stacks[b] - start[b] for b in self.bot_ids},
            "bot_errors":   self.errors,
            "error_counts": {b: len(e) for b, e in self.errors.items()},
            "restarts":     self.restarts,
            "decisions":    self._decision_id,
            "run_dir":      str(self.writer.dir) if self.writer else None,
            "hands":        self.hands,
        }

def make_bot_seats(paths: dict, timeout: Optional[float] = 2.0,
                   docker_image: Optional[str] = None, **kw) -> dict:
    """{bot_id: path} -> {bot_id: SubprocessBotSeat}"""
    return {bid: SubprocessBotSeat(bid, path, timeout=timeout,
                                   docker_image=docker_image, **kw)
            for bid, path in paths.items()}


def run_match_sync(match_id: str, seats: dict, config: Optional[MatchConfig] = None,
                   **kw) -> dict:
    return asyncio.run(MatchRunner(match_id, seats, config, **kw).run())


def new_match_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
