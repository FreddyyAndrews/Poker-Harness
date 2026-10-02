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

`spot`, `spots`, `hand` and `equity` exist today (see
[Using the arena CLI](#using-the-arena-cli)). Still planned:

```bash
arena probe mybot <spot> -n 20                  # how often it picks each action, plus short reasoning
arena sweep mybot <spot> --vary bet=200..2000:200
arena runout <spot> --seats mybot,shark --runs 500   # play to the end many times; EV per bot
arena test mybot --suite river-spots            # saved spots with expected actions
```

Probes will run in a separate bot process and can't write to the bot's
memory.

---

## Roadmap

1. **Core:** engine changes (fixed seats, configurable blinds,
   `legal_actions()`, strict mode, rigged deals) and spots / starting
   mid-hand / `arena spot/hand/equity`, and bot protocol v2 with the
   `Seat` interface (fixing the stderr stall and `print()` bugs) are
   **done**. Still to do: async match runner, event store.
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

### Using the arena CLI

`make install` puts an `arena` command in `.venv/bin`. Every command runs
without prompts, prints a short summary, and takes `--json` for the full
structure. Errors exit with code 2 (as `{"error": ...}` with `--json`).

**Describe a situation in one line.** Cards you don't fix are random from
`--seed`. God view shows every hole card, the undealt runout and each
player's equity:

```bash
arena spot --players 6 --button 3 --cards "BTN=AsKh BB=QdQc" --board "Kd7c2s|9h|" \
  --actions "pre: BTN r250, BB r900, BTN c; flop: BB r600" --to-act BTN --seed 1
```
```
spot · flop · pot 2,450 · s3 (BTN) to act   [seed 1, rigged]
board  Kd 7c 2s   (runout: 9h 6d)

   seat pos      stack    bet  state   cards  equity
   s0   UTG     10,000      -  folded  (Ts2c)
   ...
 > s3   BTN      9,100      -  active  AsKh     91.2%
   s5   BB       8,500    600  active  QdQc      8.8%

line   pre: s3 r250, s5 r900, s3 c; flop: s5 r600
legal  fold | call 600 | raise 1,200..9,100
```

| Field | Notation |
|---|---|
| `--cards` | `"s0=AsKh s4=QdQc"` or by position `"BTN=AsKh BB=QdQc"` |
| `--board` | `"Kd7c2s\|9h\|"` (flop\|turn\|river); missing or `??` = random |
| `--stacks` | `"10000"`, `"10000,5000,0"` (0 = busted, sits out), `"10000 s3=2500"` |
| `--blinds` | `"50/100"` |
| `--actions` | `"pre: BTN r250, BB c; flop: BB x"`: `f` fold, `x` check, `c` call, `rN` raise to N, `a` all-in |
| `--to-act` | seat expected to act next (checked) |

Positions are `BTN SB BB UTG UTG+1 UTG+2 LJ HJ CO`, or `s0`..`s8`. Naming a
seat that isn't next makes the seats in between check if they can and fold
otherwise, so `pre: BTN r250` means it folded to the button. Every spot is
replayed through the real rules in strict mode, so an impossible line is an
error, not a silently corrected one.

**Spot library.** `--save NAME` writes `spots/NAME.yaml`, and `arena spots`
lists the library. Use a saved spot by name, optionally overriding fields:
`arena spot hu-missed-flush-river-bet --cards "BTN=AhKh"`. `--as-bot` prints
the exact JSON the seat to act would receive, with no hidden information.

**Step through a hand, controlling every seat:**

```bash
arena hand new 6max-tptk-vs-flop-lead   # or any spot options; prints h1
arena hand act h1 c "BB r2000"          # several actions per call
arena hand act h1 x                     # illegal: error lists the legal actions
arena hand act h1 r100 --lenient        # corrected like a live match, with a note
arena hand undo h1                      # -n N to take back more
arena hand save h1 my-new-spot          # current position -> library
arena hand state h1 [--as-bot] | arena hand list | arena hand rm h1
```

Hands live in `.arena/hands/` (`ARENA_HOME` to move them); the library in
`spots/` (`ARENA_SPOTS`).

**Equity** (exact from the flop on, seeded Monte Carlo otherwise; ranges in
eval7 syntax, `any` for a random hand):

```bash
arena equity AsKh QdQc --board Kd7c2s
arena equity AsAh "QQ+,AKs" any
```

### Inherited from upstream

Everything below is inherited from upstream and still works.

### Install

Use Python 3.10. eval7 doesn't build on 3.11+ (on macOS: `brew install python@3.10`).

```bash
make install     # creates .venv, installs Cython<3, eval7 (--no-build-isolation), then this package
source .venv/bin/activate
make test        # engine tests, including fuzzers
```

### Run a match

```bash
python3 sandbox/match.py bots/shark/bot.py bots/aggressor/bot.py --hands 400 --seed 1
```

Bots can be passed as a `.py` file, a directory containing `bot.py` (plus an
optional `data/`), or a `.zip`. With `--seed`, the same bots get the same cards
every time.

To run every bot in its own locked-down container instead of a local
process:

```bash
brew install colima docker && colima start   # or Docker Desktop
./sandbox.sh build                           # builds poker-harness-sandbox:latest
./sandbox.sh security-check
USE_DOCKER=true python3 sandbox/match.py bots/shark/bot.py bots/aggressor/bot.py
```

Containers have no network, a read-only filesystem, 768 MB and half a core
(`BOT_MEMORY`, `BOT_CPUS` to change).

### Demo UI

```bash
python3 demo.py   # http://localhost:5001  (DEMO_PORT to change)
```

Six reference bots play single matches or a 3-round Swiss tournament, with a
live log and hand replay. The planned frontend will replace this.

### The bot contract

```python
def decide(state: dict, ctx) -> dict:      # or decide(state)
    ctx.log("3-bet bluff", equity=0.31)    # saved with this decision
    return {"action": "raise", "amount": 900}

def warmup(ctx):                           # optional: runs once, before hand 1
    ...                                    # (30s budget) - load tables here
```

The state includes:
- `your_cards`, `community_cards`, `street`
- `pot`, `your_stack`, `amount_owed`, `can_check`
- `current_bet`, `min_raise_to`, `your_bet_this_street`, `legal_actions`
- `seat_to_act`, `dealer_seat`, `hand_num`, `small_blind`, `big_blind`
- `players` (public info), `action_log` (this hand)
- `match_action_log` (the last 200 actions across the match)

`ctx.log(msg=None, **data)` records structured notes (up to 200 per decision,
16 KB each). `ctx.time_left()` is the seconds left in the decision's budget.
`print()` is safe: it goes to the bot's stderr log, as does anything else
written to stdout.

Valid actions are `fold`, `check`, `call`, `{"action": "raise", "amount": N}`
(N is the **total** bet, not the amount added on top) and `all_in`. Out-of-range
raises are adjusted to a legal amount, and invalid actions become folds.

When a decision fails, the bot checks if it can and folds otherwise:
- **Timeout** (2s, `ACTION_TIMEOUT`): the bot's process is also restarted,
  since its stuck code can't be stopped any other way. Module-level state is
  lost.
- **Crash**: restarted the same way.
- **Exception or bad return value**: no restart; the bot keeps its state.
- **Fails to load**: every decision checks/folds.

After 5 restarts in a match the bot stops being restarted and always
checks/folds.

`sandbox/validator.py` rejects network, subprocess, threading and pickle
imports, and `eval`/`exec`.

The protocol between host and bot is newline-delimited JSON; see
[arena/runner/bot_runner.py](arena/runner/bot_runner.py).

The LLM broker will relax these rules for approved LLM calls.

### Repo layout

```
arena/engine/game.py  NLHE rules for one hand: fixed seats, legal_actions(), strict mode,
                      rigged deals, side pots, events
arena/spot.py         spots: notation parser, YAML/JSON files, replay into the engine
arena/equity.py       showdown equity (exact / Monte Carlo, ranges)
arena/cli/            the `arena` command
arena/seats.py        Seat interface: bot processes/containers, scripted and callback seats
arena/runner/         bot side of protocol v2 (bot_runner.py, stdlib only) and bot packaging
arena/tournament.py   Swiss pairing and standings
spots/                the spot library
sandbox/match.py      multi-hand match runner; bots run as subprocesses or in Docker
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
