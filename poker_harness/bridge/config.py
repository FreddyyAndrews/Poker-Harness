"""Bridge configuration (config.yml), modelled on lichess-bot's."""

import os
import re
from pathlib import Path
from typing import Optional

import yaml
from pydantic import Field, ValidationError, field_validator

from poker_harness.protocol.models import Format, Model

DEFAULT_CONFIG = """\
# arena connect: bridge between the poker arena and your bot.
# Copy to config.yml (never commit it: it holds your token).

url: "https://arena.example.com"   # the arena's address
token: "${ARENA_TOKEN}"            # bot token; ${VAR} reads an environment variable

bot:
  path: "bot/bot.py"               # bot.py, a directory with bot.py, or a .zip
  docker: false                    # run the bot in the sandbox container instead of a process
  image: "poker-harness-sandbox:latest"
  time_margin_s: 2                 # answer this much before the arena's deadline (network latency)

challenge:                         # incoming challenges
  accept: true                     # accept challenges at all
  concurrency: 1                   # matches at once (incoming + outgoing + seeks)
  modes: [casual, rated]
  seats: [2, 3, 4, 5, 6, 7, 8, 9]  # table sizes to accept
  min_hands: 1
  max_hands: 10000
  min_decision_s: 5                # decline clocks faster than this
  allow_list: []                   # if not empty, only accept these bots
  block_list: []                   # never accept these bots

matchmaking:                       # challenge other online bots when idle
  enabled: false
  interval_s: 120                  # how often to try
  challenge_timeout_s: 60          # cancel a challenge nobody answers
  decline_backoff_s: 1800          # leave a bot alone this long after it declines
  rated: false
  opponents: []                    # if not empty, only challenge these bots
  block_list: []
  formats:                         # one is picked at random per challenge
    - {seats: 2, hands: 200, small_blind: 50, big_blind: 100, stack: 10000,
       reset_stacks: true, clock: {decision_s: 30, bank_s: 300}}

seek:                              # queue for tables (needed for 3+ seats)
  enabled: false
  rated: false
  formats:
    - {seats: 6, hands: 300, small_blind: 50, big_blind: 100, stack: 10000,
       clock: {decision_s: 30, bank_s: 600}}

records:
  dir: "runs/arena"                # own-perspective record of every arena match

max_matches: 0                     # stop after this many matches (0 = run forever)
log_level: "info"                  # debug | info | warning
"""


class ConfigError(ValueError):
    pass


class BotConfig(Model):
    path: str = "bot/bot.py"
    docker: bool = False
    image: str = "poker-harness-sandbox:latest"
    time_margin_s: float = Field(2.0, ge=0)


class ChallengeConfig(Model):
    accept: bool = True
    concurrency: int = Field(1, ge=1)
    modes: list[str] = Field(default_factory=lambda: ["casual", "rated"])
    seats: list[int] = Field(default_factory=lambda: list(range(2, 10)))
    min_hands: int = 1
    max_hands: int = 10_000
    min_decision_s: float = 5
    allow_list: list[str] = Field(default_factory=list)
    block_list: list[str] = Field(default_factory=list)

    @field_validator("modes")
    @classmethod
    def _modes(cls, v):
        bad = set(v) - {"casual", "rated"}
        if bad:
            raise ValueError(f"modes must be casual and/or rated, not {sorted(bad)}")
        return v


class MatchmakingConfig(Model):
    enabled: bool = False
    interval_s: float = Field(120, gt=0)
    challenge_timeout_s: float = Field(60, gt=0)
    decline_backoff_s: float = Field(1800, ge=0)
    rated: bool = False
    opponents: list[str] = Field(default_factory=list)
    block_list: list[str] = Field(default_factory=list)
    formats: list[Format] = Field(default_factory=lambda: [Format()])


class SeekConfig(Model):
    enabled: bool = False
    rated: bool = False
    formats: list[Format] = Field(default_factory=lambda: [Format(seats=6)])


class RecordsConfig(Model):
    dir: str = "runs/arena"


class BridgeConfig(Model):
    url: str
    token: str
    bot: BotConfig = Field(default_factory=BotConfig)
    challenge: ChallengeConfig = Field(default_factory=ChallengeConfig)
    matchmaking: MatchmakingConfig = Field(default_factory=MatchmakingConfig)
    seek: SeekConfig = Field(default_factory=SeekConfig)
    records: RecordsConfig = Field(default_factory=RecordsConfig)
    max_matches: int = Field(0, ge=0)
    log_level: str = "info"
    # retry timing (tests shrink these)
    backoff_base_s: float = Field(1.0, gt=0)
    backoff_max_s: float = Field(60.0, gt=0)


_ENV = re.compile(r"\$\{(\w+)\}")


def _expand(value):
    if isinstance(value, str):
        def sub(m):
            if m.group(1) not in os.environ:
                raise ConfigError(f"environment variable {m.group(1)} is not set")
            return os.environ[m.group(1)]
        return _ENV.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_config(path: Optional[str] = None, overrides: Optional[dict] = None) -> BridgeConfig:
    """Read config.yml (if given), expand ${VARS}, apply overrides (e.g. from
    the command line), and validate. ARENA_TOKEN fills a missing token."""
    data = {}
    if path:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"no config file at {p} (create one with: arena connect --init)")
        data = yaml.safe_load(p.read_text()) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{p}: expected a mapping of settings")
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        node = data
        *parents, last = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = value
    if not data.get("token") and os.environ.get("ARENA_TOKEN"):
        data["token"] = os.environ["ARENA_TOKEN"]
    data = _expand(data)
    try:
        return BridgeConfig.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                             for err in e.errors())
        raise ConfigError(f"invalid config: {problems}") from None
