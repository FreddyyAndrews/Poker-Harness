"""`arena serve-mock`: run a local arena that speaks the bot API."""

import asyncio
import logging
import sys


class MockCliError(Exception):
    pass


def _parse_house(specs) -> dict:
    out = {}
    for spec in specs or []:
        name, sep, path = spec.partition("=")
        if not sep or not name or not path:
            raise MockCliError(f"--house takes NAME=PATH (a bot file), got {spec!r}")
        out[name] = path
    return out


def cmd_serve_mock(args):
    try:
        import uvicorn

        from poker_harness.mock.app import create_app
        from poker_harness.mock.arena import MockArena
        from poker_harness.mock.house import POLICIES
    except ImportError as e:
        raise MockCliError(f"the mock arena needs extra packages ({e.name}): "
                           "pip install 'poker-harness[mock]'") from None

    house = {name: None for name in POLICIES} if not args.no_builtin_house else {}
    house.update(_parse_house(args.house))
    arena = MockArena(runs_root=None if args.no_records else __import__("pathlib").Path(args.runs),
                      house=house, connect_timeout_s=args.connect_timeout,
                      house_fill_after_s=args.fill_after)
    tokens = {}
    for spec in args.bot or ["mybot=dev-token"]:
        name, _, token = spec.partition("=")
        tokens[name] = arena.register(name, token or None)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        stream=sys.stderr)
    url = f"http://{args.host}:{args.port}"
    print(f"mock arena on {url}", file=sys.stderr)
    for name, token in tokens.items():
        print(f"  bot {name}: token {token}", file=sys.stderr)
    print(f"  house bots (always online, accept any heads-up challenge): {', '.join(house)}",
          file=sys.stderr)
    if not args.no_records:
        print(f"  matches are recorded with god view in {args.runs}/ "
              "(arena match list / show / hand / verify)", file=sys.stderr)
    print(f"connect a bot: arena connect --url {url} --token <token> --bot path/to/bot.py",
          file=sys.stderr)

    config = uvicorn.Config(create_app(arena), host=args.host, port=args.port, log_level="warning")
    asyncio.run(uvicorn.Server(config).serve())
    return 0


def add_parser(sub):
    import argparse
    p = sub.add_parser(
        "serve-mock", help="run a local arena (the bot API) with house bots, for testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  arena serve-mock                                  # bot mybot, token dev-token\n"
               "  arena serve-mock --bot alice=t1 --bot bob=t2\n"
               "  arena serve-mock --house shark=bots/shark/bot.py\n\n"
               "Built-in house bots: house-caller, house-tight, house-aggro, house-random.\n"
               "House bots accept every heads-up challenge and fill seeks for tables\n"
               "after --fill-after seconds. Needs: pip install 'poker-harness[mock]'.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--bot", action="append", metavar="NAME[=TOKEN]",
                   help="register a bot (repeatable; default mybot=dev-token)")
    p.add_argument("--house", action="append", metavar="NAME=PATH", help="add a house bot from a file")
    p.add_argument("--no-builtin-house", action="store_true", help="no built-in house bots")
    p.add_argument("--fill-after", type=float, default=2.0, metavar="S",
                   help="fill waiting seeks with house bots after this many seconds")
    p.add_argument("--connect-timeout", type=float, default=60.0, metavar="S",
                   help="abort a match if a bot doesn't open its match stream in time")
    p.add_argument("--runs", default="runs", help="where to record matches (god view)")
    p.add_argument("--no-records", action="store_true")
    p.set_defaults(fn=cmd_serve_mock)
