# CLAUDE.md

Poker Harness is the toolkit library of a three-repo poker bot arena
modelled on lichess (this repo ~ python-chess plus dev tools;
poker-bot-template ~ lichess-bot; poker-arena ~ lila). All three are
public. See
README.md for the architecture and the plan (T1-T8).

- Everything here is public and used by both other repos: keep the
  engine's rules and the bot protocol identical for local development and
  production, and change the protocol only through docs/bot-api.md.

## Git workflow

- After each body of work is complete (a roadmap step, a feature, a fix),
  commit and push it. Don't leave finished work uncommitted.
- Split the work into logical commits (e.g. docs, build/restructure, feature
  + its tests) rather than one large commit.
- Commit messages: Conventional Commits style (`feat(engine): ...`,
  `fix(match): ...`, `docs: ...`, `build: ...`, `test: ...`). The subject says
  what changed; the body says why and lists notable behaviour changes.
- No Claude Code attribution in commits or PRs: no `Co-Authored-By: Claude`
  trailer and no "Generated with Claude Code" line.
- For now, commit directly on `main` and push to `origin main`. No feature
  branches until we decide on a branching workflow.
- Run `make test` before committing; don't commit with failing tests.

## Environment

- Python 3.10 only (eval7 0.1.7 doesn't build on 3.11+). The venv is `.venv/`;
  `make install` creates it.
- Tests: `make test` (or `.venv/bin/python -m pytest -q`).
- Quick end-to-end check:
  `.venv/bin/python sandbox/match.py bots/shark/bot.py bots/aggressor/bot.py --hands 200 --seed 1`

## Code notes

- `arena/engine/game.py` is the poker rules for one hand. Keep the rules
  covered by the fuzzers in `tests/test_engine_seats_rigging.py`. Any engine
  change must keep chip conservation and "never ask a busted or all-in seat
  to act".
- Seats are fixed for a whole match; a seat with no chips sits out.
- `arena/spot.py` owns the spot notation. Spots are always built by
  replaying actions through the engine in strict mode; don't add a second
  way to construct mid-hand states.
- To check poker behaviour quickly, use the CLI (`.venv/bin/arena spot ...`,
  `arena hand ...`, `arena equity ...`; `-h` on each). Example spots live in
  `spots/`.
- `arena/runner/bot_runner.py` runs inside the sandbox container, so it must
  stay standard-library only and must not import from `arena`.
- Docker runs through Colima here (`colima start` if `docker` can't connect).
  Colima only shares `$HOME` with containers, so anything mounted into one
  must live under it. The Docker test in `tests/test_seats.py` skips if the
  `poker-harness-sandbox:latest` image isn't built (`./sandbox.sh build`).
- Matches go through `arena/match.py` (MatchRunner) and are stored by
  `arena/runs.py`; its docstring is the event/record schema, so keep it in
  sync. Bump `SCHEMA_VERSION` for incompatible changes. Every hand must
  still replay exactly (`arena match verify`, `tests/test_match.py`).
- `runs/index.sqlite` (arena/index.py) is derived from runs/ and can be
  deleted at any time. Bump `INDEX_VERSION` whenever its schema or stat
  definitions change (old indexes are then cleared and rebuilt). Stats
  tests use seats with fixed behaviour so every number is known exactly.
- Tests must not write into the repo: point `ARENA_RUNS`, `ARENA_HOME` and
  `ARENA_SPOTS` at `tmp_path` (or pass a `root`).
- When working on a bot, start from `arena brief BOT`: it lists the
  result, the biggest leaks and the commands that drill into them.
- Judge whether a bot change helped with `arena compare NEW OLD` (duplicate
  deals, confidence interval), not with a single match's chip count.
- When changing a bot, run `arena test BOT --suite basics` (and any suite
  for that bot) before committing; exit code 1 means a spot failed.
- `sandbox/` and `demo.py` are upstream code that the roadmap will replace.
  Keep them working, but put new functionality in `arena/`.
