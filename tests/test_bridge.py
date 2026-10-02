"""The bridge (`arena connect`) against a fake arena."""
import asyncio
import json
import textwrap

import pytest

from poker_harness.bridge import ArenaClient, ArenaError, Bridge, ConfigError, load_config
from poker_harness.bridge.bridge import to_action_body
from poker_harness.bridge.config import BridgeConfig
from fake_arena import FakeArena, challenge, decide, hand_end, match_end, match_full, match_ref

CHECKER = """
    def decide(state, ctx):
        ctx.log("free check" if state["can_check"] else "calling", owed=state["amount_owed"])
        return {"action": "check" if state["can_check"] else "call"}
"""

SLOW = """
    import time
    def decide(state):
        time.sleep(5)
        return {"action": "call"}
"""


@pytest.fixture
def setup(tmp_path):
    def make(bot_src=CHECKER, **cfg):
        bot = tmp_path / "bot.py"
        bot.write_text(textwrap.dedent(bot_src))
        arena = FakeArena()
        config = BridgeConfig.model_validate({
            "url": "http://arena.test", "token": "t", "bot": {"path": str(bot)},
            "records": {"dir": str(tmp_path / "records")}, "backoff_base_s": 0.01,
            "backoff_max_s": 0.05, **cfg})
        client = ArenaClient(config.url, config.token, transport=arena.transport(), silence_s=3,
                             backoff_base_s=0.01, backoff_max_s=0.05)
        return arena, Bridge(config, client), tmp_path / "records"
    return make


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


async def play_one_match(arena, bridge, pending_in_full=False):
    """Challenge -> accept -> match -> one decision -> end."""
    task = asyncio.create_task(bridge.run())
    await arena.event({"type": "challenge", "challenge": challenge()})
    await arena.until(lambda: arena.posted("/api/challenge/ch1/accept"))
    await arena.event({"type": "match_start", "match": match_ref(challenge_id="ch1")})
    if pending_in_full:
        await arena.match("m1", match_full(pending=decide()))
    else:
        await arena.match("m1", match_full())
        await arena.match("m1", decide())
    await arena.until(lambda: arena.posted("/api/bot/match/m1/decision"))
    await arena.match("m1", hand_end())
    await arena.match("m1", match_end())
    return task


# ---------------------------------------------------------------------------
# Playing
# ---------------------------------------------------------------------------

def test_plays_a_match_end_to_end(setup):
    arena, bridge, records = setup(max_matches=1)

    async def go():
        task = await play_one_match(arena, bridge)
        await task
    run(go())
    [post] = arena.posted("/api/bot/match/m1/decision")
    assert post["decision_id"] == "m1:d1"
    assert post["action"] == {"action": "check"}
    assert post["logs"] == [{"msg": "free check", "data": {"owed": 0}}]
    meta = json.loads((records / "m1" / "meta.json").read_text())
    assert meta["status"] == "finished" and meta["result"]["chip_delta"]["river-rat"] == 100
    assert meta["me"] == "river-rat" and meta["bot"]["version"]
    stream = [json.loads(l)["type"] for l in (records / "m1" / "stream.jsonl").read_text().splitlines()]
    assert stream == ["match_full", "decide", "hand_end", "match_end"]
    [rec] = [json.loads(l) for l in (records / "m1" / "decisions.jsonl").read_text().splitlines()]
    assert rec["applied"] == {"action": "check"} and rec["error"] is None and rec["bot_ms"] is not None


def test_pending_decision_in_match_full_is_answered_once(setup):
    arena, bridge, records = setup(max_matches=1)

    async def go():
        task = asyncio.create_task(bridge.run())
        await arena.event({"type": "match_start", "match": match_ref()})
        await arena.match("m1", match_full(pending=decide()))
        await arena.until(lambda: arena.posted("/api/bot/match/m1/decision"))
        # the stream drops; on reconnect the arena repeats the (now answered) decide
        arena.close_match("m1")
        await arena.until(lambda: len(arena.match_conns["m1"]) == 2)
        await arena.match("m1", match_full(pending=decide()))
        await arena.match("m1", decide())
        await arena.match("m1", match_end())
        await task
    run(go())
    assert len(arena.posted("/api/bot/match/m1/decision")) == 1


def test_slow_bot_sends_fallback_before_the_deadline(setup):
    arena, bridge, records = setup(bot_src=SLOW, max_matches=1, bot={"path": "x", "time_margin_s": 0.5})
    bridge.cfg.bot.path = str(records.parent / "bot.py")

    async def go():
        task = asyncio.create_task(bridge.run())
        await arena.event({"type": "match_start", "match": match_ref()})
        await arena.match("m1", match_full())
        await arena.match("m1", decide(decision_s=1.0, bank_s=0.0))
        await arena.until(lambda: arena.posted("/api/bot/match/m1/decision"), timeout=6)
        await arena.match("m1", match_end())
        await task
    run(go())
    [post] = arena.posted("/api/bot/match/m1/decision")
    assert post["action"] == {"action": "check"}            # fallback: check is free here
    assert "timeout" in post["logs"][-1]["msg"]


def test_stale_decision_is_logged_not_fatal(setup):
    arena, bridge, records = setup(max_matches=1)
    arena.applied.add("m1:d1")

    async def go():
        task = asyncio.create_task(bridge.run())
        await arena.event({"type": "match_start", "match": match_ref()})
        await arena.match("m1", match_full(pending=decide()))
        await arena.until(lambda: arena.posted("/api/bot/match/m1/decision"))
        await arena.match("m1", match_end())
        await task
    run(go())
    rec = json.loads((records / "m1" / "decisions.jsonl").read_text())
    assert rec["post_error"] == "409 decision_stale"


def test_unknown_messages_are_ignored(setup):
    arena, bridge, records = setup(max_matches=1)

    async def go():
        task = asyncio.create_task(bridge.run())
        await arena.event({"type": "tournament_announced", "name": "x"})
        await arena.event("")                                 # keepalive
        await arena.event({"type": "match_start", "match": match_ref()})
        await arena.match("m1", match_full())
        await arena.match("m1", {"type": "chat", "text": "gl"})
        await arena.match("m1", match_end())
        await task
    run(go())


def test_bad_bot_reply_becomes_fold():
    assert to_action_body({"action": "bluff"}).action == "fold"
    assert to_action_body({"action": "raise", "amount": "lots"}).model_dump(exclude_none=True) == \
        {"action": "raise"}
    assert to_action_body({"action": "call", "amount": 5}).amount is None
    assert to_action_body(None).action == "fold"


# ---------------------------------------------------------------------------
# Challenges
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ch, cfg, reason", [
    (challenge(seats=6), {}, "format"),
    (challenge(hands=5000), {"challenge": {"max_hands": 1000}}, "format"),
    (challenge(clock={"decision_s": 1}), {}, "format"),
    ({**challenge(), "rated": True}, {"challenge": {"modes": ["casual"]}}, "rated"),
    (challenge(challenger="spammer"), {"challenge": {"block_list": ["spammer"]}}, "generic"),
    (challenge(), {"challenge": {"allow_list": ["friend"]}}, "generic"),
    (challenge(), {"challenge": {"accept": False}}, "generic"),
])
def test_declines_by_policy(setup, ch, cfg, reason):
    arena, bridge, records = setup(challenge={"seats": [2], **cfg.get("challenge", {})})

    async def go():
        task = asyncio.create_task(bridge.run())
        await arena.event({"type": "challenge", "challenge": ch})
        await arena.until(lambda: arena.posted(f"/api/challenge/{ch['id']}/decline"))
        await bridge.stop()
        await task
    run(go())
    assert arena.posted(f"/api/challenge/{ch['id']}/decline") == [{"reason": reason}]
    assert not arena.posted(f"/api/challenge/{ch['id']}/accept")


def test_concurrency_limit(setup):
    arena, bridge, records = setup(challenge={"concurrency": 1})

    async def go():
        task = asyncio.create_task(bridge.run())
        await arena.event({"type": "challenge", "challenge": challenge("ch1")})
        await arena.until(lambda: arena.posted("/api/challenge/ch1/accept"))
        await arena.event({"type": "challenge", "challenge": challenge("ch2")})
        await arena.until(lambda: arena.posted("/api/challenge/ch2/decline"))
        await bridge.stop()
        await task
    run(go())
    assert arena.posted("/api/challenge/ch2/decline") == [{"reason": "too_many_matches"}]


def test_stopping_declines_with_later_and_withdraws(setup):
    arena, bridge, records = setup(seek={"enabled": True, "formats": [{"seats": 6}]},
                                   challenge={"concurrency": 2})

    async def go():
        task = asyncio.create_task(bridge.run())
        await arena.until(lambda: arena.posted("/api/seek"))
        bridge.stopping = True
        await arena.event({"type": "challenge", "challenge": challenge()})
        await arena.until(lambda: arena.posted("/api/challenge/ch1/decline"))
        await bridge.stop()
        await task
    run(go())
    assert arena.posted("/api/challenge/ch1/decline") == [{"reason": "later"}]
    assert any(m == "DELETE" and p.startswith("/api/seek/") for m, p, _ in arena.calls)


# ---------------------------------------------------------------------------
# Matchmaking and seeks
# ---------------------------------------------------------------------------

def test_matchmaking_challenges_online_bots_and_backs_off(setup):
    arena, bridge, records = setup(matchmaking={
        "enabled": True, "interval_s": 0.05, "decline_backoff_s": 60,
        "formats": [{"seats": 2, "hands": 50}]})
    arena.online = ["house-shark"]

    async def go():
        task = asyncio.create_task(bridge.run())
        await arena.until(lambda: arena.posted("/api/challenge/house-shark"), timeout=8)
        await arena.event({"type": "challenge_declined", "reason": "later", "challenge": {
            "id": "ch_out1", "challenger": {"name": "river-rat"}, "dest": {"name": "house-shark"},
            "format": {"seats": 2}}})
        await asyncio.sleep(0.3)                 # several intervals: no new challenge (backoff)
        await bridge.stop()
        await task
    run(go())
    posts = arena.posted("/api/challenge/house-shark")
    assert len(posts) == 1 and posts[0]["format"]["hands"] == 50


def test_unanswered_challenge_is_canceled(setup):
    arena, bridge, records = setup(matchmaking={
        "enabled": True, "interval_s": 0.05, "challenge_timeout_s": 0.1})
    arena.online = ["house-shark"]

    async def go():
        task = asyncio.create_task(bridge.run())
        await arena.until(lambda: arena.posted("/api/challenge/ch_out1/cancel"), timeout=8)
        await bridge.stop()
        await task
    run(go())


def test_seek_is_renewed_after_expiry(setup):
    arena, bridge, records = setup(seek={"enabled": True, "formats": [{"seats": 6, "hands": 10}]})

    async def go():
        task = asyncio.create_task(bridge.run())
        await arena.until(lambda: len(arena.posted("/api/seek")) == 1)
        await arena.event({"type": "seek_expired", "seek": {"id": "sk1", "format": {"seats": 6}}})
        await arena.until(lambda: len(arena.posted("/api/seek")) == 2, timeout=5)
        await bridge.stop()
        await task
    run(go())


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------

def test_event_stream_reconnects_and_resumes_matches(setup):
    arena, bridge, records = setup(max_matches=1)
    arena.fail("GET", "/api/stream/event", 500)

    async def go():
        task = asyncio.create_task(bridge.run())
        await arena.until(lambda: arena.event_conns)          # connected after a 500
        arena.close_events()
        await arena.until(lambda: len(arena.event_conns) == 2)
        # after reconnecting the arena resends match_start for ongoing matches
        await arena.event({"type": "match_start", "match": match_ref()})
        await arena.event({"type": "match_start", "match": match_ref()})
        await arena.match("m1", match_full())
        await arena.match("m1", match_end())
        await task
    run(go())
    assert len(arena.match_conns["m1"]) == 1                 # one player, not two


def test_rate_limit_is_honoured(setup):
    arena, bridge, records = setup()
    arena.fail("POST", "/api/challenge/ch1/accept", 429, "rate_limited", headers={"Retry-After": "0.05"})

    async def go():
        task = asyncio.create_task(bridge.run())
        await arena.event({"type": "challenge", "challenge": challenge()})
        await arena.until(lambda: len(arena.posted("/api/challenge/ch1/accept")) == 2)
        await bridge.stop()
        await task
    run(go())


def test_bad_token_stops_the_bridge(setup):
    arena, bridge, records = setup()
    arena.fail("GET", "/api/stream/event", 401, "bad_token")

    async def go():
        await bridge.run()
    run(go())
    assert bridge.stopping


def test_stop_now_leaves_matches(setup):
    arena, bridge, records = setup()

    async def go():
        task = asyncio.create_task(bridge.run())
        await arena.event({"type": "match_start", "match": match_ref()})
        await arena.match("m1", match_full())
        await bridge.stop(now=True)
        await task
    run(go())
    assert arena.posted("/api/bot/match/m1/leave") == [None]
    meta = json.loads((records / "m1" / "meta.json").read_text())
    assert meta["status"] == "left"


# ---------------------------------------------------------------------------
# Config and CLI
# ---------------------------------------------------------------------------

def test_config_file_env_and_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_TOKEN", "secret")
    path = tmp_path / "config.yml"
    from poker_harness.bridge import DEFAULT_CONFIG
    path.write_text(DEFAULT_CONFIG)
    cfg = load_config(str(path), {"url": "http://x", "bot.path": "b.py"})
    assert cfg.token == "secret" and cfg.url == "http://x" and cfg.bot.path == "b.py"
    assert cfg.challenge.concurrency == 1 and cfg.matchmaking.formats[0].seats == 2
    monkeypatch.delenv("ARENA_TOKEN")
    with pytest.raises(ConfigError, match="ARENA_TOKEN is not set"):
        load_config(str(path))
    path.write_text("url: http://x\ntoken: t\nchallenge: {modes: [ranked]}\n")
    with pytest.raises(ConfigError, match="challenge.modes"):
        load_config(str(path))
