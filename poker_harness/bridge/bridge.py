"""
The bridge between the arena and a bot: lichess-bot's job, for poker.

    bridge = Bridge(config, client)
    await bridge.run()          # until stopped, or max_matches finish

- Keeps the bot's event stream open (reconnecting with backoff).
- Accepts or declines incoming challenges by the `challenge` config.
- Optionally challenges online bots (`matchmaking`) and queues for tables
  (`seek`) while it has free capacity.
- Plays each match in its own task with its own bot process: opens the
  match stream, answers every `decide` by running bot.py through the
  bot-process seat (isolation, timeouts, restarts), and posts the decision
  with the bot's ctx.log notes. Reconnects to the match stream as needed;
  a pending decision is answered exactly once.
- Records every match from the bot's own perspective (records.py).
- stop(): finish current matches, take no new ones. stop(now=True): leave
  current matches too.
"""

import asyncio
import contextlib
import logging
import random
import time
from pathlib import Path
from typing import Optional

from poker_harness.bridge.client import ArenaClient, ArenaError, StreamClosed, backoff_delays
from poker_harness.bridge.config import BridgeConfig
from poker_harness.bridge.records import MatchRecord
from poker_harness.protocol import models as m
from poker_harness.seats import SubprocessBotSeat

log = logging.getLogger("arena.connect")

MAX_LOGS = 200
MIN_BUDGET_S = 0.5


def describe(fmt: m.Format) -> str:
    kind = "heads-up" if fmt.seats == 2 else f"{fmt.seats}-max"
    return (f"{kind}, {fmt.hands} hands, {fmt.small_blind}/{fmt.big_blind}, "
            f"{fmt.clock.decision_s:g}s+{fmt.clock.bank_s:g}s")


def to_action_body(raw) -> m.ActionBody:
    """A bot's reply -> a valid ActionBody. Unknown actions become fold
    (the same as the engine would do locally); the arena corrects sizes."""
    raw = raw if isinstance(raw, dict) else {}
    act = str(raw.get("action", "fold")).lower().strip()
    if act not in ("fold", "check", "call", "raise", "all_in"):
        return m.ActionBody(action="fold")
    amount = raw.get("amount")
    try:
        amount = int(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = None
    if act != "raise":
        amount = None
    return m.ActionBody(action=act, amount=amount)


class Bridge:
    def __init__(self, config: BridgeConfig, client: ArenaClient):
        self.cfg = config
        self.client = client
        self.me: Optional[str] = None
        self.matches: dict = {}            # match id -> task
        self.players: dict = {}            # match id -> MatchPlayer
        self.accepted: dict = {}           # challenge id -> time accepted, until match_start
        self.outgoing: dict = {}           # challenge id -> (opponent, time sent)
        self.seeks: dict = {}              # seek id -> format
        self.declined_by: dict = {}        # bot -> time it declined us
        self.finished = 0
        self.stopping = False
        self._done = asyncio.Event()
        self._tasks: list = []

    # -- capacity -----------------------------------------------------------

    def busy(self) -> int:
        return len(self.matches) + len(self.accepted) + len(self.outgoing) + len(self.seeks)

    def free(self) -> int:
        return self.cfg.challenge.concurrency - self.busy()

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        account = await self.client.account()
        self.me = account.name
        log.info(f"connected as {self.me} (bot: {self.cfg.bot.path}, "
                 f"concurrency {self.cfg.challenge.concurrency})")
        self._tasks = [asyncio.create_task(self._event_loop(), name="events")]
        if self.cfg.matchmaking.enabled:
            self._tasks.append(asyncio.create_task(self._matchmaking_loop(), name="matchmaking"))
        if self.cfg.seek.enabled:
            self._tasks.append(asyncio.create_task(self._seek_loop(), name="seek"))
        try:
            await self._done.wait()
        finally:
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            if self.matches:
                await asyncio.gather(*self.matches.values(), return_exceptions=True)

    async def stop(self, now: bool = False) -> None:
        """Take no new matches and withdraw challenges and seeks. With now,
        also leave current matches; otherwise wait for them to finish."""
        if not self.stopping:
            log.info("stopping: finishing current matches" if not now else "stopping now")
        self.stopping = True
        for cid in list(self.outgoing):
            await self._quietly(self.client.cancel(cid))
        self.outgoing.clear()
        for sid in list(self.seeks):
            await self._quietly(self.client.cancel_seek(sid))
        self.seeks.clear()
        if now:
            for mid, task in list(self.matches.items()):
                if mid in self.players:
                    self.players[mid].left = True
                await self._quietly(self.client.leave(mid))
                task.cancel()
        self._maybe_done()

    def _maybe_done(self) -> None:
        if self.stopping and not self.matches:
            self._done.set()

    @staticmethod
    async def _quietly(coro) -> None:
        try:
            await coro
        except Exception as e:      # best effort while shutting down
            log.debug(f"ignored during shutdown: {e}")

    # -- event stream ---------------------------------------------------------

    async def _event_loop(self) -> None:
        delays = backoff_delays(self.cfg.backoff_base_s, self.cfg.backoff_max_s)
        while True:
            try:
                async with contextlib.aclosing(self.client.events()) as events:
                    async for event in events:
                        delays = backoff_delays(self.cfg.backoff_base_s, self.cfg.backoff_max_s)
                        await self._handle_event(event)
                raise StreamClosed("event stream ended")
            except StreamClosed as e:
                wait = next(delays)
                log.warning(f"event stream: {e}; reconnecting in {wait:.1f}s")
                await asyncio.sleep(wait)
            except ArenaError as e:
                if e.status in (401, 403):
                    log.error(f"the arena rejected the token: {e}")
                    self.stopping = True
                    self._done.set()
                    return
                wait = next(delays)
                log.warning(f"event stream: {e}; reconnecting in {wait:.1f}s")
                await asyncio.sleep(wait)

    async def _handle_event(self, event) -> None:
        if isinstance(event, m.ChallengeEvent):
            ch = event.challenge
            if ch.challenger.name == self.me:
                return                                  # our own challenge
            await self._answer_challenge(ch)
        elif isinstance(event, m.ChallengeDeclinedEvent):
            ch = event.challenge
            if self.outgoing.pop(ch.id, None):
                self.declined_by[ch.dest.name] = time.monotonic()
                log.info(f"{ch.dest.name} declined our challenge ({event.reason})")
        elif isinstance(event, m.ChallengeCanceledEvent):
            self.accepted.pop(event.challenge.id, None)
            self.outgoing.pop(event.challenge.id, None)
        elif isinstance(event, m.SeekExpiredEvent):
            self.seeks.pop(event.seek.id, None)
        elif isinstance(event, m.MatchStartEvent):
            self._start_match(event.match)
        elif isinstance(event, m.MatchFinishEvent):
            log.debug(f"match {event.match.id} finished (event stream)")
        else:
            log.debug(f"ignoring event {getattr(event, 'type', event)!r}")

    def decline_reason(self, ch: m.Challenge) -> Optional[str]:
        """None to accept, or the reason to decline (lichess-bot's filters)."""
        c = self.cfg.challenge
        who = ch.challenger.name
        if not c.accept or who in c.block_list or (c.allow_list and who not in c.allow_list):
            return "generic"
        if self.stopping:
            return "later"
        if self.free() <= 0:
            return "too_many_matches"
        if ("rated" if ch.rated else "casual") not in c.modes:
            return "rated" if ch.rated else "casual"
        f = ch.format
        if (f.seats not in c.seats or not c.min_hands <= f.hands <= c.max_hands
                or f.clock.decision_s < c.min_decision_s):
            return "format"
        return None

    async def _answer_challenge(self, ch: m.Challenge) -> None:
        reason = self.decline_reason(ch)
        try:
            if reason:
                log.info(f"declining {ch.challenger.name}'s challenge ({describe(ch.format)}): {reason}")
                await self.client.decline(ch.id, reason)
            else:
                log.info(f"accepting {ch.challenger.name}'s challenge ({describe(ch.format)})")
                self.accepted[ch.id] = time.monotonic()
                await self.client.accept(ch.id)
        except ArenaError as e:
            self.accepted.pop(ch.id, None)
            log.warning(f"challenge {ch.id}: {e}")

    # -- matchmaking and seeks ------------------------------------------------

    async def _matchmaking_loop(self) -> None:
        mm = self.cfg.matchmaking
        await asyncio.sleep(min(mm.interval_s, 5))
        while not self.stopping:
            try:
                self._expire_outgoing()
                if self.free() > 0 and not self.outgoing:
                    await self._challenge_someone()
            except ArenaError as e:
                log.warning(f"matchmaking: {e}")
            await asyncio.sleep(mm.interval_s)

    def _expire_outgoing(self) -> None:
        now = time.monotonic()
        for cid, (opp, sent) in list(self.outgoing.items()):
            if now - sent > self.cfg.matchmaking.challenge_timeout_s:
                log.info(f"{opp} didn't answer our challenge; canceling")
                self.outgoing.pop(cid, None)
                asyncio.create_task(self._quietly(self.client.cancel(cid)))

    async def _challenge_someone(self) -> None:
        mm = self.cfg.matchmaking
        now = time.monotonic()
        bots = [b.name for b in await self.client.online_bots()]
        candidates = [b for b in bots
                      if b != self.me and b not in mm.block_list
                      and (not mm.opponents or b in mm.opponents)
                      and now - self.declined_by.get(b, -1e9) > mm.decline_backoff_s]
        if not candidates:
            log.debug("matchmaking: no suitable bot online")
            return
        opponent = random.choice(candidates)
        fmt = random.choice(mm.formats)
        ch = await self.client.challenge(opponent, m.ChallengeRequest(format=fmt, rated=mm.rated))
        self.outgoing[ch.id] = (opponent, time.monotonic())
        log.info(f"challenged {opponent} ({describe(fmt)})")

    async def _seek_loop(self) -> None:
        sk = self.cfg.seek
        while not self.stopping:
            for fmt in sk.formats:
                if self.free() <= 0 or self.stopping:
                    break
                if fmt in self.seeks.values():
                    continue
                try:
                    seek = await self.client.seek(m.SeekRequest(format=fmt, rated=sk.rated))
                    self.seeks[seek.id] = fmt
                    log.info(f"seeking a table ({describe(fmt)})")
                except ArenaError as e:
                    log.warning(f"seek: {e}")
            await asyncio.sleep(max(1.0, self.cfg.backoff_base_s))

    # -- matches --------------------------------------------------------------

    def _start_match(self, match: m.MatchRef) -> None:
        if match.challenge_id:
            self.accepted.pop(match.challenge_id, None)
            self.outgoing.pop(match.challenge_id, None)
        if match.seek_id:
            self.seeks.pop(match.seek_id, None)
        if match.id in self.matches:
            return                                      # already playing (event stream reconnect)
        opponents = [s.bot.name for s in match.seats if s.seat != match.your_seat]
        log.info(f"match {match.id} started: {describe(match.format)} vs {', '.join(opponents)}")
        task = asyncio.create_task(self._play(match), name=f"match-{match.id}")
        self.matches[match.id] = task
        task.add_done_callback(lambda t, mid=match.id: self._match_done(mid, t))

    def _match_done(self, match_id: str, task: asyncio.Task) -> None:
        self.matches.pop(match_id, None)
        self.players.pop(match_id, None)
        self.finished += 1
        if not task.cancelled() and task.exception():
            log.error(f"match {match_id} failed: {task.exception()!r}")
        if self.cfg.max_matches and self.finished >= self.cfg.max_matches and not self.stopping:
            log.info(f"played {self.finished} match(es); stopping (max_matches)")
            asyncio.create_task(self.stop())
        self._maybe_done()

    async def _play(self, match: m.MatchRef) -> None:
        player = self.players[match.id] = MatchPlayer(self, match)
        await player.run()


class MatchPlayer:
    """Plays one match: one bot process, one match stream (reconnecting)."""

    def __init__(self, bridge: Bridge, match: m.MatchRef):
        self.b = bridge
        self.match = match
        self.answered: set = set()
        self.left = False                  # set by Bridge.stop(now=True)
        self.record = MatchRecord(Path(bridge.cfg.records.dir), match, bridge.me, bridge.cfg.bot.path)
        bot = bridge.cfg.bot
        self.seat = SubprocessBotSeat(bridge.me, bot.path, timeout=None,
                                      docker_image=bot.image if bot.docker else None,
                                      stderr_path=self.record.stderr_path)
        self.chips = 0

    async def run(self) -> None:
        await self.seat.start()
        if self.seat.status != "ready":
            log.error(f"match {self.match.id}: bot failed to start ({self.seat.status_detail}); "
                      "the arena will check/fold for it")
        delays = backoff_delays(self.b.cfg.backoff_base_s, self.b.cfg.backoff_max_s)
        ended, status = None, "finished"
        try:
            while ended is None and not self.left:
                try:
                    async with contextlib.aclosing(self.b.client.match_stream(self.match.id)) as stream:
                        async for msg in stream:
                            self.record.message(msg)
                            ended = await self._handle(msg)
                            if ended is not None:
                                break
                    if ended is None:
                        raise StreamClosed("match stream ended")
                except StreamClosed as e:
                    if ended is not None or self.left:
                        break
                    wait = next(delays)
                    log.warning(f"match {self.match.id}: {e}; reconnecting in {wait:.1f}s")
                    await asyncio.sleep(wait)
                except ArenaError as e:
                    if e.status == 404:
                        log.warning(f"match {self.match.id} is gone: {e}")
                        status = "gone"
                        break
                    raise
        except asyncio.CancelledError:
            status = "left"
            raise
        finally:
            if self.left:
                status = "left"
            self.record.finish(ended, status=status)
            self.record.close()
            await self.seat.close()
        if ended is not None:
            mine = ended.chip_delta.get(self.b.me)
            log.info(f"match {self.match.id} over ({ended.reason}, {ended.hands_played} hands): "
                     + (f"{mine:+,} chips" if mine is not None else "no result for us"))

    async def _handle(self, msg):
        if isinstance(msg, m.MatchFull):
            if msg.pending is not None:
                await self._decide(msg.pending)
        elif isinstance(msg, m.Decide):
            await self._decide(msg)
        elif isinstance(msg, m.HandEnd):
            if self.match.your_seat is not None and self.match.your_seat < len(msg.delta):
                self.chips += msg.delta[self.match.your_seat]
                log.debug(f"match {self.match.id} hand {msg.hand_num}: "
                          f"{msg.delta[self.match.your_seat]:+,} (total {self.chips:+,})")
        elif isinstance(msg, m.MatchEnd):
            return msg
        return None

    async def _decide(self, d: m.Decide) -> None:
        if d.decision_id in self.answered:
            return
        self.answered.add(d.decision_id)
        budget = max(MIN_BUDGET_S, d.decision_s + d.bank_s - self.b.cfg.bot.time_margin_s)
        state = d.state.model_dump(mode="json")
        decision = await self.seat.act(state, timeout=budget)
        logs = list(decision.logs[:MAX_LOGS])
        if decision.error and len(logs) < MAX_LOGS:
            logs.append({"msg": f"[bridge] bot {decision.error}; sent the fallback action",
                         "data": {"detail": (decision.detail or "")[-500:]}})
        body = to_action_body(decision.action)
        record = {"decision_id": d.decision_id, "hand_num": d.hand_num, "response": decision.action,
                  "sent": body.model_dump(mode="json", exclude_none=True), "error": decision.error,
                  "detail": decision.detail, "logs": decision.logs, "bot_ms": decision.bot_ms,
                  "elapsed_ms": decision.elapsed_ms, "restarted": decision.restarted}
        try:
            res = await self.b.client.decision(self.match.id, m.DecisionRequest(
                decision_id=d.decision_id, action=body, logs=[m.LogEntry(**l) for l in logs]))
            record.update(applied=res.applied.model_dump(mode="json", exclude_none=True), corrected=res.corrected)
        except ArenaError as e:
            record.update(post_error=f"{e.status} {e.code}")
            if e.code == "decision_stale":
                log.warning(f"match {self.match.id}: decision {d.decision_id} was too late "
                            "(the arena already acted)")
            else:
                log.warning(f"match {self.match.id}: posting decision {d.decision_id}: {e}")
        self.record.decision(record)
