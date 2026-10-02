"""
Run store: everything a match produced, one directory per match.

  runs/<match_id>/
    meta.json                      config, seats, status, result summary
    events.jsonl                   the full timeline, god view included
    bots/<dir>/decisions.jsonl     one record per decision
    bots/<dir>/stderr.log          the bot's stderr and print() output

<dir> is the bot id made filename-safe (meta.json's seats list records
each bot's "dir"); bot ids themselves can be any string.

Every line is written and flushed as it happens, so a live match can be
followed with `tail -f runs/<id>/events.jsonl`.

Every event has: v (schema version), seq (0, 1, 2, ... per match), ts
(unix time), match_id, hand_num (None outside hands) and type.

Event types:
  match_start      config and seats
  hand_start       god view: dealer, blinds, stacks, positions, every
                   seat's hole cards and the full board plan
  blind            small_blind / big_blind posted
  street_start     street and board so far
  action           seat, bot_id, action, amount, pot_after, stack_after,
                   stacks, decision_id (links to decisions.jsonl)
  uncalled_bet_returned
  showdown         revealed cards, hand strengths, winners
  uncontested_win
  hand_end         winners, uncalled, final stacks, per-seat delta
  bot_ready, bot_restart, bot_load_failed, bot_broken, bot_closed
  match_end        reason, final stacks, chip deltas, error counts

Decision records (decisions.jsonl): decision_id, hand_num, seat, bot_id,
street, state (exactly what the bot was sent, except match_action_log,
which is replaced by its length since it can be rebuilt from events),
response (the bot's raw reply), applied (the action the engine used),
corrected (applied differs from the reply), error, detail, logs (ctx.log
entries), elapsed_ms, bot_ms, restarted.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Iterator, Optional

SCHEMA_VERSION = 1
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def runs_dir() -> Path:
    return Path(os.environ.get("ARENA_RUNS", "runs"))


def check_id(kind: str, value: str) -> str:
    if not _ID_RE.match(value or ""):
        raise ValueError(f"{kind} {value!r} must be letters, digits, '.', '_' or '-' "
                         "(starting with a letter or digit)")
    return value


def make_event(seq: int, match_id: str, type_: str, hand_num: Optional[int] = None,
               **data) -> dict:
    return {"v": SCHEMA_VERSION, "seq": seq, "ts": time.time(), "match_id": match_id,
            "hand_num": hand_num, **data, "type": type_}


def _dump(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), default=repr)


class RunWriter:
    """Writes one match's directory. Not thread-safe; one writer per match."""

    def __init__(self, match_id: str, root: Optional[Path] = None):
        self.match_id = check_id("match id", match_id)
        self.dir      = (root or runs_dir()) / match_id
        if self.dir.exists():
            raise FileExistsError(f"run {match_id!r} already exists at {self.dir}")
        (self.dir / "bots").mkdir(parents=True)
        self._events    = open(self.dir / "events.jsonl", "w", encoding="utf-8")
        self._decisions = {}
        self._dirs      = {}          # bot_id -> directory name
        self.meta       = {}

    def write_meta(self, meta: dict) -> None:
        self.meta = {"v": SCHEMA_VERSION, "match_id": self.match_id, **meta}
        tmp = self.dir / "meta.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=2, default=repr) + "\n")
        tmp.replace(self.dir / "meta.json")

    def write_event(self, ev: dict) -> None:
        """Append an event that already has its envelope (see make_event)."""
        self._events.write(_dump(ev) + "\n")
        self._events.flush()

    def dir_name(self, bot_id: str) -> str:
        """A unique, filename-safe directory name for bot_id."""
        if bot_id not in self._dirs:
            base = re.sub(r"[^A-Za-z0-9_.-]", "_", bot_id).strip("._") or "bot"
            name, n = base, 2
            while name in self._dirs.values():
                name, n = f"{base}-{n}", n + 1
            self._dirs[bot_id] = name
        return self._dirs[bot_id]

    def bot_dir(self, bot_id: str) -> Path:
        d = self.dir / "bots" / self.dir_name(bot_id)
        d.mkdir(exist_ok=True)
        return d

    def stderr_path(self, bot_id: str) -> Path:
        return self.bot_dir(bot_id) / "stderr.log"

    def decision(self, bot_id: str, record: dict) -> None:
        f = self._decisions.get(bot_id)
        if f is None:
            f = self._decisions[bot_id] = open(self.bot_dir(bot_id) / "decisions.jsonl",
                                               "a", encoding="utf-8")
        f.write(_dump(record) + "\n")
        f.flush()

    def close(self) -> None:
        self._events.close()
        for f in self._decisions.values():
            f.close()
        self._decisions.clear()


class Run:
    """Read access to a stored match."""

    def __init__(self, path: Path):
        self.dir = Path(path)
        if not (self.dir / "meta.json").exists():
            raise FileNotFoundError(f"no run at {self.dir}")
        self.meta = json.loads((self.dir / "meta.json").read_text())

    @classmethod
    def open(cls, ref: str, root: Optional[Path] = None):
        """A run id in the runs dir, or a path to a run directory. Ids
        starting with "arena/" (and paths to bridge records) open the
        bridge's own-perspective record of an arena match."""
        from poker_harness.arena_records import ArenaRecord, is_arena_ref
        p = Path(ref)
        if not (p / "meta.json").exists():
            p = (root or runs_dir()) / ref
        if is_arena_ref(ref) or _is_arena_record(p):
            return ArenaRecord(p)
        return cls(p)

    @property
    def match_id(self) -> str:
        return self.meta["match_id"]

    def events(self) -> Iterator[dict]:
        with open(self.dir / "events.jsonl", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    def hand_events(self, hand_num: int) -> list:
        return [e for e in self.events() if e.get("hand_num") == hand_num]

    def hand_nums(self) -> list:
        return [e["hand_num"] for e in self.events() if e["type"] == "hand_start"]

    def bot_dir(self, bot_id: str) -> Path:
        for s in self.meta.get("seats", []):
            if s["bot_id"] == bot_id:
                return self.dir / "bots" / s.get("dir", bot_id)
        return self.dir / "bots" / bot_id

    def decisions(self, bot_id: str) -> Iterator[dict]:
        path = self.bot_dir(bot_id) / "decisions.jsonl"
        if not path.exists():
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    def bot_ids(self) -> list:
        return [s["bot_id"] for s in self.meta.get("seats", [])]


def _is_arena_record(path: Path) -> bool:
    try:
        return json.loads((Path(path) / "meta.json").read_text()).get("kind") == "arena_match"
    except (OSError, json.JSONDecodeError):
        return False


def list_runs(root: Optional[Path] = None, arena: bool = True) -> list:
    """Every run under root, oldest first: local and mock matches, plus
    (with arena) the bridge's records of arena matches in root/arena/. A
    bridge record of a match that also has a god-view run here (when
    testing against `arena serve-mock` with a shared runs dir) is left out,
    so the match isn't counted twice."""
    root = root or runs_dir()
    if not root.is_dir():
        return []
    runs = []
    for d in root.iterdir():
        if (d / "meta.json").exists() and not _is_arena_record(d):
            try:
                runs.append(Run(d))
            except (OSError, json.JSONDecodeError):
                continue
    if arena:
        from poker_harness.arena_records import list_records
        local = {r.match_id for r in runs}
        runs += [r for r in list_records(root) if r.raw["match"]["id"] not in local]
    return sorted(runs, key=lambda r: r.meta.get("created") or 0)
