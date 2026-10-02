"""MatchRunner, the run store, replay, and `arena match` commands."""
import asyncio
import json

import pytest

from poker_harness.cli.main import main
from poker_harness.match import MatchConfig, MatchRunner, bot_ids_for_paths, make_bot_seats
from poker_harness.replay import hand_to_spot, replay_hand, verify_run
from poker_harness.runs import Run, RunWriter
from poker_harness.seats import CallbackSeat, ScriptedSeat


def policy(kind):
    """Deterministic policies, so matches are reproducible from the seed."""
    def caller(state):
        return "x" if state["can_check"] else "c"

    def raiser(state):
        legal = state["legal_actions"]
        if legal["can_raise"] and state["street"] in ("preflop", "flop"):
            return {"action": "raise", "amount": legal["min_raise_to"]}
        return caller(state)

    def folder(state):
        return "x" if state["can_check"] else "f"

    def junk(state):
        return {"action": "raise", "amount": 1} if state["hand_num"] % 2 else {"action": "lol"}

    return {"caller": caller, "raiser": raiser, "folder": folder, "junk": junk}[kind]


def seats(*kinds):
    return {f"{k}{i}": CallbackSeat(f"{k}{i}", policy(k)) for i, k in enumerate(kinds)}


def play(match_id, kinds, root, **cfg):
    writer = RunWriter(match_id, root)
    runner = MatchRunner(match_id, seats(*kinds), MatchConfig(**cfg), writer=writer)
    return asyncio.run(runner.run()), Run.open(match_id, root)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "runs"


# ---------------------------------------------------------------------------
# Runner and store
# ---------------------------------------------------------------------------

def test_run_writes_meta_events_and_decisions(root):
    result, run = play("m1", ["caller", "raiser", "folder"], root, n_hands=20, seed=3)
    assert result["n_hands"] == 20 and result["end_reason"] == "hands_complete"
    assert sum(result["final_stacks"].values()) == 30_000
    assert run.meta["status"] == "complete" and run.meta["result"]["seed"] == 3
    assert [s["bot_id"] for s in run.meta["seats"]] == ["caller0", "raiser1", "folder2"]

    events = list(run.events())
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert events[0]["type"] == "match_start" and events[-1]["type"] == "match_end"
    types = {e["type"] for e in events}
    assert {"hand_start", "blind", "street_start", "action", "hand_end"} <= types

    hs = next(e for e in events if e["type"] == "hand_start")
    assert set(hs["hole_cards"]) == {"0", "1", "2"} and len(hs["board_plan"]) == 5

    # every action links to exactly one decision record
    actions = [e for e in events if e["type"] == "action"]
    decisions = {d["decision_id"]: d for b in run.bot_ids() for d in run.decisions(b)}
    assert sorted(decisions) == sorted(e["decision_id"] for e in actions)
    d = decisions[actions[0]["decision_id"]]
    assert d["bot_id"] == actions[0]["bot_id"] and "match_action_log" not in d["state"]
    assert d["applied"]["action"] == actions[0]["action"]


def test_corrected_and_fallback_decisions_are_recorded(root):
    result, run = play("m2", ["junk", "caller"], root, n_hands=6, seed=1)
    ds = list(run.decisions("junk0"))
    assert ds and all(d["corrected"] for d in ds)
    assert {d["applied"]["action"] for d in ds} <= {"fold", "raise"}


def test_bust_ends_match_and_busted_seat_sits_out(root):
    result, run = play("m3", ["raiser", "caller", "caller"], root, n_hands=60, seed=2,
                       stacks={"caller1": 300})
    assert result["final_stacks"]["caller1"] == 0 and result["n_hands"] == 60
    hand_starts = [e for e in run.events() if e["type"] == "hand_start"]
    busted = [h for h in hand_starts if h["stacks"][1] == 0]
    assert busted and all("1" not in h["hole_cards"] for h in busted)
    assert all(e["seat"] != 1 for e in run.events()
               if e["type"] == "action" and e["hand_num"] >= busted[0]["hand_num"])


def test_same_seed_same_events(root):
    def strip(run):
        return [{k: v for k, v in e.items() if k not in ("ts", "match_id", "hand_id", "elapsed_ms",
                                                          "startup_ms", "load_ms")}
                for e in run.events() if e["type"] not in ("match_start", "match_end")]
    _, a = play("a", ["caller", "raiser", "folder", "raiser"], root, n_hands=50, seed=9)
    _, b = play("b", ["caller", "raiser", "folder", "raiser"], root, n_hands=50, seed=9)
    _, c = play("c", ["caller", "raiser", "folder", "raiser"], root, n_hands=50, seed=10)
    assert strip(a) == strip(b)
    assert strip(a) != strip(c)


def test_seed_is_always_chosen(root):
    result, run = play("m4", ["caller", "folder"], root, n_hands=2)
    assert isinstance(result["seed"], int) and run.meta["config"]["seed"] == result["seed"]


def test_run_id_must_be_new(root):
    RunWriter("dup", root).close()
    with pytest.raises(FileExistsError):
        RunWriter("dup", root)
    with pytest.raises(ValueError):
        RunWriter("../escape", root)


def test_live_on_event_sees_every_event(root):
    seen = []
    runner = MatchRunner("live", seats("caller", "raiser"), MatchConfig(n_hands=5, seed=1),
                         writer=RunWriter("live", root), on_event=seen.append)
    asyncio.run(runner.run())
    assert [e["seq"] for e in seen] == [e["seq"] for e in Run.open("live", root).events()]


def test_seat_exception_marks_run_as_error(root):
    bad = {"a": ScriptedSeat("a", [], strict=True), "b": CallbackSeat("b", policy("caller"))}
    runner = MatchRunner("err", bad, MatchConfig(n_hands=5, seed=1), writer=RunWriter("err", root))
    with pytest.raises(RuntimeError):
        asyncio.run(runner.run())
    run = Run.open("err", root)
    assert run.meta["status"] == "error"
    end = list(run.events())[-1]
    assert end["type"] == "match_end" and end["end_reason"] == "error"


def test_real_bot_processes_write_stderr_logs(root, tmp_path):
    bot = tmp_path / "printer.py"
    bot.write_text("def decide(s, ctx):\n    print('thinking out loud')\n"
                   "    ctx.log('note', x=1)\n    return {'action': 'check' if s['can_check'] else 'call'}\n")
    paths = {"printer": str(bot), "shark": "bots/shark/bot.py"}
    runner = MatchRunner("procs", make_bot_seats(paths), MatchConfig(n_hands=10, seed=4),
                         writer=RunWriter("procs", root))
    asyncio.run(runner.run())
    run = Run.open("procs", root)
    assert "thinking out loud" in (run.dir / "bots" / "printer" / "stderr.log").read_text()
    assert next(run.decisions("printer"))["logs"] == [{"msg": "note", "data": {"x": 1}}]
    assert {e["type"] for e in run.events()} >= {"bot_ready", "bot_closed"}
    assert run.meta["seats"][0]["version"]


def test_free_form_bot_ids_get_safe_dirs(root):
    odd = {"The Shark": CallbackSeat("x", policy("caller")),
           "The_Shark": CallbackSeat("y", policy("raiser")),
           "../up": CallbackSeat("z", policy("folder"))}
    runner = MatchRunner("names", odd, MatchConfig(n_hands=5, seed=1), writer=RunWriter("names", root))
    asyncio.run(runner.run())
    run = Run.open("names", root)
    dirs = [s["dir"] for s in run.meta["seats"]]
    assert dirs == ["The_Shark", "The_Shark-2", "up"]
    assert all((run.dir / "bots" / d).parent == run.dir / "bots" for d in dirs)
    assert next(run.decisions("The Shark"))["bot_id"] == "The Shark"


def test_bot_ids_for_paths():
    assert bot_ids_for_paths(["bots/shark/bot.py", "bots/shark/bot.py", "x/my bot.py", "b.zip"]) == \
        ["shark", "shark_2", "my_bot", "b"]


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def test_every_hand_replays_exactly(root):
    _, run = play("r1", ["raiser", "caller", "raiser", "caller", "folder", "raiser"],
                  root, n_hands=150, seed=5)
    assert verify_run(run) == {}


def test_verify_catches_a_tampered_log(root):
    _, run = play("r2", ["raiser", "caller"], root, n_hands=10, seed=5)
    path = run.dir / "events.jsonl"
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        ev = json.loads(line)
        if ev["type"] == "hand_start":
            ev["board_plan"] = list(reversed(ev["board_plan"]))   # change the runout
            lines[i] = json.dumps(ev)
            break
    path.write_text("\n".join(lines) + "\n")
    bad = verify_run(Run.open("r2", root))
    assert bad and 0 in bad


def test_hand_to_spot_stops_mid_hand(root):
    _, run = play("r3", ["raiser", "caller", "raiser"], root, n_hands=5, seed=8)
    events = run.hand_events(0)
    actions = [e for e in events if e["type"] == "action"]
    spot = hand_to_spot(events, upto=2)
    eng, state = spot.to_engine()
    replayed, rstate = replay_hand(events, upto=2)
    assert state["seat_to_act"] == rstate["seat_to_act"] == actions[2]["seat"]
    assert state["your_cards"] == rstate["your_cards"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARENA_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("ARENA_SPOTS", str(tmp_path / "spots"))

    def run(*argv):
        code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err
    return run


def test_cli_match_flow(cli, tmp_path):
    code, out, err = cli("match", "run", "bots/shark/bot.py", "bots/template/bot.py",
                         "--hands", "40", "--seed", "3", "--id", "c1")
    assert code == 0 and "match c1 · 40 hands · seed 3" in out
    code, out, _ = cli("match", "list")
    assert out.startswith("c1")
    code, out, _ = cli("match", "show", "c1")
    assert "decisions" in out and "biggest pots" in out
    code, out, _ = cli("match", "hand", "c1", "0", "--no-equity")
    assert "match c1 hand 0" in out and "decisions:" in out
    code, out, _ = cli("match", "hand", "c1", "0", "--at", "0", "--save", "from-c1")
    assert code == 0 and (tmp_path / "spots" / "from-c1.yaml").exists()
    code, out, _ = cli("spot", "from-c1", "--no-equity")
    assert code == 0 and "to act" in out
    code, out, _ = cli("match", "verify", "c1")
    assert code == 0 and "all match" in out
    code, out, err = cli("match", "hand", "c1", "999")
    assert code == 2 and "no hand 999" in err
    code, out, err = cli("match", "run", "bots/shark/bot.py", "bots/template/bot.py", "--id", "c1")
    assert code == 2 and "already exists" in err


def test_cli_match_json(cli):
    code, out, _ = cli("match", "run", "bots/shark/bot.py", "bots/shark/bot.py",
                       "--hands", "5", "--seed", "1", "--no-store", "--json")
    data = json.loads(out)
    assert data["bot_ids"] == ["shark", "shark_2"] and data["run_dir"] is None
