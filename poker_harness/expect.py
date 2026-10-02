"""
Expected answers for spots, used by `arena test`.

A spot's `expect` field says what a good bot does there. Every key is
optional:

  expect:
    action: [call, raise]     every sample must be one of these
    not: [fold]               no sample may be one of these
    raise_to: "250..400"      chips, for samples that bet or raise
    raise_to_bb: ">=2.5"      ... in big blinds
    raise_to_pot: "<=1.2"     ... as a multiple of the pot before acting
    freq: {fold: "<0.2"}      share of samples, across all n samples
    n: 20                     samples to take (default 5)
    errors_ok: false          by default any error (timeout, crash...) fails

Actions: fold, check, call, raise (any bet or raise) and all_in (any
action that puts the bot all-in). Comparisons: "<0.2", ">=2.5", "==1",
"2.5..4" (inclusive), or a bare number for equality.
"""

import re
from collections import Counter

ACTIONS = ("fold", "check", "call", "raise", "all_in")
KEYS = {"action", "not", "raise_to", "raise_to_bb", "raise_to_pot", "freq", "n", "errors_ok"}
DEFAULT_N = 5


class ExpectError(ValueError):
    pass


def _actions(v, key) -> list:
    items = [v] if isinstance(v, str) else list(v or [])
    bad = [a for a in items if a not in ACTIONS]
    if bad or not items:
        raise ExpectError(f"expect.{key}: actions must be from {ACTIONS}, got {v!r}")
    return items


def parse_cmp(v, key: str):
    """'>=2.5' | '<0.2' | '2.5..4' | 3 -> (predicate, text)"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (lambda x, v=v: x == v), f"=={v}"
    text = str(v).strip()
    m = re.fullmatch(r"(-?[\d.]+)\s*\.\.\s*(-?[\d.]+)", text)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (lambda x: lo <= x <= hi), text
    m = re.fullmatch(r"(<=|>=|==|<|>)\s*(-?[\d.]+)", text)
    if m:
        op, num = m.group(1), float(m.group(2))
        fn = {"<": lambda x: x < num, "<=": lambda x: x <= num, ">": lambda x: x > num,
              ">=": lambda x: x >= num, "==": lambda x: x == num}[op]
        return fn, text
    raise ExpectError(f"expect.{key}: can't parse {v!r}; use <0.2, >=2.5, ==1 or 2.5..4")


def validate(expect) -> dict:
    """Check an expect mapping and return it normalised; raises ExpectError."""
    if expect is None:
        return None
    if not isinstance(expect, dict) or not expect:
        raise ExpectError("expect must be a non-empty mapping")
    unknown = set(expect) - KEYS
    if unknown:
        raise ExpectError(f"unknown expect keys {sorted(unknown)}; allowed: {sorted(KEYS)}")
    out = dict(expect)
    for key in ("action", "not"):
        if key in out:
            out[key] = _actions(out[key], key)
    for key in ("raise_to", "raise_to_bb", "raise_to_pot"):
        if key in out:
            parse_cmp(out[key], key)
    if "freq" in out:
        if not isinstance(out["freq"], dict) or not out["freq"]:
            raise ExpectError("expect.freq must map actions to comparisons, e.g. {fold: '<0.2'}")
        for a, cmp in out["freq"].items():
            _actions(a, "freq")
            parse_cmp(cmp, f"freq.{a}")
    if "n" in out and (not isinstance(out["n"], int) or out["n"] < 1):
        raise ExpectError("expect.n must be a positive integer")
    if "errors_ok" in out and not isinstance(out["errors_ok"], bool):
        raise ExpectError("expect.errors_ok must be true or false")
    return out


def sample_actions(s: dict) -> set:
    """The expectation actions a probe sample counts as."""
    kind = "raise" if s["kind"] in ("bet", "raise") else s["kind"]
    return {kind, "all_in"} if s.get("all_in") else {kind}


def evaluate(expect: dict, samples: list, state: dict) -> dict:
    """{"passed": bool, "failures": [str], "n": int, "freq": {...}}"""
    n = len(samples)
    failures = []
    counts = Counter()
    for s in samples:
        for a in sample_actions(s):
            counts[a] += 1
    freq = {a: counts[a] / n for a in ACTIONS if counts[a]} if n else {}

    def describe(s):
        return s["kind"] + (f" to {s['to']:,}" if s.get("to") else "") + (" (all-in)" if s.get("all_in") else "")

    if "action" in expect:
        bad = [s for s in samples if not (sample_actions(s) & set(expect["action"]))]
        if bad:
            failures.append(f"{describe(bad[0])} in {len(bad)}/{n}; expected {' or '.join(expect['action'])}")
    if "not" in expect:
        bad = [s for s in samples if sample_actions(s) & set(expect["not"])]
        if bad:
            failures.append(f"{describe(bad[0])} in {len(bad)}/{n}; expected not {' or '.join(expect['not'])}")

    raises = [s for s in samples if s.get("to")]
    bb, pot = state.get("big_blind") or 100, state["pot"]
    for key, unit, scale in (("raise_to", "", 1), ("raise_to_bb", "bb", bb), ("raise_to_pot", "x pot", pot)):
        if key in expect and raises:
            fn, text = parse_cmp(expect[key], key)
            bad = [s for s in raises if not fn(s["to"] / scale)]
            if bad:
                got = bad[0]["to"] / scale
                failures.append(f"raise to {bad[0]['to']:,} ({got:g}{unit}) in {len(bad)}/{len(raises)} "
                                f"raises; expected {key} {text}")

    for a, cmp in (expect.get("freq") or {}).items():
        fn, text = parse_cmp(cmp, f"freq.{a}")
        got = freq.get(a, 0.0)
        if not fn(got):
            failures.append(f"{a} {got * 100:.0f}% of samples; expected {text}")

    errors = Counter(s["error"] for s in samples if s.get("error"))
    if errors and not expect.get("errors_ok"):
        failures.append("errors: " + ", ".join(f"{k} x{v}" for k, v in errors.items()))

    return {"passed": not failures and n > 0, "failures": failures, "n": n, "freq": freq,
            "errors": dict(errors)}
