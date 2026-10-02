"""
Match runner: plays a multi-hand match between 2-9 bots.

Each bot runs in an arena.seats.SubprocessBotSeat: a local process by
default, or a Docker container with USE_DOCKER=true (build the image with
./sandbox.sh build). Seats are fixed for the match; a bot with no chips
sits out.

Submission formats (auto-detected from the path):
  - bot.py         single-file bot
  - bot/           directory containing bot.py + optional data/
  - bot.zip        archive containing bot.py at the root + optional data/

Phase 1d of the roadmap replaces this loop with the async MatchRunner and
the event store; the CLI below will stay as a thin wrapper.
"""

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from arena.engine.game import PokerEngine, STARTING_STACK, next_button
from arena.seats import SubprocessBotSeat

SANDBOX_IMAGE  = os.environ.get("SANDBOX_IMAGE", "poker-harness-sandbox:latest")
USE_DOCKER     = os.environ.get("USE_DOCKER", "false").lower() == "true"
ACTION_TIMEOUT = float(os.environ.get("ACTION_TIMEOUT", "2"))

# Container limits (USE_DOCKER=true only). 768 MB leaves room for bots that
# load lookup tables or model weights from data/ at import time.
CONTAINER_MEMORY     = os.environ.get("BOT_MEMORY", "768m")
CONTAINER_CPUS       = os.environ.get("BOT_CPUS",   "0.5")
CONTAINER_TMPFS_SIZE = os.environ.get("BOT_TMPFS",  "20m")

# Per-match rolling action log exposed to bots in state["match_action_log"].
# Lets bots build cross-hand opponent models within a match.
MATCH_LOG_MAX_ENTRIES = 200


def make_seat(bot_id, bot_path, on_event=None):
    return SubprocessBotSeat(
        bot_id, bot_path,
        timeout       = ACTION_TIMEOUT,
        on_event      = on_event,
        docker_image  = SANDBOX_IMAGE if USE_DOCKER else None,
        docker_memory = CONTAINER_MEMORY,
        docker_cpus   = CONTAINER_CPUS,
        docker_tmpfs  = CONTAINER_TMPFS_SIZE,
    )


# ---------------------------------------------------------------------------
# Match runner
# ---------------------------------------------------------------------------

def _inject_match_log(state, match_log):
    if state.get("type") == "action_request":
        state["match_action_log"] = match_log[-MATCH_LOG_MAX_ENTRIES:]
    return state


def run_match(match_id, bot_paths, n_hands=400, verbose=False, seed=None):
    return asyncio.run(_run_match(match_id, bot_paths, n_hands, verbose, seed))


async def _run_match(match_id, bot_paths, n_hands, verbose, seed):
    bot_ids = list(bot_paths.keys())
    n = len(bot_ids)
    assert 2 <= n <= 9, "Need 2-9 bots, got " + str(n)

    seat_events = []
    seats    = {bid: make_seat(bid, path, seat_events.append) for bid, path in bot_paths.items()}
    errors   = {bid: [] for bid in bot_ids}
    stacks   = {bid: STARTING_STACK for bid in bot_ids}
    hand_log = []
    match_action_log = []
    dealer = None
    start_ts = time.time()

    # Start every bot in parallel; each gets its warmup budget to import.
    await asyncio.gather(*(s.start() for s in seats.values()))
    for bid, s in seats.items():
        if s.status != "ready":
            errors[bid].append(f"{s.status}: {s.status_detail}")

    try:
        for hand_num in range(n_hands):
            # Seats are fixed for the match; busted bots sit out.
            if sum(1 for bid in bot_ids if stacks[bid] > 0) < 2:
                break

            dealer = next_button([stacks[bid] for bid in bot_ids], dealer)
            hand_id = match_id + "_h" + str(hand_num).zfill(4)
            hand_seed = (seed * 1000003 + hand_num) if seed is not None else None
            engine = PokerEngine(
                hand_id        = hand_id,
                bot_ids        = bot_ids,
                dealer_seat    = dealer,
                starting_stacks= dict(stacks),
                seed           = hand_seed,
                hand_num       = hand_num,
            )

            result = await _play_hand(engine, seats, bot_ids, errors,
                                      match_action_log, hand_num, verbose)
            hand_log.append({"hand_num": hand_num, "hand_id": hand_id, **result})

            for bid, s in result["final_stacks"].items():
                stacks[bid] = s

            if verbose and hand_num % 25 == 0:
                _print_stacks(hand_num, n_hands, stacks)

    finally:
        await asyncio.gather(*(s.close() for s in seats.values()))

    return {
        "match_id":     match_id,
        "bot_ids":      bot_ids,
        "seed":         seed,
        "n_hands":      len(hand_log),
        "duration_s":   round(time.time() - start_ts, 2),
        "final_stacks": stacks,
        "chip_delta":   {bid: stacks[bid] - STARTING_STACK for bid in bot_ids},
        "bot_errors":   errors,
        "bot_events":   seat_events,
        "hands":        hand_log,
    }


async def _play_hand(engine, seats, bot_ids, errors, match_action_log, hand_num, verbose):
    state = _inject_match_log(engine.start_hand(), match_action_log)
    steps = 0

    while state.get("type") == "action_request":
        seat     = state["seat_to_act"]
        bot_id   = bot_ids[seat]
        decision = await seats[bot_id].act(state)
        action   = decision.action
        if decision.error:
            errors[bot_id].append(decision.error)

        if verbose:
            note = f"  ({decision.error})" if decision.error else ""
            print("  [" + bot_id + "] " + str(action) + note, file=sys.stderr)

        match_action_log.append({
            "hand_num": hand_num,
            "seat":     seat,
            "bot_id":   bot_id,
            "action":   action.get("action"),
            "amount":   action.get("amount"),
        })

        state = _inject_match_log(engine.apply_action(seat, action), match_action_log)
        steps += 1

        if steps > 1000:
            raise RuntimeError("Hand exceeded 1000 steps: " + engine.hand_id)

    return state


def _print_stacks(hand_num, total, stacks):
    print("\n  === Hand " + str(hand_num) + "/" + str(total) + " ===", file=sys.stderr)
    for bid, s in sorted(stacks.items(), key=lambda x: -x[1]):
        bar = "X" * (s // 1000)
        print("  " + bid.ljust(20) + " " + str(s).rjust(7) + "  " + bar, file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI entrypoint for local testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run a Fullhouse match locally")
    parser.add_argument("bots", nargs="+",
                        help="Paths to bot.py files, bot directories, or bot.zip archives")
    parser.add_argument("--hands", type=int, default=400)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true", help="Output result as JSON (for worker)")
    parser.add_argument("--match-id", default=None)
    parser.add_argument("--seed", type=int, default=None,
                        help="Deterministic seed — same seed + same bots = same cards. Useful for reproducing a match locally.")
    args = parser.parse_args()

    paths = {}
    for i, path in enumerate(args.bots):
        pp = Path(path)
        suffix = pp.suffix
        # Default bot_id from filename, but if it would collide (very common
        # with the "bots/<name>/bot.py" layout where every stem is "bot"),
        # fall back to the parent directory name. If that also collides
        # (e.g. the same bot path passed twice for self-play testing),
        # append a numeric suffix so every entry is unique.
        if suffix in (".py", ".zip"):
            bot_id = pp.stem
        else:
            bot_id = pp.name or "bot_" + str(i)
        if bot_id in paths or bot_id in ("bot",):
            bot_id = pp.parent.name or ("bot_" + str(i))
        base = bot_id or "bot_" + str(i)
        bot_id = base
        n = 2
        while bot_id in paths:
            bot_id = base + "_" + str(n)
            n += 1
        paths[bot_id] = path

    match_id = args.match_id or os.environ.get("MATCH_ID") or "local_" + uuid.uuid4().hex[:8]

    if not args.json:
        print("Starting match " + match_id + " with " + str(len(paths)) + " bots, " + str(args.hands) + " hands\n")

    result = run_match(match_id, paths, n_hands=args.hands, verbose=args.verbose, seed=args.seed)

    if args.json:
        print(json.dumps({
            "match_id":     result["match_id"],
            "seed":         result["seed"],
            "n_hands":      result["n_hands"],
            "duration_s":   result["duration_s"],
            "final_stacks": result["final_stacks"],
            "chip_delta":   result["chip_delta"],
            "bot_errors":   result["bot_errors"],
        }))
        sys.exit(0)

    print("\n" + "=" * 50)
    print("Match complete in " + str(result["duration_s"]) + "s")
    print("=" * 50)
    print("Bot".ljust(25) + " " + "Final Stack".rjust(12) + " " + "Delta".rjust(10))
    print("-" * 50)
    for bid in sorted(result["bot_ids"], key=lambda b: -result["final_stacks"][b]):
        delta = result["chip_delta"][bid]
        sign  = "+" if delta >= 0 else ""
        print(bid.ljust(25) + " " + str(result["final_stacks"][bid]).rjust(12) + " " + sign + str(delta).rjust(9))
    print("\nHands played: " + str(result["n_hands"]))

    errs = {b: e for b, e in result["bot_errors"].items() if e}
    if errs:
        print("\nBot errors: " + str(errs))
