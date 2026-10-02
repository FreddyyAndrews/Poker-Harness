# Poker Harness

> An LLM poker arena: coding agents write poker bots, play them against each
> other (and against humans), read structured logs of what happened, and
> improve their bots over time.

This is a fork of [fullhouse-engine](https://github.com/uzlez/fullhouse-engine),
the engine from the 2026 Fullhouse Hackathon. We keep its No-Limit Hold'em
engine and bot protocol, and build an arena around them.

**Status:** early. The upstream engine, match runner and reference bots work
today (see [What works today](#what-works-today)). Everything else in this
README is the plan.

---

## The research question

How should an LLM manage its context to play well over long poker sessions?

A bot in this arena can call LLMs while it decides what to do. Across hundreds
of hands and many matches, it has to decide what to remember, what to
summarise, what to forget, and what to carry into the next match. The arena
makes that measurable:

- **Bots** play heads-up or at full tables (2–9 seats).
- **Coding agents** write and revise bots, using a CLI to build test
  situations, probe their bots' decisions and query match logs.
- **Humans** watch matches live, step through replays, and sit in to play the
  bots directly.
- **Everything is logged**: every action, every bot's internal notes, and every
  LLM prompt and response.

---

## Concepts

| Term | Meaning |
|------|---------|
| **Bot** | A `bot.py` with a `decide(state, ctx)` function. It doesn't act on its own; it only answers when asked for a decision. |
| **Controller** | Whoever manages bots: a coding agent, a scheduler, or a human. Controllers create tables, queue bots and read results. |
| **Seat** | A place at a table. It can hold a hosted bot, a human, or (later) a remote client. |
| **Table / match** | A game with a mode (heads-up or N-max), a number of hands, blinds, time per decision and an LLM budget. |
| **Spot** | A poker situation set up on purpose: chosen hole cards, board, stacks and action so far. Used for testing bots. |
| **Probe** | Asking a bot "what would you do here?" without moving a game forward or changing the bot's memory. |
| **God view** | Seeing every player's hole cards and the full deck. |

---

## Planned architecture

```
           Frontend (lobby / watch / replay / play)
                    │  WebSocket + REST
          ┌─────────▼──────────┐        arena CLI / MCP
          │   Arena server     │◄────── (coding agents)
          │ lobby, matchmaking │
          └─────────┬──────────┘
                    │
          MatchRunner (async) ──► EventStore (JSONL per match + SQLite index)
           │        │         │
       BotSeat   LLMBotSeat  HumanSeat     ← one Seat.act(state) interface
           │        │
     runner.py ◄──► LLM broker (host side: API keys, model allowlist,
                                budgets, logging of every call)
```

### Engine
- Based on `engine/game.py`. The poker rules stay as they are.
- Seats stay fixed for the whole match (today they're renumbered when a player
  busts), and each bot's state includes `dealer_seat`.
- A full-information `hand_start` event (every player's hole cards, the deck
  order) goes to the log only. Bots never see it.
- Rigged deals and starting mid-hand, so any situation can be built and
  replayed exactly.

### Bots and LLM access
- Bots receive a `ctx` object: `decide(state, ctx)`.
  - `ctx.llm(messages, model=...)`: an LLM call made **by the host**, not the
    bot. The bot process has no network access and never holds API keys.
  - `ctx.log(...)`: structured notes saved with each decision.
  - `ctx.memory`: a per-bot store that persists across matches.
- The host enforces which models are allowed and token/cost limits per
  decision, match and bot, and logs every call.
- Time limits are set per seat: fast for rule-based bots, longer for LLM bots,
  none for humans.

### Logs and storage
```
runs/<match_id>/
  meta.json                      config, seats, seed, ranked/unranked
  events.jsonl                   full timeline with god view; replay uses this
  bots/<bot_id>/decisions.jsonl  state seen, action, ctx.log notes, LLM calls, timing
  bots/<bot_id>/stderr.log
```
A SQLite index lets you query across matches, for example "biggest losing
hands for bot X, with its reasoning".

### Arena server and lobby
- Tables go through `open → filling → running → finished`.
- Controllers can create tables, seat bots, or queue a bot for a game mode
  and let the matchmaker fill tables (with per-mode ratings).
- A scheduler runs ladders, round-robins and long unattended simulations.
- Bot-only matches run as fast as possible. Viewers watch at a speed they
  choose (1×, 4×, step by step). Tables with a human run at human speed.

### Who can see what
| Viewer | Hole cards visible |
|--------|--------------------|
| Seated bot | Its own cards only, always |
| Human player | Own cards, or god view if they turn it on |
| Spectator | Public view (showdown only) or god view |
| Anyone, after the match | Everything, including bot reasoning and LLM calls |

Bots and the agents controlling them can **never** see hidden information in a
live match they're playing in. Any match with a god-view seat, a rigged deal or
an edited state is marked `unranked`. It's still logged, but it stays out of
ratings and out of the default data agents learn from.

### Frontend
- **Lobby:** create and join tables, pick bots and seats, see running matches.
- **Live table:** watch with either public or god view.
- **Replay:** scrub through any match one action at a time.
- **Play:** take a seat against the bots, with god view optional.
- **Bot inspector:** each decision's notes and LLM reasoning.
- **Edit and fork:** stop a replay at any decision, change something, then
  continue as a new match or a probe.

### `arena` CLI (built for coding agents)
- Commands never wait for interactive input.
- Default output is terse, with `--json` and `--verbose` when needed.
- Every result has an ID, so agents only fetch full details when they need
  them.

```bash
# describe any situation in one line; unspecified cards are random (seedable)
arena spot --players 6 --button 3 --stacks 10000 \
  --cards "s0=AsKh s4=QdQc" --board "Kd7c2s|9h|" \
  --actions "pre: s4 r300, s0 r1000, s4 c; flop: s4 x" --to-act s0

arena probe mybot <spot> -n 20                  # how often it picks each action, plus short reasoning
arena sweep mybot <spot> --vary bet=200..2000:200
arena runout <spot> --seats mybot,shark --runs 500   # play to the end many times; EV per bot
arena hand new|act|state <id>                   # step through a hand, controlling every seat
arena equity AsKh QdQc --board Kd7c2s
arena test mybot --suite river-spots            # saved spots with expected actions
```

`spot`, `hand` and `equity` work offline, straight against the engine. Probes
run in a separate bot process and can't write to the bot's memory.

---

## Roadmap

1. **Core:** the `Seat` interface, fixed seats, async match runner, event
   store, rigged deals, starting mid-hand, `arena spot/hand/equity`. Also
   fix two bot I/O bugs: stderr is never read (a bot that logs a lot stalls),
   and a bot's `print()` breaks the action protocol.
2. **Logs:** per-bot decision logs, log queries, `arena probe/sweep/test`.
3. **Server and frontend:** lobby, live view, replay, bot inspector.
4. **Human seats:** play mode with optional god view, edit and fork from
   replay.
5. **LLM bots:** the host-side broker, `ctx.llm` / `ctx.memory`, budgets.
6. **Agent loop:** matchmaking, ratings, scheduler, MCP server, and workflows
   where agents write, test and improve bots.

### Open decisions
- Which LLM providers to support (Anthropic only, or several via LiteLLM),
  and whether bots pick models or the harness assigns them.
- Whether to keep Docker isolation, or rely on subprocess isolation plus the
  broker for local-only use.
- Frontend stack (React/Vite + FastAPI is the working assumption).
- Single user (one machine) or multiple users with accounts.
- Whether to support remote bot seats (outside processes playing over
  WebSocket).

---

## What works today

Everything below is inherited from upstream and still works.

### Install

Use Python 3.10. eval7 doesn't build on 3.11+.

```bash
make install     # installs Cython<3, then eval7 with --no-build-isolation, then the rest
make test        # engine unit tests
```

### Run a match

```bash
python3 sandbox/match.py bots/shark/bot.py bots/aggressor/bot.py --hands 400 --seed 1
```

Bots can be passed as a `.py` file, a directory containing `bot.py` (plus an
optional `data/`), or a `.zip`. With `--seed`, the same bots get the same cards
every time.

### Demo UI

```bash
python3 demo.py   # http://localhost:5001  (DEMO_PORT to change)
```

Six reference bots play single matches or a 3-round Swiss tournament, with a
live log and hand replay. The planned frontend will replace this.

### The bot contract (current)

```python
def decide(game_state: dict) -> dict:
    return {"action": "call"}
```

The state includes:
- `your_cards`, `community_cards`, `street`
- `pot`, `your_stack`, `amount_owed`, `can_check`
- `current_bet`, `min_raise_to`, `your_bet_this_street`
- `seat_to_act`, `players` (public info), `action_log` (this hand)
- `match_action_log` (the last 200 actions across the match)

Before hand 1, `decide()` is called once with `{"type": "warmup"}` and a 30s
time limit.

Valid actions are `fold`, `check`, `call`, `{"action": "raise", "amount": N}`
(N is the **total** bet, not the amount added on top) and `all_in`. Out-of-range
raises are adjusted to a legal amount, and invalid actions become folds.

Rules today:
- 2s per decision (`ACTION_TIMEOUT`).
- `sandbox/validator.py` rejects network, subprocess, threading and pickle
  imports, and `eval`/`exec`.

The LLM broker will relax these rules for approved LLM calls.

### Repo layout

```
engine/game.py        NLHE rules for one hand (eval7 hand evaluation, side pots, events)
engine/tournament.py  Swiss pairing and standings
sandbox/match.py      multi-hand match runner; bots run as subprocesses or in Docker
sandbox/runner.py     the bot side: loads bot.py, JSON over stdin/stdout, timeouts
sandbox/validator.py  checks bot code before accepting it
sandbox/Dockerfile    isolated bot container (no network, read-only, 768 MB, 0.5 CPU)
bots/                 reference bots: template, aggressor, mathematician, shark, ref_bot_2
db/schema.sql         upstream's hosted-site schema (not used here)
demo.py               Flask demo UI
```

---

## Credits

Built on [fullhouse-engine](https://github.com/uzlez/fullhouse-engine)
(MIT, © 2026 Fullhouse Hackathon). Hand evaluation by
[eval7](https://github.com/julianandrews/pyeval7).
