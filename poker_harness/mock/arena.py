"""
MockArena: an in-memory implementation of the arena bot API
(docs/bot-api.md), for testing bots and the bridge locally. The HTTP
layer is app.py; `arena serve-mock` runs it.

It follows the spec's semantics: tokens bound to bots, online presence,
challenges, seeks, matches run by the engine through MatchRunner, a
remote seat per connected bot (decide messages, clocks with a time bank,
fallback actions), and per-seat visibility (own cards, public actions,
showdowns only). Unlike production it keeps no accounts, applies no rate
limits, and lets house bots fill tables.

Every match is also written to the run store with the full god view, as
the real arena does, so `arena match show/hand/verify` and `arena brief`
work on mock matches.
"""

import asyncio
import itertools
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from poker_harness.engine.game import normalize_action
from poker_harness.match import MatchConfig, MatchRunner
from poker_harness.mock.house import POLICIES, make_house_seat
from poker_harness.protocol import models as m
from poker_harness.runs import RunWriter
from poker_harness.seats import Decision, Seat, fallback_action

log = logging.getLogger("arena.mock")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Bot:
    name: str
    token: Optional[str] = None          # None for house bots
    house_spec: Optional[str] = None     # bot path for file house bots
    house: bool = False
    event_queue: Optional[asyncio.Queue] = None
    created: float = field(default_factory=time.time)

    @property
    def online(self) -> bool:
        return self.house or self.event_queue is not None


@dataclass
class Pending:
    decide: m.Decide
    future: asyncio.Future
    sent_at: float
    body: Optional[dict] = None


class RemoteSeat(Seat):
    """A seat played by a bot connected over the API."""
    kind = "remote"

    def __init__(self, live: "LiveMatch", seat: int, bot: Bot):
        super().__init__(bot.name)
        self.live, self.seat, self.bot = live, seat, bot
        self.bank = live.format.clock.bank_s
        self.queues: list = []
        self.pending: Optional[Pending] = None
        self.left = False
        self.decisions = itertools.count(1)
        # about the latest decision, for its decision_result
        self.last_id: Optional[str] = None
        self.last_sent: Optional[dict] = None
        self.last_error: Optional[str] = None

    @property
    def connected(self) -> bool:
        return bool(self.queues)

    async def act(self, state, timeout=None) -> Decision:
        """Send `decide` and wait for the POST. The clock: decision_s, then
        the bank. A disconnected bot gets decision_s (no bank) to come back;
        a bot that left is acted for at once."""
        clock = self.live.format.clock
        self.last_id = f"{self.live.id}:s{self.seat}d{next(self.decisions)}"
        self.last_sent = self.last_error = None
        decide = m.Decide(type="decide", decision_id=self.last_id, hand_num=state["hand_num"],
                          decision_s=clock.decision_s, bank_s=round(self.bank, 3), state=state)
        if self.left:
            return self._fallback(state, "disconnected")
        loop = asyncio.get_running_loop()
        self.pending = Pending(decide, loop.create_future(), time.monotonic())
        self.live.send(self.seat, decide)
        limit = clock.decision_s + (self.bank if self.connected else 0.0)
        try:
            done, _ = await asyncio.wait({self.pending.future}, timeout=limit)
            elapsed = time.monotonic() - self.pending.sent_at
            body = self.pending.future.result() if done else None
            if body is None:
                # no answer in time, or the bot left while we waited
                if self.connected and not self.left:
                    self.bank = 0.0
                    return self._fallback(state, "timeout", elapsed)
                return self._fallback(state, "disconnected", elapsed)
            self.bank = max(0.0, self.bank - max(0.0, elapsed - clock.decision_s))
            self.last_sent = body["action"]
            return Decision(body["action"], logs=body.get("logs", []),
                            elapsed_ms=round(elapsed * 1000, 1))
        finally:
            self.pending = None

    def _fallback(self, state, error, elapsed=0.0) -> Decision:
        self.last_error = error
        return Decision(fallback_action(state), error=error, elapsed_ms=round(elapsed * 1000, 1))


class LiveMatch:
    """A running match: its seats, subscribers and per-seat history."""

    def __init__(self, arena: "MockArena", match_id: str, fmt: m.Format, rated: bool,
                 bots: list, challenge_id=None, seek_ids=None):
        self.arena, self.id, self.format, self.rated = arena, match_id, fmt, rated
        self.bots = bots
        self.challenge_id = challenge_id
        self.seek_ids = seek_ids or {}               # bot name -> seek id
        self.seats = {}                              # seat -> Seat
        self.remote = {}                             # seat -> RemoteSeat
        for i, bot in enumerate(bots):
            if bot.house:
                self.seats[i] = make_house_seat(bot.name, bot.house_spec)
            else:
                self.seats[i] = self.remote[i] = RemoteSeat(self, i, bot)
        self.hand_log = {i: [] for i in range(len(bots))}   # messages of the hand in progress
        self.stacks = [fmt.stack] * len(bots)
        self.hands_played = 0
        self.status = "started"
        self._showdown = {}
        self._uncalled = None
        self.task: Optional[asyncio.Task] = None

    # -- references and messages -------------------------------------------------

    def ref(self, seat: Optional[int] = None) -> m.MatchRef:
        return m.MatchRef(
            id=self.id, format=self.format, rated=self.rated, your_seat=seat, status=self.status,
            seats=[m.SeatInfo(seat=i, bot=self.arena.bot_ref(b)) for i, b in enumerate(self.bots)],
            challenge_id=self.challenge_id,
            seek_id=self.seek_ids.get(self.bots[seat].name) if seat is not None else None)

    def send(self, seat: int, msg) -> None:
        data = msg.model_dump(mode="json", exclude_none=False)
        if data["type"] == "hand_start":
            self.hand_log[seat] = []
        if data["type"] not in ("decide", "match_end", "match_full"):
            self.hand_log[seat].append(data)
        rs = self.remote.get(seat)
        if rs:
            for q in rs.queues:
                q.put_nowait(data)

    def broadcast(self, msg) -> None:
        for seat in range(len(self.bots)):
            self.send(seat, msg)

    def full(self, seat: int) -> m.MatchFull:
        rs = self.remote[seat]
        return m.MatchFull(type="match_full", match=self.ref(seat), hands_played=self.hands_played,
                           stacks=self.stacks, bank_s=round(rs.bank, 3),
                           hand=list(self.hand_log[seat]),
                           pending=rs.pending.decide if rs.pending else None)

    # -- engine events -> per-seat messages ---------------------------------------

    def on_event(self, ev: dict) -> None:
        t, hn = ev["type"], ev.get("hand_num")
        names = [b.name for b in self.bots]
        if t == "hand_start":
            self._showdown, self._uncalled = {}, None
            for seat in range(len(self.bots)):
                self.send(seat, m.HandStart(
                    type="hand_start", hand_num=hn, hand_id=ev["hand_id"], dealer_seat=ev["dealer_seat"],
                    positions=ev["positions"], blinds=ev["blinds"], stacks=ev["stacks"],
                    your_cards=ev["hole_cards"].get(str(seat), [])))
        elif t == "blind":
            self.broadcast(m.Blind(type="blind", hand_num=hn, seat=ev["seat"], bot=ev["bot_id"],
                                   kind=ev["action"], amount=ev["amount"]))
        elif t == "street_start" and ev["street"] != "preflop":
            self.broadcast(m.StreetStart(type="street", hand_num=hn, street=ev["street"],
                                         board=ev["community_cards"]))
        elif t == "action":
            self.broadcast(m.ActionMessage(
                type="action", hand_num=hn, street=ev["street"], seat=ev["seat"], bot=ev["bot_id"],
                action=ev["action"], amount=ev["amount"], pot_after=ev["pot_after"],
                stacks=[ev["stacks"][n] for n in names]))
            rs = self.remote.get(ev["seat"])
            if rs and ev.get("decision_id") is not None:
                self._decision_result(rs, ev)
        elif t == "uncalled_bet_returned":
            self._uncalled = m.Uncalled(seat=ev["seat"], bot=ev["bot_id"], amount=ev["amount"])
        elif t == "showdown":
            self._showdown = {str(names.index(b)): cards for b, cards in ev["revealed"].items()}
        elif t == "hand_end":
            self.hands_played += 1
            self.stacks = [ev["final_stacks"][n] for n in names]
            self.broadcast(m.HandEnd(
                type="hand_end", hand_num=hn, board=ev["board"], pot=ev["pot"], showdown=ev["showdown"],
                winners=[m.Winner(seat=w["seat"], bot=w["bot_id"], amount=w["amount"],
                                  pot_type=w["pot_type"]) for w in ev["winners"]],
                uncalled=self._uncalled, revealed=self._showdown if ev["showdown"] else {},
                stacks=self.stacks, delta=[ev["delta"][n] for n in names]))

    def _decision_result(self, rs: RemoteSeat, ev: dict) -> None:
        applied = {"action": ev["action"], "amount": ev["amount"] if ev["action"] == "raise" else None}
        sent = rs.last_sent
        corrected = sent is not None and (sent.get("action") != applied["action"] or
                                          (applied["action"] == "raise" and sent.get("amount") != ev["amount"]))
        self.send(rs.seat, m.DecisionResult(
            type="decision_result", decision_id=rs.last_id or "", applied=m.ActionBody(**applied),
            corrected=corrected, error=rs.last_error, bank_s=round(rs.bank, 3)))

    # -- running -------------------------------------------------------------------

    async def run(self) -> None:
        arena = self.arena
        for seat, rs in self.remote.items():
            arena.notify(rs.bot, m.MatchStartEvent(type="match_start", match=self.ref(seat)))
        deadline = time.monotonic() + arena.connect_timeout_s
        while any(not rs.connected for rs in self.remote.values()):
            if time.monotonic() > deadline:
                missing = [rs.bot.name for rs in self.remote.values() if not rs.connected]
                log.info(f"match {self.id} aborted: {', '.join(missing)} never connected")
                self.status = "aborted"
                self._finish(m.MatchEnd(type="match_end", reason="aborted", hands_played=0,
                                        stacks=self.stacks, chip_delta={b.name: 0 for b in self.bots}))
                return
            await asyncio.sleep(0.05)

        fmt = self.format
        config = MatchConfig(n_hands=fmt.hands, small_blind=fmt.small_blind, big_blind=fmt.big_blind,
                             starting_stack=fmt.stack, reset_stacks=fmt.reset_stacks, ranked=self.rated)
        writer = RunWriter(self.id, arena.runs_root) if arena.runs_root else None
        runner = MatchRunner(self.id, {b.name: self.seats[i] for i, b in enumerate(self.bots)},
                             config, writer=writer, on_event=self.on_event,
                             labels={"mock_arena": True, "challenge_id": self.challenge_id})
        try:
            result = await runner.run()
            reason = result["end_reason"] if result["end_reason"] != "error" else "aborted"
            self._finish(m.MatchEnd(type="match_end", reason=reason, hands_played=result["n_hands"],
                                    stacks=self.stacks, chip_delta=result["chip_delta"]))
        except Exception as e:
            log.exception(f"match {self.id} failed: {e}")
            self.status = "aborted"
            self._finish(m.MatchEnd(type="match_end", reason="aborted", hands_played=self.hands_played,
                                    stacks=self.stacks, chip_delta={b.name: 0 for b in self.bots}))

    def _finish(self, end: m.MatchEnd) -> None:
        if self.status == "started":
            self.status = "finished"
        self.broadcast(end)
        for rs in self.remote.values():
            for q in rs.queues:
                q.put_nowait(None)                       # close match streams
            self.arena.notify(rs.bot, m.MatchFinishEvent(type="match_finish", match=self.ref(rs.seat)))

    # -- decisions -------------------------------------------------------------------

    def post_decision(self, bot: Bot, body: m.DecisionRequest) -> m.DecisionAccepted:
        seat = next((i for i, rs in self.remote.items() if rs.bot is bot), None)
        if seat is None:
            raise ApiError(403, "not_your_match", f"{bot.name} isn't playing {self.id}")
        rs = self.remote[seat]
        p = rs.pending
        if p is None or p.decide.decision_id != body.decision_id or p.future.done():
            raise ApiError(409, "decision_stale", f"decision {body.decision_id} is not pending")
        raw = body.action.model_dump(mode="json", exclude_none=True)
        applied = normalize_action(p.decide.state.model_dump(mode="json"), raw)
        p.future.set_result({"action": raw, "logs": [l.model_dump(mode="json") for l in body.logs]})
        corrected = (applied["action"] != raw["action"] or
                     (raw["action"] == "raise" and applied["amount"] != raw.get("amount")))
        return m.DecisionAccepted(applied=m.ActionBody(
            action=applied["action"], amount=applied["amount"] if applied["action"] == "raise" else None),
            corrected=corrected)


class MockArena:
    def __init__(self, *, runs_root=None, connect_timeout_s: float = 60.0,
                 challenge_ttl_s: float = 60.0, seek_ttl_s: float = 600.0,
                 house_fill_after_s: float = 2.0, house: Optional[dict] = None):
        self.bots: dict = {}                 # name -> Bot
        self.tokens: dict = {}               # token -> Bot
        self.challenges: dict = {}           # id -> (Challenge, created)
        self.seeks: dict = {}                # id -> (Seek, Bot, created)
        self.matches: dict = {}              # id -> LiveMatch
        self.runs_root = runs_root
        self.connect_timeout_s = connect_timeout_s
        self.challenge_ttl_s = challenge_ttl_s
        self.seek_ttl_s = seek_ttl_s
        self.house_fill_after_s = house_fill_after_s
        self._ids = itertools.count(1)
        for name, spec in (house if house is not None else {n: None for n in POLICIES}).items():
            self.bots[name] = Bot(name, house=True, house_spec=spec)
        self._janitor: Optional[asyncio.Task] = None

    # -- bots ------------------------------------------------------------------------

    def register(self, name: str, token: Optional[str] = None) -> str:
        if name in self.bots:
            raise ValueError(f"bot {name!r} already exists")
        token = token or "mock_" + secrets.token_urlsafe(12)
        bot = Bot(name, token=token)
        self.bots[name] = bot
        self.tokens[token] = bot
        return token

    def auth(self, header: Optional[str]) -> Bot:
        token = (header or "").removeprefix("Bearer ").strip()
        bot = self.tokens.get(token)
        if not bot:
            raise ApiError(401, "bad_token", "missing or unknown token")
        return bot

    def bot_ref(self, bot: Bot) -> m.BotRef:
        return m.BotRef(name=bot.name)

    def account(self, bot: Bot) -> m.Account:
        return m.Account(name=bot.name, owner="mock", created_at=time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(bot.created)), scopes=["bot:play", "bot:read"])

    def online(self) -> m.OnlineBots:
        return m.OnlineBots(bots=[self.bot_ref(b) for b in self.bots.values() if b.online])

    def playing(self, bot: Bot) -> list:
        return [lm.ref(i) for lm in self.matches.values() if lm.status == "started"
                for i, rs in lm.remote.items() if rs.bot is bot]

    def notify(self, bot: Bot, event) -> None:
        if bot.event_queue is not None:
            bot.event_queue.put_nowait(event.model_dump(mode="json"))

    # -- streams -----------------------------------------------------------------------

    def open_events(self, bot: Bot) -> asyncio.Queue:
        if bot.event_queue is not None:
            bot.event_queue.put_nowait(None)                 # a new connection replaces the old
        q = asyncio.Queue()
        bot.event_queue = q
        self._start_janitor()
        for ref in self.playing(bot):
            q.put_nowait(m.MatchStartEvent(type="match_start", match=ref).model_dump(mode="json"))
        return q

    def close_events(self, bot: Bot, q: asyncio.Queue) -> None:
        if bot.event_queue is q:
            bot.event_queue = None
            for sid, (seek, owner, _) in list(self.seeks.items()):
                if owner is bot:
                    self.seeks.pop(sid, None)

    def open_match(self, bot: Bot, match_id: str) -> asyncio.Queue:
        lm = self._match(match_id)
        seat = next((i for i, rs in lm.remote.items() if rs.bot is bot), None)
        if seat is None:
            raise ApiError(403, "not_your_match", f"{bot.name} isn't playing {match_id}")
        q = asyncio.Queue()
        rs = lm.remote[seat]
        if lm.status != "started":
            q.put_nowait(lm.full(seat).model_dump(mode="json"))
            q.put_nowait(None)
            return q
        was_connected = rs.connected
        rs.queues.append(q)
        q.put_nowait(lm.full(seat).model_dump(mode="json"))
        if not was_connected:
            for i in lm.remote:
                if i != seat:
                    lm.send(i, m.SeatStatus(type="seat_status", seat=seat, bot=bot.name, connected=True))
        return q

    def close_match(self, bot: Bot, match_id: str, q: asyncio.Queue) -> None:
        lm = self.matches.get(match_id)
        if not lm:
            return
        for seat, rs in lm.remote.items():
            if q in rs.queues:
                rs.queues.remove(q)
                if not rs.connected and lm.status == "started":
                    for i in lm.remote:
                        if i != seat:
                            lm.send(i, m.SeatStatus(type="seat_status", seat=seat, bot=bot.name,
                                                    connected=False))

    def _match(self, match_id: str) -> LiveMatch:
        lm = self.matches.get(match_id)
        if not lm:
            raise ApiError(404, "not_found", f"no match {match_id}")
        return lm

    # -- challenges ----------------------------------------------------------------------

    def challenge(self, bot: Bot, dest_name: str, req: m.ChallengeRequest) -> m.Challenge:
        dest = self.bots.get(dest_name)
        if dest is None:
            raise ApiError(404, "not_found", f"no bot {dest_name}")
        if dest is bot:
            raise ApiError(400, "invalid_body", "you can't challenge yourself")
        if req.format.seats != 2:
            raise ApiError(400, "invalid_format", "challenges are heads-up; use a seek for 3+ seats")
        cid = f"ch_{next(self._ids)}"
        ch = m.Challenge(id=cid, challenger=self.bot_ref(bot), dest=self.bot_ref(dest), format=req.format,
                         rated=req.rated, status="created",
                         expires_at=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime(time.time() + self.challenge_ttl_s)))
        self.challenges[cid] = (ch, time.monotonic())
        self.notify(bot, m.ChallengeEvent(type="challenge", challenge=ch))
        if dest.house:
            self._accept(ch)                                  # house bots always accept
        else:
            self.notify(dest, m.ChallengeEvent(type="challenge", challenge=ch))
        return ch

    def _get_challenge(self, cid: str) -> m.Challenge:
        if cid not in self.challenges:
            raise ApiError(404, "not_found", f"no challenge {cid}")
        ch, _ = self.challenges[cid]
        if ch.status != "created":
            raise ApiError(409, "challenge_closed", f"challenge {cid} is {ch.status}")
        return ch

    def accept(self, bot: Bot, cid: str) -> None:
        ch = self._get_challenge(cid)
        if ch.dest.name != bot.name:
            raise ApiError(403, "not_your_challenge", "only the challenged bot can accept")
        self._accept(ch)

    def _accept(self, ch: m.Challenge) -> None:
        ch.status = "accepted"
        challenger, dest = self.bots[ch.challenger.name], self.bots[ch.dest.name]
        self._start([challenger, dest], ch.format, ch.rated, challenge_id=ch.id)

    def decline(self, bot: Bot, cid: str, reason: str) -> None:
        ch = self._get_challenge(cid)
        if ch.dest.name != bot.name:
            raise ApiError(403, "not_your_challenge", "only the challenged bot can decline")
        ch.status = "declined"
        self.notify(self.bots[ch.challenger.name],
                    m.ChallengeDeclinedEvent(type="challenge_declined", challenge=ch, reason=reason))

    def cancel(self, bot: Bot, cid: str) -> None:
        ch = self._get_challenge(cid)
        if ch.challenger.name != bot.name:
            raise ApiError(403, "not_your_challenge", "only the challenger can cancel")
        ch.status = "canceled"
        self.notify(self.bots[ch.dest.name], m.ChallengeCanceledEvent(type="challenge_canceled", challenge=ch))

    # -- seeks ---------------------------------------------------------------------------

    def seek(self, bot: Bot, req: m.SeekRequest) -> m.Seek:
        if any(owner is bot and s.format == req.format for s, owner, _ in self.seeks.values()):
            raise ApiError(409, "already_seeking", "you already have a seek for this format")
        sid = f"sk_{next(self._ids)}"
        seek = m.Seek(id=sid, format=req.format, rated=req.rated, status="open")
        self.seeks[sid] = (seek, bot, time.monotonic())
        self._match_seeks()
        return seek

    def cancel_seek(self, bot: Bot, sid: str) -> None:
        entry = self.seeks.get(sid)
        if not entry or entry[1] is not bot:
            raise ApiError(404, "not_found", f"no seek {sid}")
        self.seeks.pop(sid)

    def _match_seeks(self, fill_with_house: bool = False) -> None:
        groups = {}
        for sid, (seek, bot, created) in self.seeks.items():
            groups.setdefault((seek.format.model_dump_json(), seek.rated), []).append((sid, seek, bot, created))
        for (_, rated), entries in groups.items():
            fmt = entries[0][1].format
            entries.sort(key=lambda e: e[3])
            players, seen = [], set()
            for e in entries:
                if e[2].name not in seen:
                    players.append(e)
                    seen.add(e[2].name)
            while len(players) >= fmt.seats or (fill_with_house and players):
                table = players[:fmt.seats]
                players = players[fmt.seats:]
                bots = [e[2] for e in table]
                houses = [b for b in self.bots.values() if b.house and b not in bots]
                while len(bots) < fmt.seats and houses:
                    bots.append(houses.pop(0))
                if len(bots) < fmt.seats:
                    break
                for e in table:
                    e[1].status = "matched"
                    self.seeks.pop(e[0], None)
                self._start(bots, fmt, rated, seek_ids={e[2].name: e[0] for e in table})

    def _start(self, bots: list, fmt: m.Format, rated: bool, challenge_id=None, seek_ids=None) -> LiveMatch:
        mid = f"mock-{time.strftime('%H%M%S')}-{next(self._ids)}"
        lm = LiveMatch(self, mid, fmt, rated, bots, challenge_id=challenge_id, seek_ids=seek_ids)
        self.matches[mid] = lm
        lm.task = asyncio.get_event_loop().create_task(lm.run(), name=f"match-{mid}")
        log.info(f"match {mid}: {', '.join(b.name for b in bots)}")
        return lm

    # -- housekeeping ----------------------------------------------------------------------

    def _start_janitor(self) -> None:
        if self._janitor is None or self._janitor.done():
            self._janitor = asyncio.get_event_loop().create_task(self._janitor_loop())

    async def _janitor_loop(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            now = time.monotonic()
            for cid, (ch, created) in list(self.challenges.items()):
                if ch.status == "created" and now - created > self.challenge_ttl_s:
                    ch.status = "expired"
                    for name in (ch.challenger.name, ch.dest.name):
                        self.notify(self.bots[name], m.ChallengeCanceledEvent(
                            type="challenge_canceled", challenge=ch))
            for sid, (seek, bot, created) in list(self.seeks.items()):
                if now - created > self.seek_ttl_s:
                    self.seeks.pop(sid, None)
                    seek.status = "expired"
                    self.notify(bot, m.SeekExpiredEvent(type="seek_expired", seek=seek))
            if any(now - created > self.house_fill_after_s for _, _, created in self.seeks.values()):
                self._match_seeks(fill_with_house=True)

    def post_decision(self, bot: Bot, match_id: str, body: m.DecisionRequest) -> m.DecisionAccepted:
        return self._match(match_id).post_decision(bot, body)

    def leave(self, bot: Bot, match_id: str) -> None:
        lm = self._match(match_id)
        for rs in lm.remote.values():
            if rs.bot is bot:
                rs.left = True
                if rs.pending and not rs.pending.future.done():
                    rs.pending.future.set_result(None)     # act() falls back
                return
        raise ApiError(403, "not_your_match", f"{bot.name} isn't playing {match_id}")
