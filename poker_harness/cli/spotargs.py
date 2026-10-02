"""Spot options shared by commands that take a spot (spot, hand, probe, sweep)."""

from poker_harness.cli.store import find_spot
from poker_harness.spot import Spot, SpotError

SPOT_HELP = """\
spot notation:
  --cards   "s0=AsKh s4=QdQc" or "BTN=AsKh BB=QdQc"   (others random)
  --board   "Kd7c2s|9h|"  flop|turn|river; ?? or missing = random (quote it)
  --stacks  "10000" | "10000,5000,0" (0 = busted) | "10000 s3=2500"
  --blinds  "50/100"
  --actions "pre: BTN r250, BB r900, BTN c; flop: BB x"
            f fold, x check, c call, rN raise to N, a all-in. Naming a
            seat that isn't next makes the seats in between check or fold.
  --to-act  seat expected to act after the actions (checked)
  positions: BTN SB BB UTG UTG+1 UTG+2 LJ HJ CO (or s0..s8)
"""


def add_spot_options(p):
    g = p.add_argument_group("spot options (override REF's fields)")
    g.add_argument("--players", type=int)
    g.add_argument("--button", help="button seat number")
    g.add_argument("--stacks")
    g.add_argument("--blinds")
    g.add_argument("--cards")
    g.add_argument("--board")
    g.add_argument("--actions")
    g.add_argument("--to-act", dest="to_act")
    g.add_argument("--seed", type=int)
    g.add_argument("--name")
    g.add_argument("--desc", dest="description")
    g.add_argument("--tags", help="comma-separated tags, e.g. river,bluff-catch")
    g.add_argument("--expect", help="expected answer as YAML, e.g. \"{not: [fold]}\" (see arena test -h)")


def spot_from_args(args) -> Spot:
    base = Spot.load(find_spot(args.ref)).to_dict() if getattr(args, "ref", None) else {}
    for key in ("players", "button", "stacks", "blinds", "cards", "board",
                "actions", "to_act", "seed", "name", "description", "tags"):
        val = getattr(args, key, None)
        if val is not None:
            base[key] = val
    if getattr(args, "expect", None):
        import yaml
        try:
            base["expect"] = yaml.safe_load(args.expect)
        except yaml.YAMLError as e:
            raise SpotError(f"--expect isn't valid YAML: {e}") from None
    if args.players is not None and args.stacks is None and "stacks" in base \
            and "," in str(base["stacks"]):
        raise SpotError("--players conflicts with the spot's per-seat stacks; pass --stacks too")
    return Spot.from_dict(base)
