# Poker Harness

> The toolkit behind an LLM poker bot arena: the No-Limit Hold'em rules
> engine, the bot protocol, and a local development arena where coding
> agents build, test and improve poker bots with full god view.

This is a fork of [fullhouse-engine](https://github.com/uzlez/fullhouse-engine),
the engine from the 2026 Fullhouse Hackathon, grown into the shared
library of a three-repo system modelled on lichess and lichess-bot.

**Status:** the local toolkit works today: matches, spots, replay,
cross-match queries, probes, test suites, duplicate comparisons and agent
briefs, all from the `arena` CLI (see
[What works today](#what-works-today)). The bot API is specified in
[docs/bot-api.md](docs/bot-api.md), `arena connect` is the bridge that
plays on it, `arena serve-mock` is a local arena to test against, and the
local tools read the bridge's records of arena matches. That completes
the toolkit's part of milestone 1 (see [Plan](#plan)).

---

## The three repos

| Repo | Role | lichess analogue | Visibility |
|---|---|---|---|
| **Poker-Harness** (this repo) | Toolkit library: rules engine, bot protocol, local dev arena (matches, god view, probes, tests, comparisons, briefs), the bot bridge client and a mock server | python-chess (+ dev tools) | public |
| [poker-bot-template](https://github.com/FreddyyAndrews/poker-bot-template) | What a user forks and drops an agent into: a bot, dummy opponents, spot suites, bridge config, agent instructions | lichess-bot | public |
| [poker-arena](https://github.com/FreddyyAndrews/poker-arena) | Production backend (FastAPI + Postgres) and frontend (React): accounts, tokens, the bot API, matchmaking, ratings, watching and playing | lila | public |

Both other repos install this one, so the rules are identical in local
development and in production.

## How it fits together

```
 Claude cloud session / any machine                    poker-arena (production)
┌──────────────────────────────────────────┐          ┌───────────────────────────────┐
│ poker-bot-template fork                  │          │ accounts, bots, tokens        │
│   bot/bot.py, opponents/, spots/         │          │ bot API (event + match        │
│ Poker-Harness toolkit (this repo)        │  stream  │   streams, decisions)         │
│   local matches with god view, probes,   │ ◄──────► │ deals from secret seeds       │
│   tests, compare, brief                  │  + POST  │ visibility filter per viewer  │
│   arena connect: the bridge that runs    │          │ matchmaking, ratings          │
│   bot.py for production decisions        │          │ website: watch, replay, play  │
└──────────────────────────────────────────┘          └───────────────────────────────┘
```

- **Development is local and fully visible.** An agent plays its bot
  against dummy bots and sees everything: every player's cards, every
  decision, its bot's notes. Nothing there needs protecting.
- **Production is remote and protected.** Bots run wherever their owners
  like and connect with a token, like lichess bots. The arena never runs
  user code; it deals, sends each bot only its own view, enforces time
  controls and records everything. Owners see their own cards plus
  showdowns, never opponents' hidden cards or notes.
- **Bots can use anything:** any model, tool or hardware. The arena
  compares a wide range of implementations rather than controlling them.

## The research question

How should an LLM manage its context to play well over long poker
sessions, and to improve a bot over many development sessions?

- A bot that calls LLMs while deciding has to choose what to remember,
  summarise and forget across hundreds of hands and many matches.
- A coding agent improving a bot has to work from compact evidence. The
  toolkit is built for that: short default output, references to drill
  into, and `arena brief` as the starting summary.

---

## Concepts

| Term | Meaning |
|------|---------|
| **Bot** | A `bot.py` with `decide(state, ctx)`. It only answers when asked for a decision. The same file runs locally and in production. |
| **Seat** | A place at a table, filled by a bot process, a script, a human, or (via the bridge) a remote bot. |
| **Match** | A game between 2-9 seats: hands, blinds, stacks, time per decision. |
| **Spot** | A poker situation set up on purpose: chosen cards, stacks and action so far. Can carry an expected answer for tests. |
| **Probe** | Asking a bot "what would you do here?" without playing a game. |
| **God view** | Seeing every player's hole cards and the whole deck. Local development only. |
| **Bridge** | `arena connect`: connects a bot to the arena, like lichess-bot. |

---

## Plan

The cross-repo milestones are:
1. **Protocol and local loop:** T1-T5 here, and the template connecting to
   the mock server.
2. **Arena alpha:** poker-arena's backend running locally; template bots
   play heads-up challenges.
3. **Public beta:** deployed arena, website, ladder.
4. **Play and polish:** human play, ratings, events, MCP.

Work in this repo:

**T1. Become an installable library** (**done**, v0.2.0)
- The import package is `poker_harness` (the `arena` command stays), so it
  doesn't clash with poker-arena.
- One-command install from git, pinned by tag, for both other repos (see
  [Install](#install)).
- eval7 0.1.11 installs from prebuilt wheels, so no build step and Python
  3.10+ (Linux x86_64 up to 3.15; macOS and Windows up to 3.12). Verified
  with a clean install and the full test suite on macOS (3.10, 3.12) and
  Linux x86_64 (3.12).

**T2. Bot API spec** (**done**, v0.3.0: [docs/bot-api.md](docs/bot-api.md),
`poker_harness/protocol/`, [docs/bot-api.schema.json](docs/bot-api.schema.json))
- The lichess-style API that poker-arena implements and the bridge
  consumes: the bot event stream (challenges, match start/finish), the
  match stream (hand start with your cards only, public actions, `decide`
  requests, hand results with showdowns), decision POSTs, challenges and
  seeks, time controls (per-decision limit plus a time bank), errors and
  versioning.
- The `decide` state is exactly the state bots get locally, so a bot runs
  unchanged in both places.
- Shared Python models for every message, used by the bridge, the mock
  server and poker-arena, plus a JSON Schema for other languages. Tests
  validate every example in the spec against the models and check the
  engine's real decide state matches the documented one field for field.

**T3. Bridge client (`arena connect`)** (**done**, v0.4.0; see
[Connect a bot to the arena](#connect-a-bot-to-the-arena))
- lichess-bot's job for poker: read `config.yml` (server, token, bot
  entry point, concurrency, which challenges to accept, optional
  matchmaking), open the event stream, accept challenges or join seeks,
  play each match by running `bot.py` through the existing bot-process
  seat (isolation, timeouts, restarts), and post decisions with their
  `ctx.log` notes.
- Reconnects with backoff, honours rate limits, and keeps a local record
  of every production match from the bot's own perspective, so the local
  tools (`stats`, `brief`, `hands`) work on production games too.

**T4. Mock server (`arena serve-mock`)** (**done**, v0.5.0; see
[Test against a local arena](#test-against-a-local-arena))
- A small local implementation of the bot API, backed by the engine and
  house bots. It lets the bridge and the template be tested end to end
  without production, and gives poker-arena a reference to check its
  implementation against.

**T5. Own-perspective tooling** (**done**, v0.6.0; see
[Analyse arena matches](#analyse-arena-matches))
- Production records have no hidden cards, so the index, `stats` and
  `brief` must work without them: equity only where cards were shown, and
  no hindsight leaks.
- Turn a production hand into a spot where opponents' unknown cards are
  random.

**T6. Production client commands and MCP**
- Owner queries against the arena API (own matches, hands, decisions,
  stats) through the same commands (`--server` or a profile in config).
- An MCP server exposing the local toolkit and the remote queries to
  agents.

**T7. Integrity helpers for the arena**
- Hand-seed derivation keyed with a secret (HMAC-SHA256) so production
  deals can't be predicted; the engine takes the derived seed.
- A visibility filter that turns a full event log into what a given
  viewer may see (seat owner, spectator, admin), used by poker-arena and
  tested here.

**T8. Housekeeping**
- Reference bots: fix `shark`'s rank-sorting bug (it folds AK and AQ)
  and add a few stronger house bots for the template's opponents.
- Retire upstream leftovers that the new design replaces (`demo.py`,
  `sandbox/match.py` wrapper, `db/schema.sql`, `engine/tournament.py`
  usage).

### Open decisions

- Rating system (decided in poker-arena).

### Dropped from the earlier plan

- A host-side LLM broker: production bots run on their owners' machines
  and call whatever they like. `ctx.log` still records their reasoning.
- Running user bots on the server in Docker: the arena never runs user
  code. Docker mode stays available for local isolation.
- The server and frontend in this repo: they live in poker-arena.

---

## What works today

### Install

Python 3.10 or newer. The hand evaluator, eval7, installs from prebuilt
wheels: Linux x86_64 on Python 3.10-3.15, macOS and Windows on 3.10-3.12.
Other platforms (for example Linux on ARM) aren't supported yet, because
eval7 0.1.11 publishes no source package.

From a clone:

```bash
make install     # creates .venv with the newest python3.1x it finds, installs this package (+ dev, mock)
source .venv/bin/activate
make test        # all tests, including the engine fuzzers
```

As a dependency (how poker-bot-template and poker-arena use it), pinned to
a release:

```bash
pip install "poker-harness @ git+https://github.com/FreddyyAndrews/Poker-Harness@v0.2.0"
# without git:
pip install https://github.com/FreddyyAndrews/Poker-Harness/archive/refs/tags/v0.2.0.tar.gz
```

The Python package is `poker_harness`; the command is `arena`. (The
sandbox Docker image for bots is separate: it still builds eval7 0.1.7
from source on Python 3.10 so it also works on ARM hosts.)

### Using the arena CLI

`make install` puts an `arena` command in `.venv/bin`. Every command runs
without prompts, prints a short summary, and takes `--json` for the full
structure. Errors exit with code 2 (as `{"error": ...}` with `--json`).

The CLI documents itself, so an agent with only the installed package can
learn it: `arena guide` explains the workflow, conventions and file
layout (`arena guide loop` for one section), and every command's `-h`
has a description, examples and help for every option. Tests check all
of that, and that every example in the help actually parses.

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
fields are documented in [poker_harness/runs.py](poker_harness/runs.py). To look at a run:

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

### Test a bot

Spots can carry an expected answer. `arena test` runs a bot against every
spot that has one, prints PASS/FAIL, and exits 1 if anything fails, so it
works as a check after every change to a bot.

```bash
arena test bots/mybot --suite basics        # spots/suites/basics/
arena test bots/mybot --tag river -n 20     # every library spot tagged river
arena test bots/mybot my-spot other-spot    # named spots
```
```
test t-20261002-143603-b5eb · bots/shark/bot.py · 5 spot(s)
  PASS  suites/basics/call-tiny-bet-huge-odds  call 100%
  PASS  suites/basics/check-when-free          check 100%
  FAIL  suites/basics/dont-fold-the-nuts       fold in 5/5; expected not fold
  PASS  suites/basics/fold-trash-to-shove      fold 100%
  PASS  suites/basics/raise-aces-short         raise 100%
4 passed, 1 failed
```

The expectation lives in the spot file:

```yaml
expect:
  action: [call, raise]     # every sample must be one of these
  not: [fold]               # no sample may be one of these
  raise_to_bb: ">=2.5"      # bet/raise size (also raise_to in chips, raise_to_pot)
  freq: {fold: "<0.2"}      # share of all samples
  n: 20                     # samples (default 5)
  errors_ok: false          # by default a timeout/crash/exception fails
```

Actions are `fold`, `check`, `call`, `raise` (any bet or raise) and
`all_in`. Add one from the command line with
`arena spot ... --expect "{not: [fold]}" --tags river --save suites/NAME/SPOT`.
`spots/suites/basics/` is a starter suite of sanity checks (check when
it's free, don't fold the nuts, call a tiny bet getting huge odds, raise
aces when short, fold trash to a shove). Every reference bot fails at
least one of them.

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

### Compare two bots

Is the new version better? A single match can't tell you: over a few
hundred hands, card luck swamps skill. `arena compare` plays duplicate
deals and reports the difference with a 95% confidence interval.

```bash
arena compare bots/mybot-v2 bots/mybot                       # head-to-head
arena compare bots/mybot-v2 bots/mybot --field bots/shark/bot.py --field bots/aggressor/bot.py
```
```
compare c-20261002-144130-dddd · field · template vs mathematician · field shark, ref_bot_2 · duplicate (3 rotations) · 400 deals · seed 5
  template          -17.5 bb/100  (95% CI -20.7 .. -14.2)
  mathematician      -9.9 bb/100  (95% CI -12.2 .. -7.6)
  difference         -7.5 bb/100  (95% CI -10.8 .. -4.3)
  -> mathematician is better (the interval excludes 0)
  duplicate: interval ±3.2 vs ±5.7 for the same hands played plainly (1.78x tighter)
```

How it works:
- Every hand starts from the starting stacks (`MatchConfig.reset_stacks`),
  so hands are independent and a seed deals the same cards to the same
  seat every time.
- **Head-to-head:** each deal is played twice, with A and B swapping seats.
- **Field:** A and B each play the same opponents on the same deals, and
  every player rotates through every seat. The difference is measured deal
  by deal, so the shared card luck cancels.
- When the interval includes 0, the output estimates how many hands would
  show a gap of the measured size.

The matches are stored and indexed like any others (ids `c-...-A-r0`,
...), so `arena stats` and `arena hands` work on them. `arena compares`
lists comparisons; `arena compares ID` shows one again.

### Brief an agent

`arena brief` is the starting point for an agent working on a bot: a short
summary that fits in its context, with every detail one command away.

```bash
arena brief mybot                      # across all its matches (--last N, --version V)
arena brief 20261002-141530-ab12       # a whole match
arena brief mybot --match ID --max-lines 25
```
```
brief: mathematician (version 782e554e28f8) · 643 hands in 2 match(es)
result  -7.8 bb/100 (95% CI -16.3 .. +0.6): not distinguishable from break-even
style   VPIP 24% PFR 0% 3-bet 0% AF 0.0 WTSD 74% W$SD 60%
leaks (most costly first):
  - 18 folds were +EV calls against the actual cards (~70 bb given up, hindsight)  -> arena decisions --bot mathematician --action fold --equity-above 0.4 --sort pot
  - folds to 92% of flop bets (n=25): easy to bluff  -> arena decisions --bot mathematician --street flop --facing-bet
  - never raises preflop (VPIP 24%, PFR 0%)
  - loses most in SB: -34.3 bb/100 over 169 hands  -> arena hands --bot mathematician --pos SB
costliest hands (arena match hand REF):
  five1:22  BB   5dKs  3c6dJc6s9c  -300  showdown
  ...
tests   t-20261002-143604-aa07: 3/5 passed (failed: dont-fold-the-nuts, raise-aces-short)
next: arena stats mathematician · arena hands --bot mathematician --lost-more 1000 · ...
```

Leaks are only flagged with enough data, and the ones with a chip cost
come first. "+EV call" and "-EV call" compare equity against the actual
cards with the pot odds, ignoring later betting, so treat them as
pointers, not verdicts. The brief also lists the bot version's latest
`arena test` runs and the bot's latest comparisons. `--json` gives the same
content as structured data.

### Connect a bot to the arena

`arena connect` is the bridge between the arena and your bot, modelled on
[lichess-bot](https://github.com/lichess-bot-devs/lichess-bot). It speaks
the [bot API](docs/bot-api.md) and runs your `bot.py` locally for every
decision, so the same file you develop with plays in production.

```bash
arena connect --init            # writes a commented config.yml
export ARENA_TOKEN=...          # the bot's token from the arena website
arena connect --check           # checks the token and settings
arena connect                   # plays until Ctrl-C
arena connect --max-matches 1   # plays one match, then exits
```

What it does:
- Keeps the bot's event stream open, reconnecting with backoff.
- Accepts or declines challenges by the `challenge` settings: how many
  matches at once, table sizes, match length, slowest clock, rated or
  casual, allow and block lists.
- Optionally challenges online bots when idle (`matchmaking`: interval,
  formats, opponents; cancels unanswered challenges and leaves a bot alone
  for a while after it declines) and queues for tables (`seek`).
- Plays each match with its own bot process (the same isolation,
  timeouts and restarts as local matches; `bot.docker: true` for the
  sandbox container). It answers each `decide` before the arena's
  deadline minus `bot.time_margin_s`; if the bot fails, it sends the
  fallback (check if free, otherwise fold) instead of letting the clock
  run out. Your `ctx.log` notes go with each decision.
- Reconnects to match streams and never answers a decision twice.
- Ctrl-C once finishes current matches and takes no new ones; twice
  leaves them.

Every match is recorded from your bot's own perspective under
`runs/arena/<match_id>/`: `meta.json`, `stream.jsonl` (every message
received), `decisions.jsonl` (replies, what was applied, errors, notes,
timing) and `stderr.log`. Only what the arena sent your bot is there:
your cards, public actions and showdowns.

### Test against a local arena

`arena serve-mock` runs a local server that implements the whole
[bot API](docs/bot-api.md), backed by the engine, with house bots that
are always online. Use it to try `arena connect` (or your own client)
end to end without production.

```bash
pip install 'poker-harness[mock]'     # FastAPI + uvicorn (included in [dev])
arena serve-mock                      # bot "mybot", token "dev-token", on 127.0.0.1:8765
arena connect --url http://127.0.0.1:8765 --token dev-token --bot bot/bot.py
```

- **House bots:** `house-caller`, `house-tight`, `house-aggro` and
  `house-random` are built in; add any bot file with
  `--house NAME=path/to/bot.py`. They accept every heads-up challenge, and
  fill a waiting seek's table after `--fill-after` seconds, so one bot can
  play 6-max on its own.
- **Your bots:** `--bot NAME=TOKEN` registers a bot (repeatable), so two
  bridges can challenge each other.
- **Same rules as the spec:** challenges, seeks, clocks with a time bank,
  check/fold on timeout, reconnecting with `match_full`, abort when a bot
  never connects (`--connect-timeout`), leaving, and per-seat visibility.
  Not modelled: accounts, ratings, rate limits.
- **Server-side records:** every match is written to `runs/` with the full
  god view, as the real arena does, so `arena match show/hand/verify` and
  `arena brief` work on mock matches. Compare with the bridge's
  own-perspective record in `runs/arena/`.

To have the bridge find a game by itself, enable matchmaking in its
config:

```yaml
matchmaking: {enabled: true, interval_s: 5, opponents: [house-tight],
              formats: [{seats: 2, hands: 100, reset_stacks: true}]}
```

### Analyse arena matches

The bridge records every arena match from your bot's own view in
`runs/arena/<match_id>/`. The local tools read those records under the id
`arena/<match_id>`, the same way as local matches:

```bash
arena match list                              # includes arena/<match_id> records
arena match show arena/mock-170751-2
arena match hand arena/mock-170751-2:12       # opponents' unshown cards appear as ????
arena match verify arena/mock-170751-2        # every hand replays from the record
arena stats mybot                             # arena records are indexed too
arena brief mybot
arena probe --from arena/mock-170751-2:12 --warm   # your bot, in the state it was sent
arena match hand arena/mock-170751-2:12 --at 3 --save from-the-arena
```

A record only holds what the arena sent your bot: your cards, public
actions, the board as dealt, and hands shown at showdown. So:
- Stats that come from actions (win rate, VPIP, PFR, 3-bet, aggression,
  showdown rates, positions) are exactly the same as with god view.
- Hindsight equity is only known for a decision if every opponent still in
  the hand later showed their cards; briefs and equity queries say so.
- Opponents' hidden cards are never shown or saved: a spot made from an
  arena hand keeps your cards and the dealt board, and leaves the rest
  random.
- Only your own bot's decisions can be probed.

When you test against `arena serve-mock` with a shared `runs/`, the mock's
god-view record of a match is used and the bridge's copy is skipped, so
nothing is counted twice.

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

`sandbox/validator.py` (from upstream) enforces the hackathon's rules: no
network, subprocess, threading or pickle imports, no `eval`/`exec`. Those
rules don't apply here, since bots may call LLMs and other tools; the
validator will be retired or reduced to a sanity check (T8).

The protocol between host and bot is newline-delimited JSON; see
[poker_harness/runner/bot_runner.py](poker_harness/runner/bot_runner.py).

### Demo UI (from upstream)

```bash
python3 demo.py   # http://localhost:5001  (DEMO_PORT to change)
```

Six reference bots play single matches or a 3-round Swiss tournament, with a
live log and hand replay. poker-arena's website replaces it (T8).

### Repo layout

```
poker_harness/engine/game.py  NLHE rules for one hand: fixed seats, legal_actions(), strict mode,
                              rigged deals, side pots, events
poker_harness/spot.py         spots: notation parser, YAML/JSON files, replay into the engine
poker_harness/equity.py       showdown equity (exact / Monte Carlo, ranges)
poker_harness/cli/            the `arena` command
poker_harness/seats.py        Seat interface: bot processes/containers, scripted and callback seats
poker_harness/match.py        async MatchRunner: plays a match through seats, records everything
poker_harness/runs.py         run store (runs/<id>/): writer, reader, event schema
poker_harness/replay.py       rebuild/verify hands from events; any point in a hand -> spot
poker_harness/arena_records.py  read the bridge's own-view arena records as runs (arena/<id>)
poker_harness/index.py        SQLite index of all runs (runs/index.sqlite): schema and stat definitions
poker_harness/probe.py        probes and sweeps: targets from spots or match decisions, warm-up, variants
poker_harness/expect.py       expected answers for spots (used by arena test)
poker_harness/compare.py      duplicate comparisons: seat rotations, paired statistics
poker_harness/runner/         bot side of protocol v2 (bot_runner.py, stdlib only) and bot packaging
poker_harness/protocol/       arena bot API message models (spec: docs/bot-api.md) and JSON Schema export
poker_harness/bridge/         arena connect: config, API client, the bridge loop, match records
poker_harness/mock/           arena serve-mock: in-memory arena, remote seats, house bots, FastAPI app
poker_harness/tournament.py   Swiss pairing and standings
spots/                        the spot library; spots/suites/ holds test suites
sandbox/match.py              upstream-compatible wrapper around poker_harness/match.py (used by demo.py)
sandbox/validator.py          checks bot code before accepting it
sandbox/Dockerfile            isolated bot container (no network, read-only, 768 MB, 0.5 CPU)
bots/                         reference bots: template, aggressor, mathematician, shark, ref_bot_2
db/schema.sql                 upstream's hosted-site schema (not used here)
demo.py                       Flask demo UI
```

---

## Credits

Built on [fullhouse-engine](https://github.com/uzlez/fullhouse-engine)
(MIT, © 2026 Fullhouse Hackathon). Hand evaluation by
[eval7](https://github.com/julianandrews/pyeval7).
