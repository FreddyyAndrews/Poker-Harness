"""
Probes and sweeps: ask a bot what it would do in a position, without
playing a game.

A probe target is a position (an action_request state) plus an engine at
that position, used to work out how each reply would actually be applied
(the same lenient rules as a live match). Targets come from:

  - a spot (target_from_spot)
  - a decision in a stored match (target_from_run): the bot is sent the
    exact state it was sent in the match, including the real
    match_action_log rebuilt from the events

Every probe starts the bot in a fresh process, so nothing a probe does
can touch a match. With warm states (target_from_run(..., warm=True)),
the bot is first sent every earlier state its seat saw in that match, and
its answers are ignored: a bot that models opponents in memory then knows
what it knew at that point.

A sweep re-probes one spot across variants (bet size, hole cards, a board
card, a stack); see expand_vary for the syntax.

Results are stored under runs/probes/<probe_id>/: probe.json (what was
asked and the summary) and samples.jsonl (every answer), plus the bot's
stderr.log.
"""

import asyncio
import copy
import itertools
import json
import os
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import eval7

from poker_harness.index import action_kind
from poker_harness.match import bot_fingerprint
from poker_harness.replay import hand_to_spot, replay_hand
from poker_harness.runs import Run, runs_dir
from poker_harness.seats import SubprocessBotSeat
from poker_harness.spot import Spot, SpotError, resolve_seat

KINDS = ["fold", "check", "call", "bet", "raise"]


class ProbeError(ValueError):
    pass


@dataclass
class ProbeTarget:
    state: dict                 # what the bot is sent
    engine: object              # engine at this position (for previews)
    label: str                  # human description of where this is from
    source: dict                # {"spot": {...}} or {"match": id, "hand": n, "at": k}
    warm_states: list = field(default_factory=list)
    recorded: Optional[dict] = None     # the decision record, for --from


def target_from_spot(spot: Spot, label: str = None) -> ProbeTarget:
    eng, state = spot.to_engine(hand_id="probe")
    if state["type"] != "action_request":
        raise ProbeError("the spot's hand is over; nobody is to act")
    state = dict(state)
    state["match_action_log"] = []
    return ProbeTarget(state, eng, label or (spot.name or "spot"), {"spot": spot.to_dict()})


def target_from_run(run: Run, hand_num: int, at: Optional[int] = None,
                    warm: bool = False) -> ProbeTarget:
    """The decision before action `at` of a hand (default: the hand's last
    decision), exactly as its bot saw it."""
    events  = run.hand_events(hand_num)
    actions = [e for e in events if e["type"] == "action"]
    if not actions:
        raise ProbeError(f"hand {hand_num} has no decisions")
    # an arena record only has our own bot's decisions
    probeable = [i for i, e in enumerate(actions) if e.get("decision_id") is not None
                 and (run.meta.get("perspective") != "own" or e["bot_id"] == run.meta.get("me"))]
    if not probeable:
        raise ProbeError(f"hand {hand_num} has no decisions of yours to probe")
    at = probeable[-1] if at is None else at
    if not 0 <= at < len(actions):
        raise ProbeError(f"--at must be 0..{len(actions) - 1} for this hand")
    if at not in probeable:
        raise ProbeError(f"action {at} of hand {hand_num} was {actions[at]['bot_id']}'s, and this "
                         f"record only has {run.meta.get('me')}'s decisions; try --at "
                         + " or ".join(str(i) for i in probeable))
    ev  = actions[at]
    did = ev.get("decision_id")
    rec = next((d for d in run.decisions(ev["bot_id"]) if d["decision_id"] == did), None)
    if rec is None:
        raise ProbeError(f"no decision record for {run.match_id}:{hand_num} action {at}")

    limit = run.meta.get("config", {}).get("match_log_entries", 200)
    log   = _match_log_until(run, did)
    state = dict(rec["state"])
    state.pop("match_action_log_len", None)
    state["match_action_log"] = log[-limit:]

    eng, rstate = replay_hand(events, upto=at)
    if rstate.get("seat_to_act") != ev["seat"]:
        raise ProbeError("replay didn't reach the recorded decision")

    warm_states = []
    if warm:
        for d in run.decisions(ev["bot_id"]):
            if d["decision_id"] == did:            # ids are in order; strings in arena records
                break
            s = dict(d["state"])
            s.pop("match_action_log_len", None)
            s["match_action_log"] = _match_log_until(run, d["decision_id"])[-limit:]
            warm_states.append(s)

    return ProbeTarget(state, eng, f"{run.match_id}:{hand_num} action {at} ({ev['bot_id']})",
                       {"match": run.match_id, "hand": hand_num, "at": at,
                        "decision_id": did, "bot_id": ev["bot_id"]},
                       warm_states=warm_states, recorded=rec)


def _match_log_until(run: Run, decision_id: int) -> list:
    """match_action_log as the runner built it: applied actions of every
    decision before `decision_id`."""
    out = []
    for e in run.events():
        if e["type"] != "action":
            continue
        if e.get("decision_id") == decision_id:
            break
        out.append({"hand_num": e["hand_num"], "seat": e["seat"], "bot_id": e["bot_id"],
                    "action": e["action"], "amount": e["amount"]})
    return out


# ---------------------------------------------------------------------------
# Running probes
# ---------------------------------------------------------------------------

def _classify(target: ProbeTarget, raw) -> dict:
    eng     = target.engine
    seat    = target.state["seat_to_act"]
    applied = eng.preview(raw if isinstance(raw, dict) else {}, seat)
    total   = applied["amount"] if applied["action"] in ("raise", "all_in") else 0
    kind    = action_kind(applied["action"], total, eng.current_bet, eng.street)
    return {"applied": applied, "kind": kind,
            "to": total if kind in ("bet", "raise") else None,
            "all_in": applied["action"] == "all_in"}


async def run_probe(bot_path, target: ProbeTarget, n: int = 10, *, timeout: float = 2.0,
                    fresh: bool = False, docker_image: Optional[str] = None,
                    stderr_path=None, seat_id: str = "probe") -> dict:
    """Ask the bot n times. Returns {"samples": [...], "warm": {...}}."""
    samples, warm_info = [], {"states": len(target.warm_states), "errors": 0}

    async def new_seat():
        seat = SubprocessBotSeat(seat_id, bot_path, timeout=timeout,
                                 docker_image=docker_image, stderr_path=stderr_path)
        await seat.start()
        for s in target.warm_states:
            d = await seat.act(copy.deepcopy(s))
            warm_info["errors"] += bool(d.error)
        return seat

    seat = await new_seat()
    try:
        for i in range(n):
            if fresh and i:
                await seat.close()
                seat = await new_seat()
            d = await seat.act(copy.deepcopy(target.state))
            samples.append({"i": i, "response": d.action, **_classify(target, d.action),
                            "error": d.error, "detail": d.detail, "logs": d.logs,
                            "bot_ms": d.bot_ms, "elapsed_ms": d.elapsed_ms})
    finally:
        await seat.close()
    return {"samples": samples, "warm": warm_info, "status": seat.status_detail}


def summarize(samples: list, target: ProbeTarget) -> dict:
    n = len(samples)
    kinds = Counter(s["kind"] for s in samples)
    sizes = Counter(s["to"] for s in samples if s["to"])
    bb    = target.state.get("big_blind") or 100
    pot   = target.state["pot"]
    ms    = [s["bot_ms"] for s in samples if s["bot_ms"] is not None]
    errs  = Counter(s["error"] for s in samples if s["error"])
    notes = {}
    for s in samples:
        if s["logs"] and s["kind"] not in notes:
            first = s["logs"][0]
            notes[s["kind"]] = ((first.get("msg") or "") + " " +
                                (json.dumps(first["data"]) if first.get("data") else "")).strip()
    return {
        "n": n,
        "freq": {k: kinds[k] / n for k in KINDS if kinds[k]},
        "counts": {k: kinds[k] for k in KINDS if kinds[k]},
        "sizes": [{"to": to, "count": c, "bb": round(to / bb, 1), "pot": round(to / pot, 2)}
                  for to, c in sizes.most_common()],
        "all_in": sum(1 for s in samples if s["all_in"]),
        "errors": dict(errs),
        "bot_ms_median": round(statistics.median(ms), 2) if ms else None,
        "bot_ms_max": max(ms) if ms else None,
        "notes": notes,
        "modal": kinds.most_common(1)[0][0] if n else None,
    }


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------

_DECK = [r + s for r in "23456789TJQKA" for s in "shdc"]
_BOARD_INDEX = {"flop1": 0, "flop2": 1, "flop3": 2, "turn": 3, "river": 4}


def _values(text: str) -> list:
    """'200..2000:200' | '300,600,900' -> ints"""
    m = re.fullmatch(r"(\d+)\.\.(\d+)(?::(\d+))?", text)
    if m:
        lo, hi, step = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if not step:
            step = max(1, (hi - lo) // 10)
        if lo > hi:
            raise ProbeError(f"range {text!r} goes backwards")
        return list(range(lo, hi + 1, step))
    try:
        return [int(v) for v in text.split(",")]
    except ValueError:
        raise ProbeError(f"expected numbers like 200..2000:200 or 300,600, got {text!r}") from None


def _hand_class(c1: str, c2: str) -> str:
    order = "23456789TJQKA"
    a, b = sorted([c1, c2], key=lambda c: order.index(c[0]), reverse=True)
    if a[0] == b[0]:
        return a[0] + b[0]
    return a[0] + b[0] + ("s" if a[1] == b[1] else "o")


def _combos(text: str) -> list:
    """'AsKh,QdQc' or an eval7 range 'QQ+,AKs' -> [(label, 'AsKh'), ...]"""
    items = [t.strip() for t in text.split(",") if t.strip()]
    if all(re.fullmatch(r"[2-9TJQKA][shdc][2-9TJQKA][shdc]", t, re.I) for t in items):
        return [(t, t) for t in items]
    try:
        hands = eval7.HandRange(text).hands
    except Exception:
        raise ProbeError(f"can't parse hands or range {text!r}") from None
    return [(_hand_class(str(a), str(b)), str(a) + str(b)) for (a, b), _ in hands]


def expand_vary(spot: Spot, varies: list, max_variants: int = 300) -> list:
    """Each --vary expression -> list of (label, edit-function); the result
    is the cartesian product: [(labels, spot_dict)].

      bet=200..2000:200      the last raise in the actions (what the bot faces)
      bet=300,600,900
      cards.BTN=AsKh,QdQc    a seat's hole cards: explicit hands
      cards.BTN=QQ+,AKs      ... or a range; results group by hand class
      turn=*  river=Ah,Kd    a board card (flop1/flop2/flop3/turn/river); * = every card
      stack.BB=1000..5000:1000
    """
    axes = []
    base = spot.to_dict()
    for expr in varies:
        if "=" not in expr:
            raise ProbeError(f"--vary takes FIELD=VALUES, got {expr!r}")
        key, val = expr.split("=", 1)
        key = key.strip()
        axis = []
        if key == "bet":
            idx = max((i for i, a in enumerate(spot.actions) if a.action == "raise"), default=None)
            if idx is None:
                raise ProbeError("bet=: the spot's actions have no raise to vary")
            for v in _values(val):
                axis.append((f"bet {v}", ("bet", idx, v)))
        elif key.startswith("cards."):
            seat = resolve_seat(key.split(".", 1)[1], spot)
            for label, hand in _combos(val):
                axis.append((label, ("cards", seat, hand)))
        elif key in _BOARD_INDEX:
            pos = _BOARD_INDEX[key]
            cards = _DECK if val.strip() == "*" else [c.strip() for c in val.split(",")]
            for c in cards:
                axis.append((f"{key} {c}", ("board", pos, c)))
        elif key.startswith("stack."):
            seat = resolve_seat(key.split(".", 1)[1], spot)
            for v in _values(val):
                axis.append((f"s{seat} stack {v}", ("stack", seat, v)))
        else:
            raise ProbeError(f"can't vary {key!r}; use bet, cards.SEAT, flop1..river or stack.SEAT")
        axes.append(axis)

    combos = list(itertools.product(*axes))
    if len(combos) > max_variants:
        raise ProbeError(f"{len(combos)} variants is more than --max-variants {max_variants}")
    out = []
    for combo in combos:
        s = copy.deepcopy(spot)
        labels = []
        for label, (kind, a, b) in combo:
            labels.append(label)
            if kind == "bet":
                s.actions[a].amount = b
            elif kind == "cards":
                s.cards[a] = [b[:2], b[2:]]
            elif kind == "board":
                s.board[a] = b
            elif kind == "stack":
                s.stacks[a] = b
        s.to_act = spot.to_act
        out.append((" · ".join(labels), s))
    return out


async def run_sweep(bot_path, variants: list, n: int, *, jobs: int = 4, timeout: float = 2.0,
                    docker_image: Optional[str] = None, stderr_dir=None) -> list:
    """Probe every (label, spot) variant; invalid variants are reported,
    not run. Returns one row per variant in order."""
    sem = asyncio.Semaphore(jobs)

    async def one(i, label, spot):
        try:
            target = target_from_spot(spot, label=label)
        except (SpotError, ProbeError, ValueError) as e:
            return {"i": i, "label": label, "invalid": str(e)}
        async with sem:
            res = await run_probe(bot_path, target, n, timeout=timeout, docker_image=docker_image,
                                  stderr_path=(Path(stderr_dir) / "stderr.log") if stderr_dir else None,
                                  seat_id=f"sweep{i}")
        return {"i": i, "label": label, "samples": res["samples"],
                "summary": summarize(res["samples"], target),
                "facing": target.state["amount_owed"], "pot": target.state["pot"]}

    return await asyncio.gather(*(one(i, l, s) for i, (l, s) in enumerate(variants)))


def group_rows(rows: list) -> list:
    """Merge variants with the same label (e.g. every combo of AKs)."""
    merged, order = {}, []
    for r in rows:
        key = r["label"]
        if key not in merged:
            merged[key] = {"label": key, "samples": [], "invalid": [], "variants": 0}
            order.append(key)
        m = merged[key]
        m["variants"] += 1
        if "invalid" in r:
            m["invalid"].append(r["invalid"])
        else:
            m["samples"] += r["samples"]
            m.setdefault("pot", r["pot"])
            m.setdefault("facing", r["facing"])
    return [merged[k] for k in order]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def probes_dir() -> Path:
    return runs_dir() / "probes"


def new_probe_dir(kind: str) -> Path:
    pid = f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"
    d = probes_dir() / pid
    d.mkdir(parents=True)
    return d


def save_probe(d: Path, info: dict, samples: list) -> None:
    (d / "probe.json").write_text(json.dumps({"id": d.name, **info}, indent=2, default=repr) + "\n")
    with open(d / "samples.jsonl", "w") as f:
        for s in samples:
            f.write(json.dumps(s, default=repr) + "\n")


def load_probe(probe_id: str) -> tuple:
    d = Path(probe_id) if (Path(probe_id) / "probe.json").exists() else probes_dir() / probe_id
    if not (d / "probe.json").exists():
        raise ProbeError(f"no probe {probe_id!r} in {probes_dir()}/")
    info = json.loads((d / "probe.json").read_text())
    samples = [json.loads(l) for l in open(d / "samples.jsonl") if l.strip()]
    return d, info, samples


def list_probes() -> list:
    d = probes_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if (p / "probe.json").exists():
            out.append(json.loads((p / "probe.json").read_text()))
    return out


def bot_info(path) -> dict:
    return {"path": str(path), "version": bot_fingerprint(path)}
