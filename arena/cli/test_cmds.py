"""`arena test`: run spots with expected answers against a bot."""

import asyncio
import json
import sys
from pathlib import Path

from arena import expect as ex
from arena import probe as pr
from arena.cli.store import find_spot, list_spots, spot_ref, spots_dir
from arena.spot import Spot, SpotError

DEFAULT_IMAGE = "poker-harness-sandbox:latest"


def collect(args) -> tuple:
    """-> ([(ref, spot)], skipped_without_expect)"""
    chosen, skipped = [], 0
    if args.spots:
        for ref in args.spots:
            path = find_spot(ref)
            spot = Spot.load(path)
            if not spot.expect:
                raise SpotError(f"spot {ref!r} has no expect field to test against")
            chosen.append((spot_ref(path), spot))
    else:
        sub = f"suites/{args.suite}" if args.suite else None
        if sub and not (spots_dir() / sub).is_dir():
            raise SpotError(f"no suite {args.suite!r} (expected a folder {spots_dir() / sub}/)")
        for path, spot, err in list_spots(sub):
            if err:
                raise SpotError(f"{spot_ref(path)}: {err}")
            if not spot.expect:
                skipped += 1
                continue
            chosen.append((spot_ref(path), spot))
    if args.tag:
        chosen = [(r, s) for r, s in chosen if args.tag in s.tags]
    if not chosen:
        raise SpotError("no spots with expectations matched (add `expect:` to spot files; "
                        "see arena test -h)")
    return chosen, skipped


async def run_tests(bot, chosen, args, stderr_path) -> list:
    sem = asyncio.Semaphore(args.jobs)

    async def one(i, ref, spot):
        n = args.n or spot.expect.get("n", ex.DEFAULT_N)
        try:
            target = pr.target_from_spot(spot, label=ref)
        except (SpotError, pr.ProbeError, ValueError) as e:
            return {"spot": ref, "passed": False, "failures": [f"invalid spot: {e}"],
                    "n": 0, "samples": []}
        async with sem:
            res = await pr.run_probe(bot, target, n, timeout=args.timeout, stderr_path=stderr_path,
                                     docker_image=args.image if args.docker else None,
                                     seat_id=f"test{i}")
        result = ex.evaluate(spot.expect, res["samples"], target.state)
        return {"spot": ref, **result, "expect": spot.expect, "samples": res["samples"]}

    return await asyncio.gather(*(one(i, r, s) for i, (r, s) in enumerate(chosen)))


def _freq_text(freq: dict) -> str:
    return " ".join(f"{a} {v * 100:.0f}%" for a, v in sorted(freq.items(), key=lambda kv: -kv[1])
                    if a != "all_in") or "-"


def test_text(info: dict) -> str:
    rows = info["results"]
    width = max(len(r["spot"]) for r in rows) + 2
    lines = [f"test {info['id']} · {info['bot']['path']} · {len(rows)} spot(s)"]
    for r in rows:
        mark = "PASS" if r["passed"] else "FAIL"
        detail = _freq_text(r["freq"]) if r["passed"] else "; ".join(r["failures"])
        lines.append(f"  {mark}  {r['spot']:<{width}}{detail}")
    passed = sum(r["passed"] for r in rows)
    lines.append(f"{passed} passed, {len(rows) - passed} failed")
    return "\n".join(lines)


def cmd_test(args):
    chosen, skipped = collect(args)
    d = pr.new_probe_dir("t")
    if not args.json:
        print(f"testing {len(chosen)} spot(s)"
              + (f" ({skipped} without expect skipped)" if skipped else "") + "...", file=sys.stderr)
    rows = asyncio.run(run_tests(args.bot, chosen, args, d / "stderr.log"))
    samples = [{"variant": r["spot"], **s} for r in rows for s in r.pop("samples")]
    info = {"kind": "test", "bot": pr.bot_info(args.bot), "target": args.suite or "library",
            "results": rows, "passed": all(r["passed"] for r in rows)}
    pr.save_probe(d, info, samples)
    info["id"] = d.name
    if args.json:
        print(json.dumps(info, indent=2, default=repr))
    else:
        print(test_text(info))
        print(f"details: arena probes {d.name} --verbose")
    return 0 if info["passed"] else 1


EPILOG = """\
Spots are tested if they have an `expect` field:

  expect:
    action: [call, raise]     every sample must be one of these
    not: [fold]               no sample may be one of these
    raise_to_bb: ">=2.5"      bet/raise size in big blinds (also raise_to, raise_to_pot)
    freq: {fold: "<0.2"}      share of all samples
    n: 20                     samples (default 5; -n overrides)
    errors_ok: false          by default any timeout/crash/exception fails

Actions: fold, check, call, raise (any bet or raise), all_in.
Comparisons: <0.2, >=2.5, ==1, 2.5..4.
Add one from the CLI: arena spot ... --expect "{not: [fold]}" --save suites/NAME/SPOT

Exit code: 0 if everything passes, 1 if anything fails, 2 on usage errors.
"""


def add_parser(sub):
    import argparse
    p = sub.add_parser("test", help="run spots with expected answers against a bot; exit 1 on failure",
                       epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bot", help="bot.py, bot directory or .zip")
    p.add_argument("spots", nargs="*", help="spot files or library names (default: the library)")
    p.add_argument("--suite", help="only spots under spots/suites/NAME/")
    p.add_argument("--tag", help="only spots with this tag")
    p.add_argument("-n", type=int, help="samples per spot (overrides each spot's expect.n)")
    p.add_argument("-j", "--jobs", type=int, default=4, help="bot processes at once")
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--docker", action="store_true")
    p.add_argument("--image", default=DEFAULT_IMAGE)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_test)
