"""
Compare two bots (or two versions of one) with most of the card luck
removed.

Poker results are noisy: over a few hundred hands, who got the better
cards swamps who played better. Duplicate evaluation plays every deal
several times with the seats rotated, so each player receives every
seat's cards on every deal and the luck largely cancels.

All matches here use reset_stacks (every hand starts from the starting
stacks), so hands are independent and the same seed deals the same cards
to the same seat in every rotation.

Two modes:

  head-to-head   A vs B heads-up. With duplicate, each deal is played
                 twice with A and B swapping seats. The unit of
                 measurement is a deal: A's total over both rotations.

  field          A and B each play the same opponents (the field) on the
                 same deals. With duplicate, every player rotates through
                 every seat (n rotations for an n-seat table). The unit is
                 a deal: A's total over the rotations minus B's total in
                 exactly the same seats with the same cards.

Results are in big blinds per 100 hands, with a 95% confidence interval
over deals. The difference is "significant" when the interval excludes 0.
The naive interval (treating every hand as independent, as a plain match
would) is reported too, to show how much the duplicate design helped.
"""

import asyncio
import json
import math
import os
import random
import statistics
import time
from pathlib import Path
from typing import Callable, Optional

from arena.match import MatchConfig, MatchRunner, bot_fingerprint, bot_ids_for_paths, make_bot_seats
from arena.runs import RunWriter, runs_dir


class CompareError(ValueError):
    pass


def compares_dir() -> Path:
    return runs_dir() / "compares"


def new_compare_id() -> str:
    return f"c-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"


def plan(a: str, b: str, field: list, duplicate: bool = True) -> dict:
    """Which matches to play: {"ids": {...}, "tables": [(role, rotation, [bot ids in seat order])]}"""
    ids = bot_ids_for_paths([a, b] + list(field))
    paths = dict(zip(ids, [a, b] + list(field)))
    ida, idb, fids = ids[0], ids[1], ids[2:]
    if len(fids) > 7:
        raise CompareError("the field can have at most 7 bots (tables seat 9)")
    lineups = {"A": [ida] + fids, "B": [idb] + fids} if fids else {"h2h": [ida, idb]}
    tables = []
    for role, lineup in lineups.items():
        n = len(lineup)
        for r in (range(n) if duplicate else [0]):
            tables.append((role, r, lineup[r:] + lineup[:r]))
    return {"a": ida, "b": idb, "field": fids, "paths": paths, "tables": tables,
            "mode": "field" if fids else "head-to-head"}


def _interval(values: list, scale: float) -> dict:
    n = len(values)
    mean = sum(values) / n
    se = statistics.stdev(values) / math.sqrt(n) if n > 1 else float("nan")
    return {"bb_per_100": round(mean * scale, 2),
            "ci95": [round((mean - 1.96 * se) * scale, 2), round((mean + 1.96 * se) * scale, 2)],
            "se": se * scale}


def analyse(p: dict, hands_by_table: dict, big_blind: int) -> dict:
    """hands_by_table: {(role, rotation): [per-hand {bot_id: delta}]}"""
    rotations = {}
    for (role, r) in hands_by_table:
        rotations.setdefault(role, []).append(r)
    n_hands = min(len(h) for h in hands_by_table.values())
    bb = big_blind

    def deal_totals(role, bot):
        """per deal: the bot's average result per hand over the rotations, in bb"""
        rs = rotations[role]
        return [sum(hands_by_table[(role, r)][k][bot] for r in rs) / len(rs) / bb
                for k in range(n_hands)]

    def naive(role, bot):
        return [hands_by_table[(role, r)][k][bot] / bb
                for r in rotations[role] for k in range(n_hands)]

    out = {"deals": n_hands, "rotations": {k: len(v) for k, v in rotations.items()}}
    if p["mode"] == "head-to-head":
        a = deal_totals("h2h", p["a"])
        out["a"] = _interval(a, 100)
        out["b"] = {"bb_per_100": -out["a"]["bb_per_100"]}
        out["diff"] = out["a"]
        out["naive"] = _interval(naive("h2h", p["a"]), 100)
    else:
        a = deal_totals("A", p["a"])
        b = deal_totals("B", p["b"])
        out["a"] = _interval(a, 100)
        out["b"] = _interval(b, 100)
        out["diff"] = _interval([x - y for x, y in zip(a, b)], 100)
        na, nb = naive("A", p["a"]), naive("B", p["b"])
        # naive: independent hands for each side, no pairing
        se = math.sqrt(statistics.variance(na) / len(na) + statistics.variance(nb) / len(nb)) * 100
        d = out["diff"]["bb_per_100"]
        out["naive"] = {"se": se, "ci95": [round(d - 1.96 * se, 2), round(d + 1.96 * se, 2)]}
    lo, hi = out["diff"]["ci95"]
    out["verdict"] = ("a_better" if lo > 0 else "b_better" if hi < 0 else "no_difference")
    se, nse = out["diff"]["se"], out["naive"]["se"]
    out["variance_reduction"] = round(nse / se, 2) if se == se and se > 1e-9 else None
    return out


async def run_compare(a: str, b: str, field: list, *, hands: int = 400, seed: Optional[int] = None,
                      duplicate: bool = True, jobs: int = 4, timeout: float = 2.0,
                      small_blind: int = 50, big_blind: int = 100, stack: int = 10_000,
                      docker_image: Optional[str] = None, compare_id: Optional[str] = None,
                      progress: Optional[Callable] = None) -> dict:
    p = plan(a, b, field, duplicate)
    cid = compare_id or new_compare_id()
    seed = seed if seed is not None else random.randrange(1, 10**9)
    sem = asyncio.Semaphore(jobs)
    results = {}

    async def play(role, r, lineup):
        match_id = f"{cid}-{role}-r{r}"
        seats = make_bot_seats({bid: p["paths"][bid] for bid in lineup}, timeout=timeout,
                               docker_image=docker_image)
        cfg = MatchConfig(n_hands=hands, small_blind=small_blind, big_blind=big_blind,
                          starting_stack=stack, seed=seed, ranked=False, reset_stacks=True)
        runner = MatchRunner(match_id, seats, cfg, writer=RunWriter(match_id), keep_hands=True,
                             labels={"compare": cid, "role": role, "rotation": r})
        async with sem:
            res = await runner.run()
        results[(role, r)] = res
        if progress:
            progress(f"  {match_id}: " + ", ".join(f"{k} {v:+,}" for k, v in res["chip_delta"].items()))

    await asyncio.gather(*(play(role, r, lineup) for role, r, lineup in p["tables"]))

    hands_by_table = {key: [h["delta"] for h in res["hands"]] for key, res in results.items()}
    stats = analyse(p, hands_by_table, big_blind)
    errors = {}
    for res in results.values():
        for bid, n in res["error_counts"].items():
            errors[bid] = errors.get(bid, 0) + n
    info = {
        "id": cid, "created": time.time(), "mode": p["mode"], "duplicate": duplicate,
        "a": {"id": p["a"], "path": a, "version": bot_fingerprint(a)},
        "b": {"id": p["b"], "path": b, "version": bot_fingerprint(b)},
        "field": [{"id": i, "path": p["paths"][i]} for i in p["field"]],
        "hands_per_match": hands, "seed": seed, "blinds": [small_blind, big_blind], "stack": stack,
        "matches": sorted(f"{cid}-{role}-r{r}" for role, r in results),
        "errors": errors, "stats": stats,
    }
    d = compares_dir() / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "compare.json").write_text(json.dumps(info, indent=2) + "\n")
    return info


def load_compare(cid: str) -> dict:
    path = compares_dir() / cid / "compare.json"
    if not path.exists():
        raise CompareError(f"no comparison {cid!r} in {compares_dir()}/")
    return json.loads(path.read_text())


def list_compares() -> list:
    d = compares_dir()
    if not d.is_dir():
        return []
    out = [json.loads((p / "compare.json").read_text())
           for p in d.iterdir() if (p / "compare.json").exists()]
    return sorted(out, key=lambda c: c["created"])
