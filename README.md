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
Each match is written to `runs/<match_id>/` as it plays (this part exists;
see [Run a match](#run-a-match)):
```
runs/<match_id>/
  meta.json                   config, seats, seed, bot versions, status, result
  events.jsonl                full timeline with god view; replay uses this
  bots/<bot>/decisions.jsonl  state seen, reply, applied action, ctx.log notes, timing
  bots/<bot>/stderr.log
```
`runs/index.sqlite` indexes every finished run for queries across
matches (see [Query across matches](#query-across-matches)). Still
planned: LLM calls in the decision records (phase 5).

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

`spot`, `spots`, `hand`, `equity`, `match`, the queries and
`probe`/`sweep` exist today (see [Using the arena CLI](#using-the-arena-cli)).
Still planned:

```bash
arena runout <spot> --seats mybot,shark --runs 500   # play to the end many times; EV per bot
arena test mybot --suite river-spots            # saved spots with expected actions
```

---

## Roadmap

1. **Core** (**done**): engine changes (fixed seats, configurable blinds,
   `legal_actions()`, strict mode, rigged deals, uncalled bets), spots and
   `arena spot/hand/equity`, bot protocol v2 with the `Seat` interface,
   the async match runner, the run store and replay.
2. **Logs and evaluation:** the cross-match index and queries
   (`arena stats/hands/decisions/sql`, 2a) and `arena probe/sweep` (2b)
   are **done**. Still to do: test suites with expected answers (2c),
   duplicate-deal comparisons with confidence intervals (2d), and short
   briefs for agents (2e).
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
arena match run bots/shark/bot.py bots/aggressor/bot.py bots/template/bot.py --hands 400 --seed 1
```
```
match 20261002-141530-ab12 · 400 hands · seed 1 · 0.6s · hands_complete
  bot             stack     delta  errors  restarts
  shark          21,350   +11,350       0         0
  ...
run  runs/20261002-141530-ab12/   (arena match show 20261002-141530-ab12)
```

Bots can be passed as a `.py` file, a directory containing `bot.py` (plus an
optional `data/`), or a `.zip`. Every match has a seed (random if you don't
pass `--seed`, and recorded); the same seed deals the same cards. Options:
`--hands`, `--blinds 50/100`, `--stack`, `--timeout` (seconds per decision),
`--id`, `--verbose`, `--json`, `--no-store`.
(`python3 sandbox/match.py ...` still works and forwards here.)

Everything goes to `runs/<id>/` as it happens (`tail -f
runs/<id>/events.jsonl` to follow a match). The event types and record
fields are documented in [arena/runs.py](arena/runs.py). To look at a run:

```bash
arena match list
arena match show ID          # results, per-bot decisions/errors/corrections/timing, biggest pots
arena match hand ID 26       # god view of hand 26, with each decision's ctx.log notes
arena match hand ID 26 --at 4 --save my-spot   # the position before action 4, saved as a spot
arena match verify ID        # replay every hand from the log and check the result
```

### Query across matches

Finished runs are indexed into `runs/index.sqlite` automatically the first
time you query (`arena index --rebuild` to redo it). A bot is its id plus
the version hash of its code, so results can be split by version. Every
result line starts with a `MATCH:HAND` reference you can open with
`arena match hand MATCH:HAND`.

```bash
arena stats mathematician                # bb/100 with a 95% CI, VPIP/PFR/3-bet/AF/WTSD/W$SD,
                                         # fold-to-bet by street, by position, errors, timing
arena stats shark --version 1409a7 --vs aggressor --last 5
arena hands --bot shark --lost-more 2000 --showdown      # costliest hands first
arena decisions --bot mathematician --action fold --equity-above 0.6   # folded a winner
arena decisions --bot mybot --street river --facing-bet --log-contains bluff
arena decisions --bot mybot --error            # timeouts, crashes, exceptions
arena sql "SELECT pos, avg(delta_bb) FROM hand_players WHERE bot_id='shark' GROUP BY pos"
arena sql --schema                             # tables, columns and stat definitions
```
```
mathematician · 643 hands in 2 match(es) · version 782e554e28f8 (2 matches)
win rate   -7.8 bb/100  (95% CI -16.3 .. +0.6)   chips -5,050
style      VPIP 24%  PFR 0%  3-bet 0% (of 121)  AF 0.0  WTSD 74%  W$SD 60%
folds to a bet  preflop 73% (n=581)  flop 92% (n=25)  turn 67% (n=12)  river 60% (n=5)
...
note: the confidence interval includes 0; the win rate isn't distinguishable from break-even yet
```

`equity` on a decision is the acting player's share of the pot against the
hole cards actually still in, on the flop, turn and river. It uses cards
the bot couldn't see, which is what makes leak queries like "folded with
70% equity" possible. Indexing computes it exactly; `--no-equity` skips it.

### Probe a bot

Ask a bot what it does in a position without playing a game. Every probe
starts the bot in a fresh process, so it can't affect a match.

```bash
arena probe bots/mybot 6max-tptk-vs-flop-lead -n 20     # a library spot (or spot options)
arena probe --from six1:26 --at 3 --warm -n 5           # a decision from a stored match
arena probe bots/mybot-v2 --from six1:26 --at 3         # ... asked of a different bot
```
```
probe p-20261002-143051-5f78 · bots/aggressor/bot.py · 6max-tptk-vs-flop-lead · 20 sample(s)
s3 · flop · board Kd 7c 2s · AsKh · pot 2,450 · stack 9,100 · facing 600 (20% pot odds)
  call     1     5%
  raise   19    95%   to 3,600 (36.0bb, 1.47x pot) x9, to 4,800 (48.0bb, 1.96x pot) x6, ...
  decide() median 0.0ms max 0.1ms · errors none
details: arena probes p-20261002-143051-5f78 --verbose
```

Answers are shown as the engine would apply them (a too-small raise shows
as the minimum raise). With `--from`, the bot gets exactly the state it was
sent in the match, including the real `match_action_log`, and the output
shows what it did then. `--warm` first replays every earlier state that
seat saw in the match (answers ignored), so a bot that models opponents in
memory knows what it knew at that point. Samples run in one process unless
`--fresh`.

`arena sweep` re-probes a spot while varying things, and marks where the
bot's most common answer flips:

```bash
arena sweep bots/mybot hu-missed-flush-river-bet --vary bet=100..1500:200
arena sweep bots/mybot --players 6 --button 3 --actions "pre: CO r250" --to-act BTN \
  --vary "cards.BTN=TT+,AQs+,AKo,A5s"          # a range: one row per hand class
arena sweep bots/mybot --from six1:26 --at 5 --vary turn=*
```
```
  variant    fold check  call raise  avg raise   n
  bet 500      0%    0%  100%    0%          -   3
  bet 700    100%    0%    0%    0%          -   3  < call -> fold
```

`--vary` takes `bet=` (the last raise in the actions), `cards.SEAT=` (hands
or a range), `flop1=`..`river=` (a board card, `*` for every card) and
`stack.SEAT=`; repeat it to combine. Variants that aren't legal positions
are listed as invalid. Probes and sweeps are stored in `runs/probes/`;
`arena probes` lists them and `arena probes ID --verbose` shows every
answer with its notes.

To run every bot in its own locked-down container instead of a local
process:

```bash
brew install colima docker && colima start   # or Docker Desktop
./sandbox.sh build                           # builds poker-harness-sandbox:latest
./sandbox.sh security-check
arena match run --docker bots/shark/bot.py bots/aggressor/bot.py
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
arena/match.py        async MatchRunner: plays a match through seats, records everything
arena/runs.py         run store (runs/<id>/): writer, reader, event schema
arena/replay.py       rebuild/verify hands from events; any point in a hand -> spot
arena/index.py        SQLite index of all runs (runs/index.sqlite): schema and stat definitions
arena/probe.py        probes and sweeps: targets from spots or match decisions, warm-up, variants
arena/runner/         bot side of protocol v2 (bot_runner.py, stdlib only) and bot packaging
arena/tournament.py   Swiss pairing and standings
spots/                the spot library
sandbox/match.py      upstream-compatible wrapper around arena/match.py (used by demo.py)
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
