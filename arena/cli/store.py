"""Where the CLI keeps things.

  spots library   $ARENA_SPOTS  (default ./spots)    <name>.yaml, tracked in git
  hand sessions   $ARENA_HOME/hands (default ./.arena/hands)  <id>.json
"""

import json
import os
import time
from pathlib import Path

from arena.spot import Spot, SpotError

SPOT_SUFFIXES = (".yaml", ".yml", ".json")


def arena_home() -> Path:
    return Path(os.environ.get("ARENA_HOME", ".arena"))


def spots_dir() -> Path:
    return Path(os.environ.get("ARENA_SPOTS", "spots"))


def find_spot(ref: str) -> Path:
    """A spot file path, or the name of a spot in the library."""
    p = Path(ref)
    if p.is_file():
        return p
    for suffix in SPOT_SUFFIXES:
        q = spots_dir() / (ref + suffix)
        if q.is_file():
            return q
    raise SpotError(f"no spot file or library spot named {ref!r} (library: {spots_dir()}/)")


def spot_ref(path: Path) -> str:
    """Library name of a spot file: its path under the library, no suffix
    (e.g. suites/basics/check-when-free)."""
    try:
        return str(path.relative_to(spots_dir()).with_suffix(""))
    except ValueError:
        return str(path)


def list_spots(subdir: str = None) -> list:
    """[(path, spot or None, error or None)] for every spot file in the
    library (or under one of its subdirectories), including suites."""
    d = spots_dir() / subdir if subdir else spots_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.rglob("*")):
        if p.suffix in SPOT_SUFFIXES and p.is_file():
            try:
                out.append((p, Spot.load(p), None))
            except Exception as e:      # list broken files instead of failing
                out.append((p, None, str(e)))
    return out


def save_spot(spot: Spot, name: str, force: bool = False) -> Path:
    parts = name.split("/")
    if not all(p and p.replace("-", "").replace("_", "").isalnum() for p in parts):
        raise SpotError(f"spot names use letters, digits, - and _ (and / for folders, "
                        f"e.g. suites/basics/x); got {name!r}")
    path = spots_dir() / f"{name}.yaml"
    if path.exists() and not force:
        raise SpotError(f"spot {name!r} already exists ({path}); use --force to overwrite")
    spot.name = parts[-1]
    spot.save(path)
    return path


class HandStore:
    def __init__(self, root: Path = None):
        self.dir = (root or arena_home()) / "hands"

    def path(self, hand_id: str) -> Path:
        return self.dir / f"{hand_id}.json"

    def new_id(self) -> str:
        n = 1
        while self.path(f"h{n}").exists():
            n += 1
        return f"h{n}"

    def exists(self, hand_id: str) -> bool:
        return self.path(hand_id).exists()

    def load(self, hand_id: str) -> dict:
        p = self.path(hand_id)
        if not p.exists():
            raise SpotError(f"no hand {hand_id!r} (see: arena hand list)")
        return json.loads(p.read_text())

    def save(self, hand_id: str, spot: Spot, created: float = None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        rec = {"id": hand_id, "created": created or time.time(),
               "updated": time.time(), "spot": spot.to_dict()}
        tmp = self.path(hand_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=2) + "\n")
        tmp.replace(self.path(hand_id))

    def delete(self, hand_id: str) -> None:
        self.load(hand_id)
        self.path(hand_id).unlink()

    def list(self) -> list:
        if not self.dir.is_dir():
            return []
        recs = [json.loads(p.read_text()) for p in self.dir.glob("*.json")]
        return sorted(recs, key=lambda r: r["created"])
