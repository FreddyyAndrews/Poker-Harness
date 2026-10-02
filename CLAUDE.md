# CLAUDE.md

Poker Harness is an LLM poker arena built on a fork of fullhouse-engine. See
README.md for the goals, the planned architecture and the roadmap.

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
- `sandbox/` and `demo.py` are upstream code that the roadmap will replace.
  Keep them working, but put new functionality in `arena/`.
