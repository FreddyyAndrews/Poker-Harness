"""
Seats: whoever makes decisions for one place at the table.

Every seat has the same async interface, so the match runner doesn't care
whether a decision comes from a bot process, a fixed script, a human or
(later) an LLM bot:

    await seat.start()
    decision = await seat.act(state, timeout=2.0)   # -> Decision
    await seat.close()

A Decision always carries a usable action. When a seat fails (timeout,
crash, exception, bad return value) the action is the fallback, check if
possible and fold otherwise, and `error` says what went wrong.

Seat types:
  SubprocessBotSeat  a bot.py in its own process (or Docker container),
                     talking protocol v2 to arena/runner/bot_runner.py
  ScriptedSeat       plays a fixed list of actions (tests, spots)
  CallbackSeat       asks a function (the CLI, and human seats later)
"""

import asyncio
import collections
import inspect
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from arena.runner.package import prepare_bot_dir

RUNNER_PATH   = Path(__file__).parent / "runner" / "bot_runner.py"
# Temp copies of bots for Docker mounts. Under $HOME because Docker VMs like
# Colima only share the home directory with containers.
DOCKER_TMP    = Path.home() / ".cache" / "poker-harness" / "bots"
DEFAULT       = object()      # "use the seat's default timeout"
STDERR_TAIL   = 50            # lines of stderr kept for error reports
READ_LIMIT    = 4 * 1024 * 1024


def fallback_action(state: dict) -> dict:
    """What a failed seat does: check if it can, otherwise fold."""
    return {"action": "check"} if state.get("can_check") else {"action": "fold"}


@dataclass
class Decision:
    action: dict                        # passed to engine.apply_action
    error: Optional[str] = None         # timeout | exception | bad_return | crashed |
                                        # load_failed | broken
    detail: Optional[str] = None
    logs: list = field(default_factory=list)    # ctx.log entries: {"msg", "data"}
    elapsed_ms: float = 0.0             # host-side wall time
    bot_ms: Optional[float] = None      # time inside decide(), reported by the runner
    restarted: bool = False             # the seat's process was restarted after this

    def to_dict(self) -> dict:
        return {"action": self.action, "error": self.error, "detail": self.detail,
                "logs": self.logs, "elapsed_ms": self.elapsed_ms,
                "bot_ms": self.bot_ms, "restarted": self.restarted}


class Seat:
    kind = "seat"

    def __init__(self, seat_id: str):
        self.seat_id = seat_id

    async def start(self) -> None:
        pass

    async def act(self, state: dict, timeout=DEFAULT) -> Decision:
        raise NotImplementedError

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Scripted and callback seats
# ---------------------------------------------------------------------------

_CODES = {"f": "fold", "x": "check", "c": "call", "a": "all_in"}


def parse_code(code) -> dict:
    """'f' | 'x' | 'c' | 'a' | 'r300' | a raw action dict -> raw action dict"""
    if isinstance(code, dict):
        return code
    c = str(code).strip().lower()
    if c in _CODES:
        return {"action": _CODES[c]}
    if c[:1] in ("r", "b") and c[1:].isdigit():
        return {"action": "raise", "amount": int(c[1:])}
    raise ValueError(f"can't parse action code {code!r}")


class ScriptedSeat(Seat):
    """Plays `actions` in order. When they run out it checks/folds, or
    raises if `strict` (useful in tests to catch unexpected decisions)."""
    kind = "scripted"

    def __init__(self, seat_id: str, actions: list, strict: bool = False):
        super().__init__(seat_id)
        self._actions = [parse_code(a) for a in actions]
        self.strict   = strict
        self.seen     = []          # states this seat was asked about

    async def act(self, state, timeout=DEFAULT) -> Decision:
        self.seen.append(state)
        if self._actions:
            return Decision(self._actions.pop(0))
        if self.strict:
            raise RuntimeError(f"{self.seat_id}: script ran out at {state.get('hand_id')}")
        return Decision(fallback_action(state))


class CallbackSeat(Seat):
    """Asks fn(state) for each decision; fn may be sync or async and returns
    a raw action dict or an action code. With a timeout, a slow answer
    gets the fallback action."""
    kind = "callback"

    def __init__(self, seat_id: str, fn: Callable, timeout: Optional[float] = None):
        super().__init__(seat_id)
        self.fn      = fn
        self.timeout = timeout

    async def act(self, state, timeout=DEFAULT) -> Decision:
        timeout = self.timeout if timeout is DEFAULT else timeout
        start   = time.monotonic()

        async def ask():
            result = self.fn(state)
            if inspect.isawaitable(result):
                result = await result
            return parse_code(result)

        try:
            action = await asyncio.wait_for(ask(), timeout)
        except asyncio.TimeoutError:
            return Decision(fallback_action(state), error="timeout",
                            detail=f"no answer within {timeout}s",
                            elapsed_ms=_ms_since(start))
        return Decision(action, elapsed_ms=_ms_since(start))


# ---------------------------------------------------------------------------
# Bot processes
# ---------------------------------------------------------------------------

class _ProtocolError(Exception):
    pass


class SubprocessBotSeat(Seat):
    """
    Runs a bot (bot.py file, directory with bot.py + data/, or .zip) in its
    own process via arena/runner/bot_runner.py, or in a Docker container
    when `docker_image` is set.

    - The bot's stderr (including its print() output) is read continuously,
      written to `stderr_path` if given, and the last lines are kept for
      error reports.
    - The host enforces every timeout itself (`timeout` + `grace` for IPC),
      so a bot that hangs the whole runner still can't stall a match.
    - On a timeout or crash the decision is the fallback action and the
      process is restarted (in-memory bot state is lost). After
      `max_restarts` the seat gives up and always uses the fallback.
    - `on_event(dict)` receives bot_ready, bot_load_failed, bot_restart,
      bot_broken and bot_closed events; they're also kept in `self.events`.
    """
    kind = "bot"

    def __init__(
        self,
        seat_id: str,
        bot_path,
        *,
        timeout: Optional[float] = 2.0,
        warmup_timeout: float = 30.0,
        grace: float = 1.0,
        max_restarts: int = 5,
        stderr_path=None,
        on_event: Optional[Callable] = None,
        docker_image: Optional[str] = None,
        docker_memory: str = "768m",
        docker_cpus: str = "0.5",
        docker_tmpfs: str = "20m",
        python: str = sys.executable,
        env: Optional[dict] = None,
    ):
        super().__init__(seat_id)
        self.bot_path       = str(bot_path)
        self.timeout        = timeout
        self.warmup_timeout = warmup_timeout
        self.grace          = grace
        self.max_restarts   = max_restarts
        self.stderr_path    = Path(stderr_path) if stderr_path else None
        self.on_event       = on_event
        self.docker_image   = docker_image
        self.docker_limits  = (docker_memory, docker_cpus, docker_tmpfs)
        self.python         = python
        self.extra_env      = env or {}

        self.status         = "new"   # new | ready | load_failed | broken | closed
        self.status_detail  = None
        self.restarts       = 0
        self.events         = []
        self.stderr_tail    = collections.deque(maxlen=STDERR_TAIL)

        self._proc          = None
        self._container     = None
        self._stderr_task   = None
        self._stderr_file   = None
        self._bot_dir       = None
        self._cleanup_dir   = None
        self._next_id       = 0

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        try:
            self._bot_dir, self._cleanup_dir = prepare_bot_dir(
                self.bot_path, tmp_root=str(DOCKER_TMP) if self.docker_image else None)
        except Exception as e:
            self._set_failed("load_failed", f"can't prepare bot: {e}")
            return
        if self.stderr_path:
            self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr_file = open(self.stderr_path, "a", encoding="utf-8")
        await self._spawn()

    async def close(self) -> None:
        await self._kill(graceful=True)
        if self._stderr_file:
            self._stderr_file.close()
            self._stderr_file = None
        if self._cleanup_dir and os.path.isdir(self._cleanup_dir):
            import shutil
            shutil.rmtree(self._cleanup_dir, ignore_errors=True)
            self._cleanup_dir = None
        if self.status != "closed":
            self.status = "closed"
            self._event("bot_closed", restarts=self.restarts)

    # -- deciding -----------------------------------------------------------

    async def act(self, state, timeout=DEFAULT) -> Decision:
        timeout = self.timeout if timeout is DEFAULT else timeout
        start   = time.monotonic()

        if self.status in ("load_failed", "broken", "closed"):
            return Decision(fallback_action(state), error=self.status,
                            detail=self.status_detail)
        if self._proc is None or self._proc.returncode is not None:
            await self._restart("crashed", "process was not running")
            if self.status != "ready":
                return Decision(fallback_action(state), error=self.status,
                                detail=self.status_detail)

        self._next_id += 1
        did  = self._next_id
        logs = []

        def failed(error, detail, restart=False):
            return Decision(fallback_action(state), error=error, detail=detail,
                            logs=logs, elapsed_ms=_ms_since(start), restarted=restart)

        try:
            await self._send({"type": "decide", "id": did, "state": state,
                              "timeout": timeout})
            deadline = None if timeout is None else start + timeout + self.grace
            while True:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                msg = await asyncio.wait_for(self._recv(), remaining)
                if msg.get("id") != did:
                    continue                 # stale message from an earlier decision
                kind = msg.get("type")
                if kind == "log":
                    logs.append({"msg": msg.get("msg"), "data": msg.get("data") or {}})
                elif kind == "action":
                    return Decision(msg.get("action"), logs=logs,
                                    elapsed_ms=_ms_since(start), bot_ms=msg.get("elapsed_ms"))
                elif kind == "error":
                    err, detail = msg.get("error"), msg.get("detail")
                    if err == "timeout":
                        # the bot's thread is still running; replace the process
                        await self._restart("timeout", detail, decision_id=did)
                        return failed("timeout", detail, restart=True)
                    return failed(err, detail)
        except asyncio.TimeoutError:
            detail = f"no response within {timeout}s (+{self.grace}s grace); process killed"
            await self._restart("timeout", detail, decision_id=did)
            return failed("timeout", detail, restart=True)
        except (_ProtocolError, BrokenPipeError, ConnectionResetError, EOFError) as e:
            await asyncio.sleep(0.05)       # let the stderr reader catch up
            detail = f"{e}; stderr tail:\n" + "\n".join(self.stderr_tail)
            await self._restart("crashed", detail, decision_id=did)
            return failed("crashed", detail, restart=True)

    # -- process management -------------------------------------------------

    def _command(self) -> list:
        if not self.docker_image:
            return [self.python, "-u", str(RUNNER_PATH)]
        memory, cpus, tmpfs = self.docker_limits
        self._container = f"arena-{self.seat_id}-{uuid.uuid4().hex[:8]}".replace("_", "-")
        return [
            "docker", "run", "--rm", "-i", "--name", self._container,
            "--network", "none",
            "--memory", memory, "--memory-swap", memory, "--cpus", cpus,
            "--read-only", "--security-opt", "no-new-privileges",
            "--user", "1000:1000", "--tmpfs", f"/tmp:size={tmpfs}",
            "-v", f"{self._bot_dir}:/bot:ro",
            "-e", "BOT_PATH=/bot/bot.py", "-e", "BOT_DATA_DIR=/bot/data",
            "-e", f"BOT_ID={self.seat_id}",
            "-e", f"WARMUP_TIMEOUT={self.warmup_timeout}",
            self.docker_image,
        ]

    def _env(self) -> dict:
        env = {**os.environ, **self.extra_env,
               "BOT_ID": self.seat_id, "WARMUP_TIMEOUT": str(self.warmup_timeout),
               "PYTHONUNBUFFERED": "1"}
        if not self.docker_image:
            env["BOT_PATH"]     = os.path.join(self._bot_dir, "bot.py")
            env["BOT_DATA_DIR"] = os.path.join(self._bot_dir, "data")
        return env

    async def _spawn(self) -> None:
        start = time.monotonic()
        self._proc = await asyncio.create_subprocess_exec(
            *self._command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(),
            limit=READ_LIMIT,
        )
        self._stderr_task = asyncio.ensure_future(self._drain_stderr(self._proc))
        try:
            msg = await asyncio.wait_for(self._recv(), self.warmup_timeout + self.grace)
        except asyncio.TimeoutError:
            await self._kill()
            self._set_failed("load_failed",
                             f"not ready within warmup timeout {self.warmup_timeout}s")
            return
        except (_ProtocolError, EOFError) as e:
            await asyncio.sleep(0.05)
            await self._kill()
            self._set_failed("load_failed", f"{e}; stderr tail:\n" + "\n".join(self.stderr_tail))
            return
        if msg.get("type") == "ready":
            self.status = "ready"
            self._event("bot_ready", load_ms=msg.get("load_ms"),
                        startup_ms=_ms_since(start), restarts=self.restarts)
        else:
            await self._kill()
            self._set_failed("load_failed", msg.get("detail") or json.dumps(msg))

    async def _restart(self, reason: str, detail, decision_id=None) -> None:
        await self._kill()
        if self.restarts >= self.max_restarts:
            self.status = "broken"
            self.status_detail = (f"gave up after {self.restarts} restarts; "
                                  f"last failure: {reason}: {detail}")
            self._event("bot_broken", reason=reason, detail=detail, restarts=self.restarts)
            return
        self.restarts += 1
        self._event("bot_restart", reason=reason, detail=detail,
                    decision_id=decision_id, restarts=self.restarts)
        await self._spawn()

    async def _kill(self, graceful: bool = False) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            try:
                if graceful and proc.stdin:
                    proc.stdin.close()
                    await asyncio.wait_for(proc.wait(), 2)
            except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
                pass
            if proc.returncode is None:
                if self._container:
                    # killing `docker run` doesn't stop the container
                    killer = await asyncio.create_subprocess_exec(
                        "docker", "kill", self._container,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    await killer.wait()
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, 1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._stderr_task.cancel()
            self._stderr_task = None
        self._container = None

    # -- I/O ----------------------------------------------------------------

    async def _send(self, msg: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise BrokenPipeError("bot process is not running")
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()

    async def _recv(self) -> dict:
        if self._proc is None:
            raise EOFError("bot process is not running")
        try:
            line = await self._proc.stdout.readline()
        except ValueError as e:              # line longer than READ_LIMIT
            raise _ProtocolError(f"protocol line too long: {e}") from None
        if not line:
            raise EOFError("bot process exited")
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            raise _ProtocolError(f"bad protocol line: {line[:200]!r}") from None

    async def _drain_stderr(self, proc) -> None:
        """Read stderr in chunks (not lines: one huge line must not stop
        the reader, or the pipe fills and the bot blocks)."""
        partial = ""
        while True:
            chunk = await proc.stderr.read(65536)
            if not chunk:
                if partial:
                    self._stderr_line(partial)
                return
            text = partial + chunk.decode("utf-8", errors="replace")
            *lines, partial = text.split("\n")
            for line in lines:
                self._stderr_line(line)
            if len(partial) > 65536:
                self._stderr_line(partial)
                partial = ""
            if self._stderr_file:
                self._stderr_file.flush()

    def _stderr_line(self, line: str) -> None:
        self.stderr_tail.append(line[:2000])
        if self._stderr_file:
            self._stderr_file.write(line + "\n")

    # -- helpers ------------------------------------------------------------

    def _set_failed(self, status: str, detail: str) -> None:
        self.status, self.status_detail = status, detail
        self._event("bot_load_failed" if status == "load_failed" else f"bot_{status}",
                    detail=detail)

    def _event(self, kind: str, **data) -> None:
        ev = {"type": kind, "seat_id": self.seat_id, "ts": time.time(), **data}
        self.events.append(ev)
        if self.on_event:
            self.on_event(ev)


def _ms_since(start: float) -> float:
    return round((time.monotonic() - start) * 1000, 1)
