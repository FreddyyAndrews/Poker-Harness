"""A scriptable fake of the arena bot API (docs/bot-api.md) for testing the
bridge without a server. Tests push stream messages and inspect requests."""

import asyncio
import json
import re
from collections import defaultdict

import httpx

from poker_harness.engine.game import PokerEngine


class FakeArena:
    def __init__(self, me: str = "river-rat"):
        self.me = me
        self.calls = []                      # (method, path, body)
        self.event_conns = []                # one queue per event-stream connection
        self.match_conns = defaultdict(list)
        self.queued = defaultdict(list)      # (method, path) -> [(status, body, headers)]
        self.online = []
        self.applied = set()
        self.on_decision = None              # async fn(match_id, body) for follow-ups
        self._seq = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- scripting --------------------------------------------------------------

    def fail(self, method: str, path: str, status: int, code: str = "x", headers=None, times=1):
        for _ in range(times):
            self.queued[(method, path)].append((status, {"error": code, "code": code}, headers or {}))

    async def until(self, pred, timeout: float = 5.0):
        for _ in range(int(timeout / 0.01)):
            if pred():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("timed out waiting for the bridge")

    def posted(self, path: str) -> list:
        return [b for m, p, b in self.calls if m == "POST" and p == path]

    async def event(self, data, conn: int = -1):
        await self.until(lambda: self.event_conns)
        await self.event_conns[conn].put(data)

    async def match(self, match_id: str, data, conn: int = -1):
        await self.until(lambda: self.match_conns[match_id])
        await self.match_conns[match_id][conn].put(data)

    def close_events(self):
        for q in self.event_conns:
            q.put_nowait(None)

    def close_match(self, match_id: str):
        for q in self.match_conns[match_id]:
            q.put_nowait(None)

    # -- the fake server ------------------------------------------------------------

    async def _stream(self, q):
        while True:
            item = await q.get()
            if item is None:
                return
            yield b"\n" if item == "" else (json.dumps(item) + "\n").encode()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, body))
        if self.queued[(method, path)]:
            status, payload, headers = self.queued[(method, path)].pop(0)
            return httpx.Response(status, json=payload, headers=headers)

        if path == "/api/account":
            return httpx.Response(200, json={"name": self.me, "owner": "fred", "created_at": "2026-10-02",
                                             "scopes": ["bot:play", "bot:read"], "ratings": {"hu": 1500}})
        if path == "/api/token/test":
            return httpx.Response(200, json={"ok": True, "bot": self.me, "scopes": ["bot:play", "bot:read"]})
        if path == "/api/bot/online":
            return httpx.Response(200, json={"bots": [{"name": n} for n in self.online + [self.me]]})
        if path == "/api/stream/event":
            q = asyncio.Queue()
            self.event_conns.append(q)
            return httpx.Response(200, content=self._stream(q))
        m = re.fullmatch(r"/api/bot/match/([^/]+)/stream", path)
        if m:
            q = asyncio.Queue()
            self.match_conns[m.group(1)].append(q)
            return httpx.Response(200, content=self._stream(q))
        m = re.fullmatch(r"/api/bot/match/([^/]+)/decision", path)
        if m:
            did = body["decision_id"]
            if did in self.applied:
                return httpx.Response(409, json={"error": f"{did} is not pending", "code": "decision_stale"})
            self.applied.add(did)
            if self.on_decision:
                await self.on_decision(m.group(1), body)
            return httpx.Response(200, json={"ok": True, "applied": body["action"], "corrected": False})
        if re.fullmatch(r"/api/challenge/[^/]+/(accept|decline|cancel)", path) or \
                re.fullmatch(r"/api/bot/match/[^/]+/leave", path) or method == "DELETE":
            return httpx.Response(200, json={})
        m = re.fullmatch(r"/api/challenge/([^/]+)", path)
        if m and method == "POST":
            self._seq += 1
            return httpx.Response(200, json={"id": f"ch_out{self._seq}", "challenger": {"name": self.me},
                                             "dest": {"name": m.group(1)}, "format": body["format"],
                                             "rated": body.get("rated", False)})
        if path == "/api/seek" and method == "POST":
            self._seq += 1
            return httpx.Response(200, json={"id": f"sk{self._seq}", "format": body["format"],
                                             "rated": body.get("rated", False)})
        return httpx.Response(404, json={"error": f"no route {method} {path}", "code": "not_found"})


# -- message builders -------------------------------------------------------------

def match_ref(match_id="m1", me="river-rat", opp="house-shark", seats=2, your_seat=1, **extra):
    names = [opp, me] if your_seat == 1 else [me, opp]
    return {"id": match_id, "format": {"seats": seats, "hands": 1}, "your_seat": your_seat,
            "seats": [{"seat": i, "bot": {"name": n}} for i, n in enumerate(names)], **extra}


def challenge(cid="ch1", challenger="house-shark", dest="river-rat", **fmt):
    return {"id": cid, "challenger": {"name": challenger}, "dest": {"name": dest},
            "format": {"seats": 2, "hands": 1, **fmt}, "rated": False}


def decide(decision_id="m1:d1", me="river-rat", opp="house-shark", decision_s=30, bank_s=300):
    """A real decide for seat 1 (the BB after the button limps)."""
    eng = PokerEngine("m1_h0000", [opp, me], dealer_seat=0, seed=7)
    eng.start_hand()
    state = eng.apply_action(0, {"action": "call"})
    state["match_action_log"] = []
    return {"type": "decide", "decision_id": decision_id, "hand_num": 0,
            "decision_s": decision_s, "bank_s": bank_s, "state": state}


def match_full(pending=None, **kw):
    return {"type": "match_full", "match": match_ref(**kw), "hands_played": 0,
            "stacks": [10000, 10000], "bank_s": 300.0, "hand": [], "pending": pending}


def hand_end(delta=(-100, 100)):
    return {"type": "hand_end", "hand_num": 0, "board": [], "pot": 200, "showdown": False,
            "winners": [{"seat": 1, "bot": "river-rat", "amount": 200, "pot_type": "main"}],
            "stacks": [10000 + delta[0], 10000 + delta[1]], "delta": list(delta)}


def match_end(delta=100, me="river-rat", opp="house-shark"):
    return {"type": "match_end", "reason": "hands_complete", "hands_played": 1,
            "stacks": [10000 - delta, 10000 + delta], "chip_delta": {opp: -delta, me: delta}}
