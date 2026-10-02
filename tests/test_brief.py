"""`arena brief`, checked against bots with deliberate leaks."""
import asyncio
import json
import textwrap

import pytest

from poker_harness.cli.main import main
from poker_harness.match import MatchConfig, MatchRunner, bot_fingerprint, make_bot_seats
from poker_harness.runs import RunWriter

BOTS = {
    # bets the pot whenever it can open the betting, otherwise calls
    "bettor": """
        def decide(s):
            legal = s["legal_actions"]
            if s["can_check"] and legal["can_raise"]:
                return {"action": "raise", "amount": max(legal["min_raise_to"], s["pot"])}
            return {"action": "check" if s["can_check"] else "call"}
    """,
    # calls preflop, then folds to any bet
    "folder": """
        def decide(s):
            if s["can_check"]:
                return {"action": "check"}
            return {"action": "call" if s["street"] == "preflop" else "fold"}
    """,
    # calls everything
    "caller": """
        def decide(s):
            return {"action": "check" if s["can_check"] else "call"}
    """,
    # asks for illegal raises, which the engine corrects
    "junk": """
        def decide(s):
            if s["street"] == "river" and s["can_check"]:
                return {"action": "raise", "amount": 1}
            return {"action": "check" if s["can_check"] else "call"}
    """,
}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("brief")
    paths = {}
    for name, src in BOTS.items():
        d = root / "bots" / name
        d.mkdir(parents=True)
        (d / "bot.py").write_text(textwrap.dedent(src))
        paths[name] = str(d / "bot.py")
    runs = root / "runs"
    cfg = MatchConfig(n_hands=150, seed=11, reset_stacks=True)
    asyncio.run(MatchRunner("bm", make_bot_seats(paths), cfg, writer=RunWriter("bm", runs)).run())
    # a stored test run and comparison mentioning "folder"
    (runs / "probes" / "t-x").mkdir(parents=True)
    (runs / "probes" / "t-x" / "probe.json").write_text(json.dumps({
        "id": "t-x", "kind": "test", "target": "basics",
        "bot": {"path": paths["folder"], "version": bot_fingerprint(paths["folder"])},
        "results": [{"spot": "suites/basics/a", "passed": True},
                    {"spot": "suites/basics/dont-fold-the-nuts", "passed": False}]}))
    (runs / "compares" / "c-x").mkdir(parents=True)
    (runs / "compares" / "c-x" / "compare.json").write_text(json.dumps({
        "id": "c-x", "created": 1, "mode": "head-to-head",
        "a": {"id": "caller"}, "b": {"id": "folder"},
        "stats": {"diff": {"bb_per_100": 12.0, "ci95": [4.0, 20.0]}}}))
    return root


@pytest.fixture
def cli(env, monkeypatch, capsys):
    monkeypatch.setenv("ARENA_RUNS", str(env / "runs"))

    def run(*argv):
        code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err
    return run


def test_folder_brief_flags_its_leaks(cli):
    code, out, _ = cli("brief", "folder")
    assert code == 0
    assert out.startswith("brief: folder (version ")
    assert "folds to 100% of flop bets" in out
    assert "folds were +EV calls against the actual cards" in out
    assert "-> arena decisions --bot folder --action fold" in out
    assert "tests   t-x: 1/2 passed (failed: dont-fold-the-nuts)" in out
    assert "compare c-x: vs caller (head-to-head) -12.0 bb/100 (CI -20.0 .. -4.0): worse" in out
    assert "costliest hands" in out and "bm:" in out


def test_caller_and_junk_briefs(cli):
    _, out, _ = cli("brief", "caller")
    assert "calls were -EV against the actual cards" in out
    _, out, _ = cli("brief", "junk")
    assert "illegal and corrected by the engine" in out


def test_leaks_are_ordered_by_cost(cli):
    _, out, _ = cli("brief", "folder", "--json")
    data = json.loads(out)
    costs = [l["cost_bb"] for l in data["leaks"] if l["cost_bb"] is not None]
    assert costs == sorted(costs, reverse=True)
    assert data["leaks"][0]["cost_bb"] is not None
    assert all(set(l) == {"cost_bb", "text", "command"} for l in data["leaks"])


def test_max_lines(cli):
    _, out, _ = cli("brief", "folder", "--max-lines", "6")
    lines = out.strip().splitlines()
    assert len(lines) == 6 and lines[-1].startswith("(+") and "raise --max-lines" in lines[-1]


def test_match_brief(cli):
    code, out, _ = cli("brief", "bm")
    assert code == 0 and out.startswith("brief: match bm · 150 hands")
    for b in BOTS:
        assert f"  {b} " in out
    assert "notable:" in out and "folder: folds to 100% of flop bets" in out
    assert "biggest pots: bm:" in out
    code, out, _ = cli("brief", "folder", "--match", "bm")
    assert "150 hands in match bm" in out and "--match bm" in out


def test_unknown_target(cli):
    code, _, err = cli("brief", "nobody")
    assert code == 2 and "no hands" in err
