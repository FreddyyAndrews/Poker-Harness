"""
Own-perspective records of arena matches, written by the bridge.

  <records dir>/<match_id>/
    meta.json         the match, our bot (name, path, version), times, result
    stream.jsonl      every match-stream message as received
    decisions.jsonl   each decide we answered: our reply, what was applied,
                      errors, ctx.log notes, timing
    stderr.log        our bot's stderr and print() output

Only what the arena sent our bot is recorded: our cards, public actions,
showdown cards. The local analysis tools learn to read these in T5.
"""

import json
import time
from pathlib import Path

from poker_harness.match import bot_fingerprint


class MatchRecord:
    def __init__(self, root: Path, match, me: str, bot_path: str):
        self.dir = Path(root) / match.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta = {
            "kind": "arena_match", "match": match.model_dump(mode="json"), "me": me,
            "bot": {"path": str(bot_path), "version": bot_fingerprint(bot_path)},
            "started": time.time(), "finished": None, "result": None,
        }
        self._write_meta()
        self._stream = open(self.dir / "stream.jsonl", "a", encoding="utf-8")
        self._decisions = open(self.dir / "decisions.jsonl", "a", encoding="utf-8")

    @property
    def stderr_path(self) -> Path:
        return self.dir / "stderr.log"

    def _write_meta(self) -> None:
        tmp = self.dir / "meta.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=2) + "\n")
        tmp.replace(self.dir / "meta.json")

    def message(self, msg) -> None:
        data = msg.model_dump(mode="json") if hasattr(msg, "model_dump") else msg
        self._stream.write(json.dumps({"received": time.time(), **data}) + "\n")
        self._stream.flush()

    def decision(self, record: dict) -> None:
        self._decisions.write(json.dumps(record, default=repr) + "\n")
        self._decisions.flush()

    def finish(self, result=None, status: str = "finished") -> None:
        self.meta["finished"] = time.time()
        self.meta["status"] = status
        if result is not None:
            self.meta["result"] = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        self._write_meta()

    def close(self) -> None:
        self._stream.close()
        self._decisions.close()
