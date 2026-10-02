import pytest

from poker_harness.equity import equity


def eqs(r):
    return [round(p["equity"], 4) for p in r["players"]]


def test_exact_on_flop_and_turn():
    r = equity(["AsKh", "QdQc"], board="Kd7c2s")
    assert r["method"] == "exact" and r["samples"] == 990
    assert abs(sum(eqs(r)) - 1) < 1e-9
    r = equity(["AsKh", "QdQc"], board="Kd7c2sQh")
    assert r["samples"] == 44
    assert eqs(r)[1] > 0.9


def test_river_is_decided_and_ties_split():
    r = equity(["AsAh", "KsKh"], board="2c7d9sJc3h")
    assert eqs(r) == [1.0, 0.0] and r["samples"] == 1
    r = equity(["AsKh", "AdKc"], board="2c7d9sJc3h")
    assert eqs(r) == [0.5, 0.5]
    assert r["players"][0]["tie"] == 1.0


def test_preflop_monte_carlo_is_close_and_seeded():
    r = equity(["AsAh", "KsKh"], iters=20_000, seed=1)
    assert r["method"] == "monte_carlo"
    assert 0.80 < r["players"][0]["equity"] < 0.85   # true value about 0.82
    assert equity(["AsAh", "KsKh"], iters=2_000, seed=7) == equity(["AsAh", "KsKh"], iters=2_000, seed=7)


def test_ranges_and_random_hands():
    r = equity(["AsAh", "KK"], iters=5_000)
    assert 0.78 < r["players"][0]["equity"] < 0.86
    r = equity(["AsKh", "any", "??"], board="Kd7c2s", iters=5_000)
    assert len(r["players"]) == 3 and r["players"][0]["equity"] > 0.6


@pytest.mark.parametrize("hands, kw, msg", [
    (["AsKh"], {}, "2-9"),
    (["AsKh", "AsQd"], {}, "twice"),
    (["AsKh", "QdQc"], {"board": "As7c2s"}, "twice"),
    (["AsKh", "zzz"], {}, "can't parse"),
    (["AsAs", "QdQc"], {}, "can't parse"),
])
def test_errors(hands, kw, msg):
    with pytest.raises(ValueError, match=msg):
        equity(hands, **kw)
