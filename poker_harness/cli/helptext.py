"""
Help text for the `arena` command: shared option help, per-command
examples, and finalize(), which applies them to the built parser so every
command and option is documented. tests/test_help.py enforces it.
"""

import argparse

# Options that mean the same thing wherever they appear (by argparse dest).
ARG_HELP = {
    "json": "print the full result as JSON instead of the short summary",
    "limit": "show at most this many rows",
    "image": "sandbox image to use with --docker",
    "docker": "run bots in the sandbox container (needs Docker)",
    "timeout": "seconds a bot gets per decision",
    "seed": "seed for the random cards (same seed, same cards)",
    "blinds": 'small/big blind, e.g. "50/100"',
    "stack": "starting stack in chips",
    "force": "overwrite if it already exists",
    "no_equity": "skip the equity calculation (faster)",
    "hand_cmd": "what to do with hands",
    "match_cmd": "what to do with matches",
    # spot options
    "players": "number of seats, 2-9 (default 6)",
    "stacks": '"10000" (every seat), "10000,5000,0" (per seat; 0 = sits out) or "10000 s3=2500"',
    "cards": 'hole cards to fix: "s0=AsKh s4=QdQc" or by position "BTN=AsKh BB=QdQc" (others random)',
    "board": 'board cards: "Kd7c2s|9h|" (flop|turn|river); missing or ?? = random (quote it)',
    "actions": 'actions so far: "pre: BTN r250, BB c; flop: BB x" '
               "(f fold, x check, c call, rN raise to N, a all-in)",
    "to_act": "seat expected to act after the actions, e.g. BTN or s3 (checked)",
    "name": "name for the spot",
    "description": "a one-line description",
}

# Options whose meaning depends on the command: (command path, dest).
PATH_ARG_HELP = {
    ("hand act", "id"): "hand id (from arena hand list)",
    ("hand act", "actions"): 'actions in order: f x c rN a, optionally after a seat ("BB r900"); '
                             "separate arguments or commas",
    ("hand state", "id"): "hand id (from arena hand list)",
    ("hand undo", "id"): "hand id (from arena hand list)",
    ("hand undo", "n"): "how many actions to take back",
    ("hand save", "id"): "hand id (from arena hand list)",
    ("hand save", "name"): "library name, e.g. river-bluffcatch or suites/mine/river-1",
    ("hand rm", "id"): "hand id (from arena hand list)",
    ("equity", "hands"): "2-9 hands: AsKh, a range like QQ+,AKs, or any for a random hand",
    ("match run", "hands"): "hands to play",
    ("match show", "id"): "match id (from arena match list)",
    ("match hand", "hand"): "hand number (or give MATCH:HAND as the first argument)",
    ("match hand", "at"): "show the position before action K (0 = before anyone acts)",
    ("match verify", "id"): "match id (from arena match list)",
    ("hands", "showdown"): "only hands that reached showdown",
    ("hands", "no_showdown"): "only hands without a showdown",
    ("hands", "lost_more"): "only hands where the bot lost at least CHIPS",
    ("hands", "won_more"): "only hands where the bot won at least CHIPS",
    ("hands", "sort"): "order: loss (biggest losses first), win, pot or time",
    ("decisions", "hand"): "only this hand, as MATCH:HAND",
    ("decisions", "street"): "only this street",
    ("decisions", "action"): "only this kind of action",
    ("decisions", "log_contains"): "only decisions whose ctx.log notes contain TEXT",
    ("decisions", "equity_below"): "only decisions where the bot's equity (hindsight) was below F (0-1)",
    ("decisions", "equity_above"): "only decisions where the bot's equity (hindsight) was above F (0-1)",
    ("decisions", "sort"): "order: time, equity (lowest first) or pot (biggest first)",
    ("sql", "query"): "a SELECT statement over the index",
    ("sql", "schema"): "print the tables, columns and stat definitions",
    ("sweep", "bot"): "bot.py, bot directory or .zip",
    ("sweep", "vary"): "what to vary (repeatable), e.g. bet=200..2000:200 (see below)",
    ("sweep", "max_variants"): "refuse to run more variants than this",
    ("probes", "id"): "probe, sweep or test id (from arena probes); omit to list them",
    ("compare", "id"): "id for the comparison (default: generated)",
    ("compares", "id"): "comparison id (from arena compares); omit to list them",
    ("brief", "max_lines"): "keep the brief within this many lines",
    ("connect", "log_level"): "how much to log (debug also logs every HTTP request)",
    ("serve-mock", "host"): "address to listen on",
    ("serve-mock", "port"): "port to listen on",
    ("serve-mock", "no_records"): "don't record matches",
    ("guide", "topic"): "one section of the guide (default: all of it)",
}

EXAMPLES = {
    "spot": """\
  arena spot --players 6 --button 3 --cards "BTN=AsKh BB=QdQc" --board "Kd7c2s|9h|" \\
    --actions "pre: BTN r250, BB r900, BTN c; flop: BB r600" --to-act BTN --seed 1
  arena spot 6max-tptk-vs-flop-lead --as-bot      # what the bot to act would receive
  arena spot ... --save my-spot                   # add it to the library (spots/)""",
    "spots": """\
  arena spots                                     # every spot, with [expect] and tags""",
    "hand": """\
  arena hand new 6max-tptk-vs-flop-lead           # prints the hand id, e.g. h1
  arena hand act h1 c "BB r2000"
  arena hand undo h1""",
    "hand new": """\
  arena hand new                                  # a fresh 6-max deal
  arena hand new 6max-tptk-vs-flop-lead           # start from a library spot
  arena hand new --players 2 --cards "BTN=AsAh" --id hu1""",
    "hand act": """\
  arena hand act h1 c                             # the seat to act calls
  arena hand act h1 c "BB r2000" x                # several actions in one call
  arena hand act h1 r100 --lenient                # corrected like a live match""",
    "hand state": """\
  arena hand state h1
  arena hand state h1 --as-bot                    # the JSON the seat to act would get""",
    "hand undo": """\
  arena hand undo h1 -n 2""",
    "hand save": """\
  arena hand save h1 river-bluffcatch --desc "faces a river overbet\"""",
    "hand list": """\
  arena hand list""",
    "hand rm": """\
  arena hand rm h1""",
    "equity": """\
  arena equity AsKh QdQc --board Kd7c2s           # exact from the flop on
  arena equity AsAh "QQ+,AKs" any                 # ranges and random hands""",
    "match": """\
  arena match run bots/shark/bot.py bots/aggressor/bot.py --hands 400
  arena match show ID
  arena match hand ID:26""",
    "match run": """\
  arena match run bot/bot.py opponents/*.py --hands 400 --seed 1
  arena match run bot/bot.py bot/bot.py --hands 50 --json   # self-play
  arena match run bot/bot.py opponents/*.py --hands 500 --reset-stacks   # nobody busts""",
    "match list": """\
  arena match list --limit 10""",
    "match show": """\
  arena match show 20261002-141530-ab12""",
    "match hand": """\
  arena match hand ID 26                          # the whole hand, god view
  arena match hand ID:26 --at 4                   # the position before action 4
  arena match hand ID:26 --at 4 --save my-spot    # ... saved as a library spot""",
    "match verify": """\
  arena match verify ID""",
    "index": """\
  arena index                                     # usually automatic
  arena index --rebuild""",
    "stats": """\
  arena stats mybot
  arena stats mybot --last 3 --vs house-tight
  arena stats mybot --pos BTN --json""",
    "hands": """\
  arena hands --bot mybot --lost-more 2000 --showdown   # costliest hands first
  arena hands --bot mybot --pos SB --street river""",
    "decisions": """\
  arena decisions --bot mybot --action fold --equity-above 0.6 --sort pot
  arena decisions --bot mybot --street river --facing-bet --log-contains bluff
  arena decisions --bot mybot --error""",
    "sql": """\
  arena sql --schema
  arena sql "SELECT pos, avg(delta_bb) FROM hand_players WHERE bot_id='mybot' GROUP BY pos\"""",
    "probes": """\
  arena probes                                    # list probes, sweeps and test runs
  arena probes p-20261002-143051-5f78 --verbose   # every answer with its notes""",
    "sweep": """\
  arena sweep bot/bot.py hu-missed-flush-river-bet --vary bet=100..1500:200
  arena sweep bot/bot.py --players 6 --button 3 --actions "pre: CO r250" --to-act BTN \\
    --vary "cards.BTN=TT+,AQs+"
  arena sweep bot/bot.py --from ID:26 --at 5 --vary turn=*""",
    "test": """\
  arena test bot/bot.py --suite basics
  arena test bot/bot.py --tag river -n 20
  arena test bot/bot.py my-spot other-spot""",
    "compares": """\
  arena compares
  arena compares c-20261002-144130-dddd""",
    "guide": """\
  arena guide                                     # the whole guide
  arena guide loop                                # one section""",
}


def _walk(parser, path=()):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {c.dest: c.help for c in action._choices_actions}
            for name, sub in action.choices.items():
                yield path + (name,), sub, helps.get(name)
                yield from _walk(sub, path + (name,))


def commands(parser):
    """[(path string, parser, one-line help)] for every command, nested ones included."""
    return [(" ".join(p), sub, h) for p, sub, h in _walk(parser)]


def finalize(parser) -> None:
    """Fill in descriptions, option help and examples, and use a formatter
    that keeps the layout of descriptions and epilogs."""
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    for path, sub, one_line in commands(parser):
        sub.formatter_class = argparse.RawDescriptionHelpFormatter
        if not sub.description and one_line:
            sub.description = one_line[0].upper() + one_line[1:] + "."
        for action in sub._actions:
            if action.help is None and not isinstance(action, argparse._HelpAction):
                action.help = PATH_ARG_HELP.get((path, action.dest)) or ARG_HELP.get(action.dest)
        if path in EXAMPLES and f"arena {path}" not in (sub.epilog or ""):
            block = "examples:\n" + EXAMPLES[path]
            sub.epilog = block + ("\n\n" + sub.epilog if sub.epilog else "")
