"""
HTTP client for the arena bot API (docs/bot-api.md), like lichess-bot's
Lichess class: one method per endpoint, retries with backoff on rate
limits and server errors, and NDJSON streams with keepalive handling.
"""

import asyncio
import json
import logging
import random
from typing import AsyncIterator, Callable, Optional

import httpx

import poker_harness
from poker_harness.protocol import models as m

log = logging.getLogger("arena.connect")


class ArenaError(Exception):
    """A non-retryable error response from the arena."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class StreamClosed(Exception):
    """A stream ended or went silent; reconnect."""


def backoff_delays(base: float, cap: float):
    """Exponential backoff with jitter: base, 2*base, ... capped at cap."""
    delay = base
    while True:
        yield delay * random.uniform(0.5, 1.0)
        delay = min(cap, delay * 2)


class ArenaClient:
    def __init__(self, url: str, token: str, *, transport: Optional[httpx.AsyncBaseTransport] = None,
                 timeout_s: float = 30.0, silence_s: float = 30.0, retries: int = 4,
                 backoff_base_s: float = 1.0, backoff_max_s: float = 60.0):
        self.http = httpx.AsyncClient(
            base_url=url.rstrip("/"), transport=transport,
            headers={"Authorization": f"Bearer {token}",
                     "User-Agent": f"poker-harness/{poker_harness.__version__} arena-connect"},
            timeout=httpx.Timeout(timeout_s))
        self.silence_s = silence_s
        self.retries = retries
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s

    async def close(self) -> None:
        await self.http.aclose()

    # -- plumbing -----------------------------------------------------------

    @staticmethod
    def _error(resp: httpx.Response) -> ArenaError:
        try:
            body = m.ErrorBody.model_validate(resp.json())
            return ArenaError(resp.status_code, body.code, body.error)
        except Exception:
            return ArenaError(resp.status_code, "http_error", resp.text[:200] or resp.reason_phrase)

    async def _request(self, method: str, path: str, body=None) -> dict:
        delays = backoff_delays(self.backoff_base_s, self.backoff_max_s)
        payload = body.model_dump(mode="json", exclude_none=True) if hasattr(body, "model_dump") else body
        for attempt in range(self.retries + 1):
            try:
                resp = await self.http.request(method, path, json=payload)
            except httpx.TransportError as e:
                if attempt == self.retries:
                    raise
                wait = next(delays)
                log.warning(f"{method} {path}: {type(e).__name__}; retrying in {wait:.1f}s")
                await asyncio.sleep(wait)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.retries:
                    raise self._error(resp)
                wait = next(delays)
                if resp.status_code == 429:
                    try:
                        wait = float(resp.headers.get("Retry-After", wait))
                    except ValueError:
                        pass
                log.warning(f"{method} {path}: {resp.status_code}; retrying in {wait:.1f}s")
                await asyncio.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise self._error(resp)
            return resp.json() if resp.content else {}
        raise AssertionError("unreachable")

    async def stream(self, path: str, parse: Callable) -> AsyncIterator:
        """Yield parsed messages from an NDJSON stream. Raises StreamClosed
        when the stream ends, errors or is silent (no keepalive) too long;
        raises ArenaError for 4xx responses."""
        try:
            async with self.http.stream("GET", path, timeout=httpx.Timeout(None)) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise StreamClosed(f"{path}: {resp.status_code}")
                    raise self._error(resp)
                lines = resp.aiter_lines().__aiter__()
                while True:
                    line = await self._next_line(lines, path)
                    if not line.strip():
                        continue                     # keepalive
                    try:
                        yield parse(json.loads(line))
                    except (json.JSONDecodeError, m.ValidationError) as e:
                        log.warning(f"{path}: ignoring a malformed message: {str(e)[:200]}")
        except httpx.TransportError as e:
            raise StreamClosed(f"{path}: {type(e).__name__}: {e}") from None

    async def _next_line(self, lines, path: str) -> str:
        """The next line, or StreamClosed after silence_s without one. Uses
        asyncio.wait rather than wait_for: on Python 3.10 wait_for can
        swallow a cancellation that arrives while it waits."""
        nxt = asyncio.ensure_future(lines.__anext__())
        try:
            done, _ = await asyncio.wait({nxt}, timeout=self.silence_s)
        finally:
            if not nxt.done():
                nxt.cancel()
        if not done:
            raise StreamClosed(f"{path}: silent for {self.silence_s:.0f}s")
        try:
            return nxt.result()
        except StopAsyncIteration:
            raise StreamClosed(f"{path}: closed by the arena") from None

    # -- endpoints (docs/bot-api.md) ------------------------------------------

    async def account(self) -> m.Account:
        return m.Account.model_validate(await self._request("GET", "/api/account"))

    async def token_test(self) -> dict:
        return await self._request("GET", "/api/token/test")

    async def playing(self) -> list:
        data = await self._request("GET", "/api/account/playing")
        return [m.MatchRef.model_validate(x) for x in data.get("matches", [])]

    async def online_bots(self) -> list:
        return m.OnlineBots.model_validate(await self._request("GET", "/api/bot/online")).bots

    def events(self) -> AsyncIterator:
        return self.stream("/api/stream/event", m.parse_event)

    def match_stream(self, match_id: str) -> AsyncIterator:
        return self.stream(f"/api/bot/match/{match_id}/stream", m.parse_match_message)

    async def challenge(self, bot: str, request: m.ChallengeRequest) -> m.Challenge:
        return m.Challenge.model_validate(await self._request("POST", f"/api/challenge/{bot}", request))

    async def accept(self, challenge_id: str) -> None:
        await self._request("POST", f"/api/challenge/{challenge_id}/accept")

    async def decline(self, challenge_id: str, reason: str = "generic") -> None:
        await self._request("POST", f"/api/challenge/{challenge_id}/decline",
                            m.DeclineRequest(reason=reason))

    async def cancel(self, challenge_id: str) -> None:
        await self._request("POST", f"/api/challenge/{challenge_id}/cancel")

    async def seek(self, request: m.SeekRequest) -> m.Seek:
        return m.Seek.model_validate(await self._request("POST", "/api/seek", request))

    async def cancel_seek(self, seek_id: str) -> None:
        await self._request("DELETE", f"/api/seek/{seek_id}")

    async def decision(self, match_id: str, request: m.DecisionRequest) -> m.DecisionAccepted:
        return m.DecisionAccepted.model_validate(
            await self._request("POST", f"/api/bot/match/{match_id}/decision", request))

    async def leave(self, match_id: str) -> None:
        await self._request("POST", f"/api/bot/match/{match_id}/leave")
