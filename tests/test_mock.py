"""The mock arena (`arena serve-mock`): a real HTTP server, the real bridge,
and a raw protocol client for the edge cases."""
import asyncio
import json
import textwrap

import httpx
import pytest

pytest.importorskip("fastapi")
import uvicorn  # noqa: E402

from poker_harness.bridge import ArenaClient, Bridge  # noqa: E402
from poker_harness.bridge.config import BridgeConfig  # noqa: E402
from poker_harness.mock.app import create_app  # noqa: E402
from poker_harness.mock.arena import MockArena  # noqa: E402
from poker_harness.replay import verify_run  # noqa: E402
from poker_harness.runs import Run  # noqa: E402

CHECKER = """
    def decide(state, ctx):
        ctx.log("seen", owed=state["amount_owed"], cards="".join(state["your_cards"]))
        return {"action": "check" if state["can_check"] else "call"}
"""

FAST = {"decision_s": 2, "bank_s": 2}


def run(coro, timeout=60):
    return asyncio.run(asyncio.wait_for(coro, timeout))


class Server:
    """The mock arena on a random local port, inside the test's event loop."""

    def __init__(self, tmp_path, **arena_kw):
        arena_kw.setdefault("runs_root", tmp_path / "server-runs")
        arena_kw.setdefault("connect_timeout_s", 5)
        self.arena = MockArena(**arena_kw)
        self.tokens = {}
        self.tmp = tmp_path

    def bot(self, name):
        self.tokens[name] = self.arena.register(name)
        return self.tokens[name]

    async def __aenter__(self):
        config = uvicorn.Config(create_app(self.arena, keepalive_s=0.5), host="127.0.0.1", port=0,
                                log_level="warning")
        self.server = uvicorn.Server(config)
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        self.server.should_exit = True
        await self.task

    def bridge(self, name, src=CHECKER, **cfg):
        bot = self.tmp / f"{name}.py"
        bot.write_text(textwrap.dedent(src))
        config = BridgeConfig.model_validate({
            "url": self.url, "token": self.tokens[name], "bot": {"path": str(bot)},
            "records": {"dir": str(self.tmp / f"{name}-records")},
            "backoff_base_s": 0.05, "backoff_max_s": 0.2, **cfg})
        client = ArenaClient(self.url, config.token, silence_s=5, backoff_base_s=0.05, backoff_max_s=0.2)
        return Bridge(config, client)

    def raw(self, name):
        return RawClient(self.url, self.tokens[name])


class RawClient:
    """Speaks the bot API directly, to test what the bridge would hide."""

    def __init__(self, url, token):
        self.http = httpx.AsyncClient(base_url=url, headers={"Authorization": f"Bearer {token}"},
                                      timeout=10)
        self.tasks = []

    async def close(self):
        for t in self.tasks:
            t.cancel()
        await self.http.aclose()

    def stream(self, path) -> asyncio.Queue:
        q = asyncio.Queue()

        async def pump():
            async with self.http.stream("GET", path, timeout=None) as resp:
                q.put_nowait({"status": resp.status_code})
                async for line in resp.aiter_lines():
                    if line.strip():
                        q.put_nowait(json.loads(line))
            q.put_nowait(None)
        self.tasks.append(asyncio.create_task(pump()))
        return q

    @staticmethod
    async def next(q, type_=None, timeout=10):
        while True:
            msg = await asyncio.wait_for(q.get(), timeout)
            if msg is None or type_ is None or msg.get("type") == type_:
                return msg

    async def post(self, path, body=None):
        return await self.http.post(path, json=body)


def own_view_leaks(record_dir, server_runs, match_id) -> int:
    """Count messages in a bridge record that carry an opponent's hidden cards."""
    god = {e["hand_num"]: e["hole_cards"] for e in Run.open(match_id, server_runs).events()
           if e["type"] == "hand_start"}
    meta = json.loads((record_dir / match_id / "meta.json").read_text())
    me = meta["match"]["your_seat"]
    leaks = 0
    for line in (record_dir / match_id / "stream.jsonl").read_text().splitlines():
        msg = json.loads(line)
        if msg["type"] == "hand_end":
            continue
        text = json.dumps(msg)
        if "board_plan" in text:
            leaks += 1
        for seat, cards in god.get(msg.get("hand_num"), {}).items():
            if int(seat) != me and all(c in text for c in cards):
                leaks += 1
    return leaks


# ---------------------------------------------------------------------------
# End to end with the bridge
# ---------------------------------------------------------------------------

def test_bridge_plays_a_house_bot(tmp_path):
    async def go():
        async with Server(tmp_path) as srv:
            srv.bot("mybot")
            bridge = srv.bridge("mybot", max_matches=1, matchmaking={
                "enabled": True, "interval_s": 0.1, "opponents": ["house-tight"],
                "formats": [{"seats": 2, "hands": 15, "reset_stacks": True, "clock": FAST}]})
            await bridge.run()
            return srv
    srv = run(go())
    [mid] = list(srv.arena.matches)
    run_ = Run.open(mid, tmp_path / "server-runs")
    assert run_.meta["status"] == "complete" and run_.meta["result"]["n_hands"] == 15
    assert verify_run(run_) == {}
    notes = [l for d in run_.decisions("mybot") for l in d["logs"]]
    assert notes and notes[0]["msg"] == "seen"                    # ctx.log reached the server
    records = tmp_path / "mybot-records"
    assert json.loads((records / mid / "meta.json").read_text())["status"] == "finished"
    assert own_view_leaks(records, tmp_path / "server-runs", mid) == 0


def test_two_bridges_play_each_other(tmp_path):
    async def go():
        async with Server(tmp_path) as srv:
            srv.bot("alice")
            srv.bot("bob")
            alice = srv.bridge("alice", max_matches=1, matchmaking={
                "enabled": True, "interval_s": 0.1, "opponents": ["bob"],
                "formats": [{"seats": 2, "hands": 10, "clock": FAST}]})
            bob = srv.bridge("bob", max_matches=1, challenge={"min_decision_s": 1})
            await asyncio.gather(alice.run(), bob.run())
            return srv
    srv = run(go())
    [lm] = srv.arena.matches.values()
    assert {b.name for b in lm.bots} == {"alice", "bob"} and lm.challenge_id
    res = Run.open(lm.id, tmp_path / "server-runs").meta["result"]
    assert sum(res["chip_delta"].values()) == 0
    for name in ("alice", "bob"):
        assert own_view_leaks(tmp_path / f"{name}-records", tmp_path / "server-runs", lm.id) == 0


def test_seek_fills_a_table_with_house_bots(tmp_path):
    async def go():
        async with Server(tmp_path, house_fill_after_s=0.2) as srv:
            srv.bot("mybot")
            bridge = srv.bridge("mybot", max_matches=1, challenge={"concurrency": 1}, seek={
                "enabled": True, "formats": [{"seats": 4, "hands": 6, "reset_stacks": True, "clock": FAST}]})
            await bridge.run()
            return srv
    srv = run(go())
    [lm] = srv.arena.matches.values()
    assert len(lm.bots) == 4 and sum(b.house for b in lm.bots) == 3
    assert lm.seek_ids == {"mybot": next(iter(lm.seek_ids.values()))}


# ---------------------------------------------------------------------------
# The protocol directly
# ---------------------------------------------------------------------------

def test_errors(tmp_path):
    async def go():
        async with Server(tmp_path) as srv:
            srv.bot("a")
            bad = RawClient(srv.url, "nope")
            assert (await bad.http.get("/api/account")).status_code == 401
            await bad.close()
            c = srv.raw("a")
            r = await c.post("/api/challenge/ghost", {"format": {"seats": 2}})
            assert r.status_code == 404 and r.json()["code"] == "not_found"
            r = await c.post("/api/challenge/house-caller", {"format": {"seats": 6}})
            assert r.status_code == 400 and r.json()["code"] == "invalid_format"
            r = await c.post("/api/challenge/house-caller", {"format": {"seats": 2, "small_blind": 500}})
            assert r.status_code == 400 and r.json()["code"] == "invalid_body"
            r = await c.post("/api/bot/match/nope/decision", {"decision_id": "x", "action": {"action": "fold"}})
            assert r.status_code == 404
            await c.close()
    run(go())


async def start_match(srv, name, hands=3, clock=FAST):
    """Challenge house-caller with a raw client; return (client, events, match id, match queue)."""
    c = srv.raw(name)
    events = c.stream("/api/stream/event")
    assert (await RawClient.next(events))["status"] == 200
    r = await c.post("/api/challenge/house-caller",
                     {"format": {"seats": 2, "hands": hands, "reset_stacks": True, "clock": clock}})
    assert r.status_code == 200
    start = await RawClient.next(events, "match_start")
    assert start["match"]["challenge_id"] == r.json()["id"]
    mid = start["match"]["id"]
    q = c.stream(f"/api/bot/match/{mid}/stream")
    assert (await RawClient.next(q))["status"] == 200
    return c, events, mid, q


def test_reconnect_resumes_pending_decision_and_stale_posts_fail(tmp_path):
    async def go():
        async with Server(tmp_path) as srv:
            srv.bot("a")
            c, events, mid, q = await start_match(srv, "a", hands=2, clock={"decision_s": 5, "bank_s": 5})
            full = await RawClient.next(q, "match_full")
            assert full["match"]["your_seat"] in (0, 1)
            d = await RawClient.next(q, "decide")
            for t in c.tasks[1:]:
                t.cancel()                                    # drop the match stream
            await asyncio.sleep(0.1)
            q2 = c.stream(f"/api/bot/match/{mid}/stream")
            await RawClient.next(q2)
            full2 = await RawClient.next(q2, "match_full")
            assert full2["pending"]["decision_id"] == d["decision_id"]
            assert any(m["type"] == "hand_start" for m in full2["hand"])
            body = {"decision_id": d["decision_id"], "action": {"action": "raise", "amount": 1}}
            r = await c.post(f"/api/bot/match/{mid}/decision", body)
            assert r.status_code == 200 and r.json()["corrected"] is True
            assert r.json()["applied"]["action"] in ("raise", "all_in", "call", "check")
            res = await RawClient.next(q2, "decision_result")
            assert res["decision_id"] == d["decision_id"] and res["corrected"] is True
            r = await c.post(f"/api/bot/match/{mid}/decision", body)
            assert r.status_code == 409 and r.json()["code"] == "decision_stale"
            await c.close()
    run(go())


def test_timeout_falls_back_and_empties_the_bank(tmp_path):
    async def go():
        async with Server(tmp_path) as srv:
            srv.bot("a")
            c, events, mid, q = await start_match(srv, "a", hands=1, clock={"decision_s": 0.3, "bank_s": 0.3})
            d = await RawClient.next(q, "decide")
            res = await RawClient.next(q, "decision_result", timeout=5)      # never answered
            assert res["decision_id"] == d["decision_id"]
            assert res["error"] == "timeout" and res["bank_s"] == 0
            assert res["applied"]["action"] in ("check", "fold")
            await c.close()
    run(go())


def test_leave_and_match_end(tmp_path):
    async def go():
        async with Server(tmp_path) as srv:
            srv.bot("a")
            c, events, mid, q = await start_match(srv, "a", hands=3)
            await RawClient.next(q, "decide")
            assert (await c.post(f"/api/bot/match/{mid}/leave")).status_code == 200
            end = await RawClient.next(q, "match_end", timeout=10)
            assert end["hands_played"] == 3 and set(end["chip_delta"]) == {"a", "house-caller"}
            fin = await RawClient.next(events, "match_finish")
            assert fin["match"]["status"] == "finished"
            await c.close()
    run(go())


def test_abort_when_a_bot_never_connects(tmp_path):
    async def go():
        async with Server(tmp_path, connect_timeout_s=0.3) as srv:
            srv.bot("a")
            c = srv.raw("a")
            events = c.stream("/api/stream/event")
            await RawClient.next(events)
            await c.post("/api/challenge/house-caller", {"format": {"seats": 2, "hands": 3}})
            await RawClient.next(events, "match_start")
            fin = await RawClient.next(events, "match_finish", timeout=5)
            assert fin["match"]["status"] == "aborted"
            await c.close()
    run(go())


def test_challenges_between_remote_bots(tmp_path):
    async def go():
        async with Server(tmp_path, challenge_ttl_s=0.5) as srv:
            srv.bot("a")
            srv.bot("b")
            a, b = srv.raw("a"), srv.raw("b")
            ea, eb = a.stream("/api/stream/event"), b.stream("/api/stream/event")
            await RawClient.next(ea), await RawClient.next(eb)
            online = (await a.http.get("/api/bot/online")).json()["bots"]
            assert {"a", "b", "house-caller"} <= {x["name"] for x in online}
            # decline
            ch = (await a.post("/api/challenge/b", {"format": {"seats": 2}})).json()
            assert (await RawClient.next(eb, "challenge"))["challenge"]["id"] == ch["id"]
            r = await b.post(f"/api/challenge/{ch['id']}/accept-nope")
            assert r.status_code == 404
            assert (await a.post(f"/api/challenge/{ch['id']}/accept")).status_code == 403
            await b.post(f"/api/challenge/{ch['id']}/decline", {"reason": "later"})
            dec = await RawClient.next(ea, "challenge_declined")
            assert dec["reason"] == "later"
            # cancel
            ch2 = (await a.post("/api/challenge/b", {"format": {"seats": 2}})).json()
            await a.post(f"/api/challenge/{ch2['id']}/cancel")
            assert (await RawClient.next(eb, "challenge_canceled"))["challenge"]["id"] == ch2["id"]
            # expiry
            ch3 = (await a.post("/api/challenge/b", {"format": {"seats": 2}})).json()
            exp = await RawClient.next(eb, "challenge_canceled", timeout=5)
            assert exp["challenge"]["id"] == ch3["id"] and exp["challenge"]["status"] == "expired"
            await a.close()
            await b.close()
    run(go())


def test_new_event_stream_replaces_the_old_and_presence(tmp_path):
    async def go():
        async with Server(tmp_path) as srv:
            srv.bot("a")
            c = srv.raw("a")
            first = c.stream("/api/stream/event")
            await RawClient.next(first)
            second = c.stream("/api/stream/event")
            await RawClient.next(second)
            assert await RawClient.next(first) is None                # closed by the arena
            names = {x["name"] for x in (await c.http.get("/api/bot/online")).json()["bots"]}
            assert "a" in names
            await c.close()
            await asyncio.sleep(0.3)
            assert not srv.arena.bots["a"].online
    run(go())
