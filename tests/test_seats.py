"""Seats and bot protocol v2: real bot processes for each failure mode."""
import asyncio
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from poker_harness.seats import (
    CallbackSeat, Decision, ScriptedSeat, SubprocessBotSeat, fallback_action,
)

STATE = {"type": "action_request", "amount_owed": 100, "can_check": False}
CHECKABLE = {"type": "action_request", "amount_owed": 0, "can_check": True}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def bot(tmp_path):
    """bot(source) -> path to a bot.py with that source."""
    n = [0]

    def make(src):
        n[0] += 1
        d = tmp_path / f"bot{n[0]}"
        d.mkdir()
        (d / "bot.py").write_text(textwrap.dedent(src))
        return d / "bot.py"
    return make


async def session(path, states, **kw):
    seat = SubprocessBotSeat("t", path, **kw)
    await seat.start()
    out = []
    try:
        for s in states:
            out.append(await seat.act(s))
    finally:
        await seat.close()
    return seat, out


COUNTER = """
    import os, signal, sys, time
    N = 0
    MARK = os.path.join(os.path.dirname(__file__), "once")
    def once():
        # true the first time only, even across process restarts
        if os.path.exists(MARK):
            return False
        open(MARK, "w").close()
        return True
    def decide(state, ctx):
        global N
        N += 1
        ctx.log("count", n=N)
        {body}
        return {{"action": "call"}}
"""


def counter_bot(bot, body="pass"):
    return bot(COUNTER.format(body=body))


# ---------------------------------------------------------------------------
# Normal operation
# ---------------------------------------------------------------------------

def test_ctx_logs_and_actions(bot):
    seat, ds = run(session(counter_bot(bot), [STATE, STATE]))
    assert [d.action for d in ds] == [{"action": "call"}] * 2
    assert ds[1].logs == [{"msg": "count", "data": {"n": 2}}]
    assert ds[0].error is None and ds[0].bot_ms is not None
    assert [e["type"] for e in seat.events] == ["bot_ready", "bot_closed"]


def test_one_argument_decide_still_works(bot):
    p = bot("def decide(state):\n    return {'action': 'check'}\n")
    _, ds = run(session(p, [CHECKABLE]))
    assert ds[0].action == {"action": "check"} and ds[0].logs == []


def test_print_fd1_and_stderr_flood_do_not_break_protocol(bot, tmp_path):
    p = bot("""
        import os, sys
        def decide(state):
            print("hello from print")
            os.write(1, b"raw write to fd 1\\n")
            sys.stderr.write("x" * 300_000)          # one huge line, no newline
            sys.stderr.write("\\n" + "y\\n" * 20_000)  # lots of lines
            return {"action": "call"}
    """)
    log = tmp_path / "stderr.log"
    seat, ds = run(session(p, [STATE] * 3, stderr_path=log))
    assert all(d.action == {"action": "call"} and d.error is None for d in ds)
    text = log.read_text()
    assert "hello from print" in text and "raw write to fd 1" in text


def test_bot_cannot_read_protocol_from_stdin(bot):
    p = bot("""
        import sys
        def decide(state, ctx):
            ctx.log("stdin", data=sys.stdin.read())
            return {"action": "call"}
    """)
    _, ds = run(session(p, [STATE, STATE]))
    assert ds[0].logs[0]["data"]["data"] == ""
    assert ds[1].action == {"action": "call"}


def test_warmup_hook_runs_before_ready(bot):
    p = bot("""
        TABLE = None
        def warmup(ctx):
            global TABLE
            TABLE = "loaded"
        def decide(state, ctx):
            ctx.log(TABLE)
            return {"action": "call"}
    """)
    _, ds = run(session(p, [STATE]))
    assert ds[0].logs[0]["msg"] == "loaded"


def test_log_limits(bot):
    p = bot("""
        def decide(state, ctx):
            ctx.log("big", blob="z" * 100_000)
            for i in range(300):
                ctx.log("spam", i=i)
            return {"action": "call"}
    """)
    _, ds = run(session(p, [STATE]))
    logs = ds[0].logs
    assert logs[0]["data"] == {"truncated": True} and len(logs[0]["msg"]) < 20_000
    assert len(logs) == 201 and logs[-1]["data"] == {"truncated": True}


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------

def test_timeout_restarts_the_process(bot):
    p = counter_bot(bot, body="if N == 2 and once(): time.sleep(5)")
    seat, ds = run(session(p, [STATE] * 4, timeout=0.3))
    assert ds[1].error == "timeout" and ds[1].restarted
    assert ds[1].action == {"action": "fold"}
    # restarted process: memory is gone, counting starts again
    assert ds[2].error is None and ds[2].logs[0]["data"]["n"] == 1
    assert [e["type"] for e in seat.events].count("bot_restart") == 1


def test_hard_hang_is_killed_by_the_host(bot):
    # SIGSTOP freezes the whole runner, so only the host's own timer works
    p = counter_bot(bot, body="if once(): os.kill(os.getpid(), signal.SIGSTOP)")
    seat, ds = run(session(p, [STATE, STATE], timeout=0.3, grace=0.3))
    assert ds[0].error == "timeout" and "process killed" in ds[0].detail
    assert ds[1].error is None


def test_crash_restarts(bot):
    p = counter_bot(bot, body="if once(): os._exit(3)")
    seat, ds = run(session(p, [STATE, STATE]))
    assert ds[0].error == "crashed" and ds[0].restarted
    assert ds[1].error is None


def test_exception_keeps_process_and_memory(bot):
    p = counter_bot(bot, body="if N == 1: raise RuntimeError('boom')")
    seat, ds = run(session(p, [STATE, STATE]))
    assert ds[0].error == "exception" and "RuntimeError: boom" in ds[0].detail
    assert not ds[0].restarted
    assert ds[1].logs[0]["data"]["n"] == 2          # same process


@pytest.mark.parametrize("ret", ["'call'", "{'act': 'call'}", "{'action': 'raise', 'amount': object()}"])
def test_bad_return(bot, ret):
    p = bot(f"def decide(state):\n    return {ret}\n")
    _, ds = run(session(p, [STATE]))
    assert ds[0].error == "bad_return" and ds[0].action == {"action": "fold"}


@pytest.mark.parametrize("src", ["def decide(:\n", "x = 1\n", "raise ImportError('no numpy')\n"])
def test_load_failure_folds_every_decision(bot, src):
    seat, ds = run(session(bot(src), [STATE, CHECKABLE]))
    assert seat.status in ("load_failed", "closed")
    assert [d.error for d in ds] == ["load_failed"] * 2
    assert [d.action["action"] for d in ds] == ["fold", "check"]
    assert any(e["type"] == "bot_load_failed" for e in seat.events)


def test_slow_warmup_fails_load(bot):
    p = bot("import time\ndef warmup(ctx):\n    time.sleep(5)\ndef decide(s):\n    return {'action': 'call'}\n")
    seat, ds = run(session(p, [STATE], warmup_timeout=0.3, grace=0.2))
    assert ds[0].error == "load_failed"


def test_gives_up_after_max_restarts(bot):
    p = bot("import os\ndef decide(state):\n    os._exit(1)\n")
    seat, ds = run(session(p, [STATE] * 5, max_restarts=2))
    assert [d.error for d in ds] == ["crashed", "crashed", "crashed", "broken", "broken"]
    assert seat.restarts == 2
    assert any(e["type"] == "bot_broken" for e in seat.events)


def test_bad_bot_path():
    seat, ds = run(session("/no/such/bot.py", [STATE]))
    assert ds[0].error == "load_failed"


# ---------------------------------------------------------------------------
# Other seat types
# ---------------------------------------------------------------------------

def test_fallback_action():
    assert fallback_action(CHECKABLE) == {"action": "check"}
    assert fallback_action(STATE) == {"action": "fold"}


def test_scripted_seat():
    seat = ScriptedSeat("s", ["r300", "c", {"action": "all_in"}])
    ds = [run(seat.act(STATE)) for _ in range(4)]
    assert [d.action for d in ds] == [
        {"action": "raise", "amount": 300}, {"action": "call"},
        {"action": "all_in"}, {"action": "fold"}]
    strict = ScriptedSeat("s", [], strict=True)
    with pytest.raises(RuntimeError):
        run(strict.act(STATE))


def test_callback_seat_sync_async_and_timeout():
    assert run(CallbackSeat("c", lambda s: "x").act(CHECKABLE)).action == {"action": "check"}

    async def slow(state):
        await asyncio.sleep(1)
        return "c"

    async def fast(state):
        return {"action": "raise", "amount": 500}

    assert run(CallbackSeat("c", fast).act(STATE)).action["amount"] == 500
    d = run(CallbackSeat("c", slow, timeout=0.05).act(STATE))
    assert d.error == "timeout" and d.action == {"action": "fold"}


# ---------------------------------------------------------------------------
# Match runner and Docker
# ---------------------------------------------------------------------------

def test_match_survives_problem_bots(bot, tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_RUNS", str(tmp_path / "runs"))
    sys.path.insert(0, str(Path(__file__).parent.parent / "sandbox"))
    import match
    paths = {
        "shark":   "bots/shark/bot.py",
        "chatty":  str(bot("import os\ndef decide(s):\n    print('hi'); os.write(1, b'junk\\n')\n    return {'action': 'call'}\n")),
        "crashy":  str(counter_bot(bot, body="if N % 7 == 0: os._exit(1)")),
        "broken":  str(bot("def decide(:\n")),
    }
    r = match.run_match("t", paths, n_hands=60, seed=1)
    assert r["n_hands"] > 0
    assert sum(r["final_stacks"].values()) == 40_000
    assert r["bot_errors"]["chatty"] == []
    assert "crashed" in r["bot_errors"]["crashy"]
    assert r["bot_errors"]["broken"][0].startswith("load_failed")
    assert r["restarts"]["crashy"] > 0
    assert (tmp_path / "runs" / "t" / "events.jsonl").exists()


def _docker_image_ready():
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", "poker-harness-sandbox:latest"],
                       capture_output=True)
    return r.returncode == 0


@pytest.mark.skipif(not _docker_image_ready(), reason="docker or sandbox image not available")
def test_docker_seat(bot):
    p = counter_bot(bot, body="if N == 2: time.sleep(5)")
    seat, ds = run(session(p, [STATE] * 3, docker_image="poker-harness-sandbox:latest",
                           timeout=0.5))
    assert ds[0].error is None and ds[0].logs[0]["data"]["n"] == 1
    assert ds[1].error == "timeout" and ds[1].restarted
    assert ds[2].error is None
    left = subprocess.run(["docker", "ps", "-q", "--filter", "name=arena-t-"],
                          capture_output=True, text=True).stdout.strip()
    assert left == ""
