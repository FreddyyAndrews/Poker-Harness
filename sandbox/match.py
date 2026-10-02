"""
Upstream-compatible wrapper around arena.match.MatchRunner.

- run_match(match_id, bot_paths, ...) keeps the old signature and result
  shape (including per-hand results under "hands") for demo.py. Matches
  are also stored in runs/<match_id>/ unless store=False.
- Running this file forwards to `arena match run`:

    python3 sandbox/match.py bots/shark/bot.py bots/aggressor/bot.py --hands 400 --seed 1

Environment (kept from upstream): USE_DOCKER=true runs bots in the sandbox
container (SANDBOX_IMAGE, BOT_MEMORY, BOT_CPUS, BOT_TMPFS); ACTION_TIMEOUT
sets seconds per decision.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from arena.match import MatchConfig, MatchRunner, make_bot_seats
from arena.runs import RunWriter

SANDBOX_IMAGE  = os.environ.get("SANDBOX_IMAGE", "poker-harness-sandbox:latest")
USE_DOCKER     = os.environ.get("USE_DOCKER", "false").lower() == "true"
ACTION_TIMEOUT = float(os.environ.get("ACTION_TIMEOUT", "2"))


def make_seats(bot_paths: dict) -> dict:
    return make_bot_seats(
        bot_paths,
        timeout       = ACTION_TIMEOUT,
        docker_image  = SANDBOX_IMAGE if USE_DOCKER else None,
        docker_memory = os.environ.get("BOT_MEMORY", "768m"),
        docker_cpus   = os.environ.get("BOT_CPUS", "0.5"),
        docker_tmpfs  = os.environ.get("BOT_TMPFS", "20m"),
    )


def run_match(match_id, bot_paths, n_hands=400, verbose=False, seed=None, store=True):
    runner = MatchRunner(
        match_id, make_seats(bot_paths), MatchConfig(n_hands=n_hands, seed=seed),
        writer=RunWriter(match_id) if store else None,
        keep_hands=True, verbose=verbose,
    )
    return asyncio.run(runner.run())


if __name__ == "__main__":
    from arena.cli.main import main

    argv = ["match", "run"]
    args = sys.argv[1:]
    # upstream flag name
    args = ["--id" if a == "--match-id" else a for a in args]
    if USE_DOCKER:
        argv += ["--docker", "--image", SANDBOX_IMAGE]
    if "ACTION_TIMEOUT" in os.environ:
        argv += ["--timeout", str(ACTION_TIMEOUT)]
    sys.exit(main(argv + args))
