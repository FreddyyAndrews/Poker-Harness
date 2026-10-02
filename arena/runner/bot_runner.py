"""
Bot runner, protocol v2. Runs one bot in its own process, either locally
or inside the sandbox container. Standard library only: this file is
copied into the Docker image without the rest of the arena package.

Protocol: newline-delimited JSON over the process's original stdin and
stdout. Before the bot is imported, the runner moves the protocol to
private file descriptors: the bot's print() / sys.stdout (and anything
written to fd 1 by C code) go to stderr, and its stdin is /dev/null. Bot
code can't corrupt or read the protocol.

host -> runner
  {"type": "decide", "id": 7, "state": {...}, "timeout": 2.0}
      timeout may be null for no limit

runner -> host
  {"type": "ready", "protocol": 2, "load_ms": 120}       once, after import
  {"type": "log", "id": 7, "msg": "...", "data": {...}}  from ctx.log, streamed
  {"type": "action", "id": 7, "action": {...}, "elapsed_ms": 3}
  {"type": "error", "id": 7, "error": "timeout" | "exception" | "bad_return",
   "detail": "..."}
  {"type": "error", "id": null, "error": "load_failed", "detail": "..."}
      then the runner exits

The bot defines decide(state) or decide(state, ctx). It may also define
warmup(ctx), called once after import and before "ready" (load big tables
there). ctx.log(msg=None, **data) records structured notes with the
decision; ctx.time_left() is the seconds left in this decision's budget.

On timeout the runner reports it, but Python can't stop the bot's thread;
the host restarts the process.
"""

import importlib.util
import inspect
import json
import os
import sys
import threading
import time
import traceback

PROTOCOL       = 2
BOT_PATH       = os.environ.get("BOT_PATH", "/bot/bot.py")
BOT_ID         = os.environ.get("BOT_ID", "bot")
WARMUP_TIMEOUT = float(os.environ.get("WARMUP_TIMEOUT", "30"))

MAX_LOG_BYTES  = 16_000   # per ctx.log entry, after JSON encoding
MAX_LOGS       = 200      # per decision


# ---------------------------------------------------------------------------
# Protocol channel (set up before any bot code runs)
# ---------------------------------------------------------------------------

def _take_over_stdio():
    proto_in  = os.fdopen(os.dup(0), "r", encoding="utf-8")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    sys.stdin  = open(os.devnull, "r")
    sys.stdout = sys.stderr
    return proto_in, proto_out


class Channel:
    def __init__(self, proto_in, proto_out):
        self._in   = proto_in
        self._out  = proto_out
        self._lock = threading.Lock()

    def send(self, msg: dict):
        line = json.dumps(msg, default=repr)
        with self._lock:
            self._out.write(line + "\n")
            self._out.flush()

    def messages(self):
        for line in self._in:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                sys.stderr.write(f"[runner] bad message from host: {e}\n")


# ---------------------------------------------------------------------------
# Bot context
# ---------------------------------------------------------------------------

class Context:
    """Passed to decide(state, ctx) and warmup(ctx)."""

    def __init__(self, channel: Channel, decision_id, timeout):
        self.bot_id      = BOT_ID
        self.decision_id = decision_id
        self._channel    = channel
        self._deadline   = time.monotonic() + timeout if timeout else None
        self._count      = 0
        self._closed     = False

    def time_left(self):
        """Seconds left in this decision's budget, or None if unlimited."""
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - time.monotonic())

    def log(self, msg=None, **data):
        """Record a note with this decision, e.g. ctx.log("bluff", equity=0.31)."""
        if self._closed:
            return
        self._count += 1
        if self._count > MAX_LOGS:
            if self._count == MAX_LOGS + 1:
                self._send({"type": "log", "id": self.decision_id,
                            "msg": f"[runner] log limit of {MAX_LOGS} per decision reached",
                            "data": {"truncated": True}})
            return
        entry = {"type": "log", "id": self.decision_id,
                 "msg": None if msg is None else str(msg), "data": data}
        if len(json.dumps(entry, default=repr)) > MAX_LOG_BYTES:
            text  = json.dumps({"msg": entry["msg"], "data": data}, default=repr)
            entry = {"type": "log", "id": self.decision_id,
                     "msg": text[:MAX_LOG_BYTES] + "...",
                     "data": {"truncated": True}}
        self._send(entry)

    def _send(self, msg):
        self._channel.send(msg)

    def close(self):
        self._closed = True


# ---------------------------------------------------------------------------
# Running bot code
# ---------------------------------------------------------------------------

class _Timeout(Exception):
    pass


def _call_with_timeout(fn, args, timeout_s):
    """Run fn(*args) on a worker thread; wait up to timeout_s (None = no
    limit). Returns the result, raises _Timeout, or re-raises fn's error."""
    box  = {"value": None, "error": None}
    done = threading.Event()

    def _worker():
        try:
            box["value"] = fn(*args)
        except BaseException as e:   # bots can raise anything
            box["error"] = e
        finally:
            done.set()

    threading.Thread(target=_worker, daemon=True).start()
    if not done.wait(timeout_s):
        raise _Timeout()
    if box["error"] is not None:
        raise box["error"]
    return box["value"]


def _wants_ctx(fn) -> bool:
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        return True
    positional = [p for p in params
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 2


def load_bot(path: str):
    bot_dir = os.path.dirname(os.path.abspath(path))
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)        # bots may import sibling modules
    spec   = importlib.util.spec_from_file_location("bot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "decide", None)):
        raise AttributeError("bot.py must define a decide() function")
    return module


def _check_action(action):
    if not isinstance(action, dict) or not isinstance(action.get("action"), str):
        raise ValueError(f"decide() must return a dict with an 'action' string, got {action!r}")
    try:
        json.dumps(action)
    except (TypeError, ValueError) as e:
        raise ValueError(f"decide() returned something that isn't JSON: {e}") from None
    return action


def handle_decide(bot, wants_ctx: bool, channel: Channel, msg: dict):
    did     = msg.get("id")
    timeout = msg.get("timeout")
    ctx     = Context(channel, did, timeout)
    args    = (msg.get("state"), ctx) if wants_ctx else (msg.get("state"),)
    start   = time.monotonic()
    try:
        result = _call_with_timeout(bot.decide, args, timeout)
    except _Timeout:
        channel.send({"type": "error", "id": did, "error": "timeout",
                      "detail": f"decide() did not return within {timeout}s"})
        return
    except BaseException:
        _send_exception(channel, did)
        return
    finally:
        ctx.close()
    try:
        action = _check_action(result)
    except ValueError as e:
        channel.send({"type": "error", "id": did, "error": "bad_return", "detail": str(e)})
        return
    channel.send({"type": "action", "id": did, "action": action,
                  "elapsed_ms": round((time.monotonic() - start) * 1000, 1)})


def _send_exception(channel, did):
    tb = traceback.format_exc()
    sys.stderr.write(f"[runner] decide() raised:\n{tb}")
    channel.send({"type": "error", "id": did, "error": "exception", "detail": tb[-4000:]})


def main():
    channel = Channel(*_take_over_stdio())
    start   = time.monotonic()
    try:
        bot = load_bot(BOT_PATH)
        if callable(getattr(bot, "warmup", None)):
            ctx = Context(channel, None, WARMUP_TIMEOUT)
            try:
                _call_with_timeout(bot.warmup, (ctx,), WARMUP_TIMEOUT)
            finally:
                ctx.close()
    except BaseException:
        tb = traceback.format_exc()
        sys.stderr.write(f"[runner] failed to load {BOT_PATH}:\n{tb}")
        channel.send({"type": "error", "id": None, "error": "load_failed", "detail": tb[-4000:]})
        return 1

    wants_ctx = _wants_ctx(bot.decide)
    channel.send({"type": "ready", "protocol": PROTOCOL,
                  "load_ms": round((time.monotonic() - start) * 1000, 1)})

    for msg in channel.messages():
        if msg.get("type") == "decide":
            handle_decide(bot, wants_ctx, channel, msg)
        else:
            sys.stderr.write(f"[runner] ignoring message type {msg.get('type')!r}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
