"""`arena connect`: run the bridge between the arena and a bot."""

import asyncio
import logging
import signal
import sys
from pathlib import Path

from poker_harness.bridge import (
    DEFAULT_CONFIG, ArenaClient, ArenaError, Bridge, ConfigError, load_config,
)


class ConnectError(Exception):
    pass


def _config(args):
    path = args.config
    if path is None and Path("config.yml").exists():
        path = "config.yml"
    overrides = {"url": args.url, "token": args.token, "bot.path": args.bot,
                 "max_matches": args.max_matches, "log_level": args.log_level}
    if path is None and not (args.url and (args.token or "ARENA_TOKEN" in __import__("os").environ)):
        raise ConfigError("no config.yml here; create one with `arena connect --init`, "
                          "or pass --url and --token")
    return load_config(path, overrides)


def _client(cfg) -> ArenaClient:
    return ArenaClient(cfg.url, cfg.token, backoff_base_s=cfg.backoff_base_s,
                       backoff_max_s=cfg.backoff_max_s)


def cmd_connect(args):
    if args.init is not None:
        path = Path(args.init)
        if path.exists() and not args.force:
            raise ConnectError(f"{path} already exists (use --force to overwrite)")
        path.write_text(DEFAULT_CONFIG)
        print(f"wrote {path}: set url and token (or ARENA_TOKEN), then run: arena connect")
        return 0

    cfg = _config(args)
    if args.check:
        return asyncio.run(_check(cfg))

    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    if cfg.log_level != "debug":
        logging.getLogger("httpx").setLevel(logging.WARNING)      # one line per request otherwise
    if not Path(cfg.bot.path).exists():
        raise ConnectError(f"bot not found: {cfg.bot.path} (set bot.path in the config or --bot)")
    return asyncio.run(_run(cfg))


async def _check(cfg) -> int:
    client = _client(cfg)
    try:
        acct = await client.account()
        test = await client.token_test()
    except (ArenaError, OSError) as e:
        print(f"error: {cfg.url}: {e}", file=sys.stderr)
        return 2
    finally:
        await client.close()
    ratings = ", ".join(f"{k} {v:.0f}" for k, v in acct.ratings.items()) or "unrated"
    print(f"ok: {cfg.url} · bot {acct.name} (owner {acct.owner}, {ratings}) · "
          f"scopes {', '.join(test.get('scopes', acct.scopes))} · api v{acct.api_version}")
    if not Path(cfg.bot.path).exists():
        print(f"warning: bot not found at {cfg.bot.path}", file=sys.stderr)
    return 0


async def _run(cfg) -> int:
    client = _client(cfg)
    bridge = Bridge(cfg, client)
    loop = asyncio.get_running_loop()
    presses = {"n": 0}

    def on_signal():
        presses["n"] += 1
        if presses["n"] == 1:
            print("\nfinishing current matches (Ctrl-C again to leave them now)", file=sys.stderr)
            asyncio.ensure_future(bridge.stop())
        else:
            asyncio.ensure_future(bridge.stop(now=True))

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal)
        except NotImplementedError:     # Windows
            pass
    try:
        await bridge.run()
    except ArenaError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        await client.close()
    return 0


def add_parser(sub):
    import argparse
    p = sub.add_parser(
        "connect", help="connect a bot to the arena (like lichess-bot)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  arena connect --init              # write a commented config.yml\n"
               "  arena connect --check             # test the token and settings\n"
               "  arena connect                     # play until Ctrl-C\n"
               "  arena connect --max-matches 1     # play one match, then exit\n\n"
               "Ctrl-C once finishes current matches; twice leaves them.\n"
               "Every match is recorded under records.dir (default runs/arena/).\n"
               "The protocol is docs/bot-api.md in Poker-Harness.")
    p.add_argument("--config", help="config file (default: ./config.yml)")
    p.add_argument("--init", nargs="?", const="config.yml", metavar="PATH",
                   help="write a default config file and exit")
    p.add_argument("--force", action="store_true", help="with --init: overwrite")
    p.add_argument("--check", action="store_true", help="check the token and config, then exit")
    p.add_argument("--url", help="override the arena URL")
    p.add_argument("--token", help="override the token (or set ARENA_TOKEN)")
    p.add_argument("--bot", help="override bot.path")
    p.add_argument("--max-matches", type=int, help="stop after this many matches")
    p.add_argument("--log-level", choices=["debug", "info", "warning"])
    p.set_defaults(fn=cmd_connect)
