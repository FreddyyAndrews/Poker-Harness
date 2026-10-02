"""Expectations (arena/expect.py) and `arena test`."""
import pytest

from arena import expect as ex
from arena.spot import Spot, SpotError


def sample(kind, to=None, all_in=False, error=None):
    return {"kind": kind, "to": to, "all_in": all_in, "error": error}


STATE = {"pot": 1000, "big_blind": 100}


# ---------------------------------------------------------------------------
# validate / parse
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, x, ok", [
    ("<0.2", 0.1, True), ("<0.2", 0.2, False), (">=2.5", 2.5, True), ("==1", 1, True),
    ("2.5..4", 4, True), ("2.5..4", 4.1, False), (3, 3, True), (3, 3.5, False),
])
def test_parse_cmp(text, x, ok):
    fn, _ = ex.parse_cmp(text, "k")
    assert fn(x) is ok


@pytest.mark.parametrize("expect, msg", [
    ({}, "non-empty"), ({"actoin": ["call"]}, "unknown expect keys"),
    ({"action": ["shove"]}, "actions must be"), ({"not": []}, "actions must be"),
    ({"raise_to_bb": "big"}, "can't parse"), ({"freq": {"bluff": "<0.1"}}, "actions must be"),
    ({"freq": "<0.1"}, "must map"), ({"n": 0}, "positive"), ({"errors_ok": "yes"}, "true or false"),
])
def test_validate_errors(expect, msg):
    with pytest.raises(ex.ExpectError, match=msg):
        ex.validate(expect)


def test_validate_normalises_single_actions():
    assert ex.validate({"not": "fold"}) == {"not": ["fold"]}


def test_spot_rejects_bad_expect_and_round_trips_good_one(tmp_path):
    with pytest.raises(SpotError, match="unknown expect"):
        Spot.from_dict({"players": 2, "expect": {"nah": 1}})
    s = Spot.from_dict({"players": 2, "tags": "a, b", "expect": {"freq": {"call": ">=0.5"}, "n": 9}})
    s.save(tmp_path / "x.yaml")
    back = Spot.load(tmp_path / "x.yaml")
    assert back.tags == ["a", "b"] and back.expect == {"freq": {"call": ">=0.5"}, "n": 9}


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------

def test_action_and_not():
    assert ex.evaluate({"action": ["call", "raise"]}, [sample("call"), sample("bet", 500)], STATE)["passed"]
    r = ex.evaluate({"action": ["call"]}, [sample("call"), sample("fold"), sample("fold")], STATE)
    assert not r["passed"] and r["failures"] == ["fold in 2/3; expected call"]
    r = ex.evaluate({"not": ["fold"]}, [sample("check")], STATE)
    assert r["passed"] and r["freq"] == {"check": 1.0}


def test_all_in_counts_as_its_own_action():
    s = [sample("raise", 9000, all_in=True)]
    assert ex.evaluate({"action": ["all_in"]}, s, STATE)["passed"]
    assert ex.evaluate({"action": ["raise"]}, s, STATE)["passed"]
    assert not ex.evaluate({"not": ["all_in"]}, s, STATE)["passed"]
    assert ex.evaluate({"action": ["call"]}, [sample("call", all_in=True)], STATE)["passed"]


def test_raise_sizes_only_check_raises():
    samples = [sample("raise", 300), sample("raise", 600), sample("fold")]
    assert ex.evaluate({"raise_to_bb": "2.5..6"}, samples, STATE)["passed"]
    r = ex.evaluate({"raise_to_bb": ">=4"}, samples, STATE)
    assert r["failures"] == ["raise to 300 (3bb) in 1/2 raises; expected raise_to_bb >=4"]
    assert not ex.evaluate({"raise_to_pot": "<=0.5"}, samples, STATE)["passed"]
    assert ex.evaluate({"raise_to": "300..600"}, samples, STATE)["passed"]
    assert ex.evaluate({"raise_to_bb": ">=100"}, [sample("fold")], STATE)["passed"]


def test_freq():
    samples = [sample("fold")] + [sample("call")] * 4
    assert ex.evaluate({"freq": {"fold": "<0.3", "call": ">=0.8"}}, samples, STATE)["passed"]
    r = ex.evaluate({"freq": {"fold": "==0"}}, samples, STATE)
    assert r["failures"] == ["fold 20% of samples; expected ==0"]


def test_errors_fail_unless_allowed():
    samples = [sample("fold", error="timeout"), sample("call")]
    r = ex.evaluate({"not": ["raise"]}, samples, STATE)
    assert not r["passed"] and r["failures"] == ["errors: timeout x1"]
    assert ex.evaluate({"not": ["raise"], "errors_ok": True}, samples, STATE)["passed"]


def test_no_samples_never_passes():
    assert not ex.evaluate({"not": ["fold"]}, [], STATE)["passed"]
