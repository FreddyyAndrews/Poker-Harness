"""The arena CLI, run in-process against temporary spot/hand directories."""
import json

import pytest

from arena.cli.main import main

SPOT = ["--players", "6", "--button", "3", "--cards", "BTN=AsKh BB=QdQc",
        "--board", "Kd7c2s|9h|", "--actions", "pre: BTN r250, BB r900, BTN c; flop: BB r600",
        "--to-act", "BTN", "--seed", "1"]


@pytest.fixture(autouse=True)
def dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ARENA_SPOTS", str(tmp_path / "spots"))
    return tmp_path


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def run_json(capsys, *argv):
    code, out, err = run(capsys, *argv, "--json")
    return code, json.loads(out)


def test_spot_text_and_json(capsys):
    code, out, _ = run(capsys, "spot", *SPOT)
    assert code == 0
    assert "s3 (BTN) to act" in out
    assert "line   pre: s3 r250, s5 r900, s3 c; flop: s5 r600" in out
    assert "legal  fold | call 600 | raise 1,200..9,100" in out
    code, view = run_json(capsys, "spot", *SPOT)
    assert view["to_act"] == 3 and view["runout"][0] == "9h"
    assert view["seats"][5]["cards"] == "QdQc"            # god view
    assert abs(sum(s.get("equity", 0) for s in view["seats"]) - 1) < 1e-9


def test_as_bot_is_exactly_what_the_bot_sees(capsys):
    code, out, _ = run(capsys, "spot", *SPOT, "--as-bot")
    state = json.loads(out)
    assert state["type"] == "action_request" and state["your_cards"] == ["As", "Kh"]
    assert "QdQc" not in out and "9h" not in out           # no hidden info


def test_spot_errors_exit_2(capsys):
    code, out, err = run(capsys, "spot", "--actions", "pre: x")
    assert code == 2 and "can't check" in err
    code, data = run_json(capsys, "spot", "--actions", "pre: x")
    assert code == 2 and "can't check" in data["error"]


def test_save_list_and_load_spot(capsys, dirs):
    code, out, _ = run(capsys, "spot", *SPOT, "--save", "tptk", "--desc", "flop lead")
    assert code == 0 and (dirs / "spots" / "tptk.yaml").exists()
    code, out, err = run(capsys, "spot", *SPOT, "--save", "tptk")
    assert code == 2 and "already exists" in err
    code, out, _ = run(capsys, "spots")
    assert "tptk" in out and "flop, s3 to act" in out and "flop lead" in out
    code, view = run_json(capsys, "spot", "tptk")
    assert view["to_act"] == 3
    # REF + overrides
    code, view = run_json(capsys, "spot", "tptk", "--cards", "BTN=7h7d BB=QdQc")
    assert view["seats"][3]["cards"] == "7h7d"


def test_hand_session(capsys):
    code, view = run_json(capsys, "hand", "new", *SPOT)
    assert code == 0 and view["to_act"] == 3
    code, view = run_json(capsys, "hand", "act", "h1", "c", "BB r2000")
    assert view["street"] == "turn" and view["to_act"] == 3
    # strict: illegal action is rejected and nothing is saved
    code, out, err = run(capsys, "hand", "act", "h1", "x")
    assert code == 2 and "owes 2000" in err and "legal: fold | call 2,000" in err
    code, view = run_json(capsys, "hand", "state", "h1")
    assert view["line"].endswith("turn: s5 r2000")
    # lenient corrects and says so
    code, out, err = run(capsys, "hand", "act", "h1", "r100", "--lenient")
    assert "applied as raise 4000" in err
    code, view = run_json(capsys, "hand", "undo", "h1")
    assert view["line"].endswith("turn: s5 r2000")
    code, view = run_json(capsys, "hand", "act", "h1", "c, x, x")
    assert view["complete"] and view["result"]["winners"][0]["seat"] == 3
    code, out, err = run(capsys, "hand", "act", "h1", "x")
    assert code == 2 and "already over" in err


def test_hand_save_list_rm(capsys, dirs):
    run(capsys, "hand", "new", "--players", "3", "--seed", "9")
    run(capsys, "hand", "act", "h1", "c, c, x")
    code, out, _ = run(capsys, "hand", "save", "h1", "limped-flop")
    assert code == 0
    code, view = run_json(capsys, "spot", "limped-flop")
    assert view["street"] == "flop"
    code, out, _ = run(capsys, "hand", "list")
    assert out.startswith("h1") and "flop" in out
    run(capsys, "hand", "rm", "h1")
    code, out, _ = run(capsys, "hand", "list")
    assert "no hands" in out


def test_hand_new_assigns_seed_so_replays_match(capsys):
    code, v1 = run_json(capsys, "hand", "new")
    code, v2 = run_json(capsys, "hand", "state", "h1")
    assert v1["seed"] is not None
    assert [s["cards"] for s in v1["seats"]] == [s["cards"] for s in v2["seats"]]


def test_equity_command(capsys):
    code, out, _ = run(capsys, "equity", "AsKh", "QdQc", "--board", "Kd7c2s")
    assert code == 0 and "exact (990 boards)" in out and "AsKh" in out
    code, data = run_json(capsys, "equity", "AsAh", "KK", "--iters", "1000")
    assert data["method"] == "monte_carlo" and data["samples"] == 1000
