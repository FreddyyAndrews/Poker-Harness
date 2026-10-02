"""`arena guide`: how to use the toolkit, for an agent that only has the
installed package. Keep it short; details belong in each command's -h."""

SECTIONS = {
    "start": """\
WHAT THIS IS
  The arena toolkit: a No-Limit Hold'em engine plus tools to build, test
  and improve poker bots locally with full god view (every player's cards,
  every decision), then play them on the arena with `arena connect`.

  A bot is a bot.py with:
      def decide(state, ctx):          # or decide(state)
          ctx.log("why", equity=0.6)   # notes saved with the decision
          return {"action": "call"}    # fold | check | call | all_in |
                                       # {"action": "raise", "amount": TOTAL}
  `arena guide bot` has the details.

FIRST STEPS
  arena match run bot/bot.py opponents/*.py --hands 400   # play, record everything
  arena brief BOT                                         # result, leaks, worst hands
""",
    "loop": """\
THE IMPROVEMENT LOOP
  1. Measure:     arena brief BOT              start here every session
                  arena stats BOT              win rate with a confidence interval
  2. Find leaks:  arena decisions --bot BOT --action fold --equity-above 0.5
                  arena hands --bot BOT --lost-more 2000
                  arena match hand MATCH:HAND  god view of one hand + the bot's notes
  3. Understand:  arena probe BOT --from MATCH:HAND --at K --warm
                  arena sweep BOT SPOT --vary bet=200..2000:200
  4. Lock it in:  arena match hand MATCH:HAND --at K --save suites/mine/NAME
                  then add `expect:` to that spot file (see `arena test -h`)
  5. Change the bot, then check:
                  arena test BOT --suite basics        exit 1 = a spot failed
                  arena compare NEW OLD [--field ...]  did it actually help?
  6. Play:        arena serve-mock (local) / arena connect (the real arena)

  A single match's chip count is mostly luck. Trust `arena compare`
  (duplicate deals, confidence interval), not one match.
""",
    "commands": """\
WHICH COMMAND
  Situations   spot, spots, hand new|act|state|undo|save, equity
  Matches      match run|list|show|hand|verify
  Queries      stats, hands, decisions, sql, brief
  Questions    probe, sweep, probes
  Evaluation   test, compare, compares
  The arena    serve-mock (local test server), connect (the bridge)
  Run `arena COMMAND -h` for options and examples.
""",
    "conventions": """\
CONVENTIONS
  Output    short by default; --json gives the full structure
  Refs      results start with MATCH:HAND; open one with arena match hand MATCH:HAND
  Queries   stats/hands/decisions/brief leave out arena compare's matches
            (add --with-compares to include them)
  IDs       matches, hands (h1), probes (p-...), sweeps (s-...), tests (t-...),
            comparisons (c-...) are stored; list them and show one by id
  Exit      0 ok · 1 arena test had failures · 2 bad input or error
            (with --json, errors print {"error": ...})
  Prompts   none; every command runs to completion
  Files     runs/                matches (god view) and runs/index.sqlite
            runs/probes/         probes, sweeps, test runs
            runs/compares/       comparisons
            runs/arena/          arena matches from your bot's own view (id arena/<id>)
            spots/               the spot library (spots/suites/NAME/ = test suites)
            .arena/hands/        hands being stepped through
  Env       ARENA_RUNS, ARENA_SPOTS, ARENA_HOME move those; ARENA_TOKEN = arena token
""",
    "bot": """\
THE BOT
  decide(state, ctx) gets one action_request state per decision:
    your_cards, community_cards, street, pot, your_stack, amount_owed,
    can_check, current_bet, min_raise_to, legal_actions {can_fold,
    can_check, call_amount, can_raise, min_raise_to, max_raise_to},
    seat_to_act, dealer_seat, small_blind, big_blind, players (public
    info), action_log (this hand), match_action_log (last 200 actions).
  Return fold, check, call, all_in, or raise with "amount" = the TOTAL
  bet for the street. Illegal replies are corrected (raises snapped up,
  check facing a bet -> call, unknown -> fold).
  ctx.log(msg, **data) records notes; ctx.time_left() = seconds left.
  Optional warmup(ctx) runs once before the first hand (load data here).
  print() is safe (it goes to the bot's stderr log).
  Timeouts and crashes check/fold and restart the bot process.
""",
    "spots": """\
SPOTS (situations set up on purpose)
  --cards   "BTN=AsKh BB=QdQc" or "s0=AsKh"     others are random
  --board   "Kd7c2s|9h|"  flop|turn|river      quote it; ?? = random
  --stacks  "10000" | "10000,5000,0" | "10000 s3=2500"
  --actions "pre: BTN r250, BB r900, BTN c; flop: BB x"
            f x c rN a; naming a later seat folds/checks the ones between
  --to-act  BTN                                checked after replaying
  Positions BTN SB BB UTG UTG+1 UTG+2 LJ HJ CO, or s0..s8.
  Every spot is replayed through the real rules, so impossible lines error.
  Save with --save NAME; add `expect:` for arena test (see `arena test -h`).
""",
    "arena": """\
PLAYING ON THE ARENA
  arena serve-mock                      local arena: bot mybot, token dev-token,
                                        house bots always online
  arena connect --init                  write config.yml (url, token, challenge rules,
                                        matchmaking, seeks)
  arena connect --check                 test the token
  arena connect                         play until Ctrl-C (once: finish; twice: leave)
  Production shows your bot only its own cards and showdowns. The bridge
  records each arena match in runs/arena/<id>/; every tool reads it as
  arena/<id>:
    arena match list | show arena/<id> | hand arena/<id>:N
    arena stats BOT / arena brief BOT    (arena records are indexed too)
    arena probe --from arena/<id>:N --warm
  Unshown cards are ????, hindsight equity is only known where every
  opponent showed down, and only your own decisions can be probed.
  The protocol is docs/bot-api.md in the Poker-Harness repo.
""",
}


def guide_text(topic: str = None) -> str:
    if topic:
        return SECTIONS[topic]
    return "\n".join(SECTIONS.values())


def cmd_guide(args):
    print(guide_text(args.topic), end="")
    return 0


def add_parser(sub):
    p = sub.add_parser("guide", help="how to use this toolkit: start here")
    p.add_argument("topic", nargs="?", choices=list(SECTIONS))
    p.set_defaults(fn=cmd_guide)
