"""The bot API: the spec's examples, the message models and the engine's
decide state must all agree."""
import asyncio
import json
import re
from pathlib import Path

import pytest

from poker_harness.match import MatchConfig, MatchRunner
from poker_harness.protocol import models as m
from poker_harness.protocol.schema import json_schema
from poker_harness.seats import CallbackSeat

ROOT = Path(__file__).parent.parent
SPEC = ROOT / "docs" / "bot-api.md"
EXAMPLE = re.compile(r"```json model=(\w+)\n(.*?)```", re.DOTALL)


def spec_examples():
    return [(name, body) for name, body in EXAMPLE.findall(SPEC.read_text())]


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, body", spec_examples(),
                         ids=[f"{n}-{i}" for i, (n, _) in enumerate(spec_examples())])
def test_every_spec_example_validates(name, body):
    model = getattr(m, name)
    data = json.loads(body)
    obj = model.model_validate(data)
    if "type" in data:
        parser = m.parse_event if data["type"] in m.EVENT_TYPES else m.parse_match_message
        assert type(parser(data)) is model


def test_every_message_type_has_a_spec_example():
    documented = {json.loads(body).get("type") for _, body in spec_examples()}
    missing = (m.EVENT_TYPES | m.MATCH_TYPES) - documented - {"challenge_canceled"}
    assert not missing, f"no example in docs/bot-api.md for: {sorted(missing)}"
    assert "challenge_canceled" in SPEC.read_text()


def test_published_schema_is_current():
    published = json.loads((ROOT / "docs" / "bot-api.schema.json").read_text())
    assert published == json.loads(json.dumps(json_schema(), sort_keys=True)), \
        "regenerate: python -m poker_harness.protocol.schema > docs/bot-api.schema.json"


# ---------------------------------------------------------------------------
# The engine's decide state matches DecideState exactly
# ---------------------------------------------------------------------------

def test_engine_states_match_decide_state():
    sent = []

    def policy(state):
        sent.append(state)
        legal = state["legal_actions"]
        if legal["can_raise"] and state["hand_num"] % 3 == 0:
            return {"action": "raise", "amount": legal["min_raise_to"]}
        return "x" if state["can_check"] else "c"

    seats = {f"b{i}": CallbackSeat(f"b{i}", policy) for i in range(4)}
    asyncio.run(MatchRunner("p", seats, MatchConfig(n_hands=30, seed=2)).run())
    documented = set(m.DecideState.model_fields)
    assert len(sent) > 50
    for state in sent:
        assert set(state) == documented, set(state) ^ documented
        parsed = m.DecideState.model_validate(state)
        assert parsed.model_dump(mode="json")["legal_actions"] == state["legal_actions"]
        assert all(set(p) == set(m.PublicPlayer.model_fields) for p in state["players"])
    assert any(s["match_action_log"] for s in sent)


# ---------------------------------------------------------------------------
# Parsing and compatibility
# ---------------------------------------------------------------------------

def test_unknown_types_and_fields_are_tolerated():
    msg = m.parse_match_message({"type": "chat", "text": "gl hf"})
    assert isinstance(msg, m.UnknownMessage) and msg.type == "chat"
    assert isinstance(m.parse_event({"type": "tournament_start"}), m.UnknownMessage)
    s = m.parse_match_message({"type": "seat_status", "seat": 1, "bot": "x", "connected": True,
                               "new_field": 7})
    assert s.new_field == 7


def test_known_types_with_bad_fields_fail():
    with pytest.raises(m.ValidationError):
        m.parse_match_message({"type": "action", "hand_num": 0, "seat": 0})
    with pytest.raises(m.ValidationError):
        m.DecisionRequest.model_validate({"decision_id": "d1", "action": {"action": "bluff"}})


@pytest.mark.parametrize("fmt", [{"seats": 10}, {"seats": 1}, {"small_blind": 200, "big_blind": 100},
                                 {"hands": 0}, {"clock": {"decision_s": 0}}])
def test_bad_formats(fmt):
    with pytest.raises(m.ValidationError):
        m.Format.model_validate(fmt)


def test_format_defaults_and_round_trip():
    f = m.Format()
    assert (f.seats, f.big_blind, f.clock.decision_s, f.clock.bank_s) == (2, 100, 30, 300)
    req = m.DecisionRequest(decision_id="m:d1", action=m.ActionBody(action="call"),
                            logs=[m.LogEntry(msg="hi", data={"x": 1})])
    assert m.DecisionRequest.model_validate_json(req.model_dump_json()) == req


def test_decision_logs_are_capped():
    with pytest.raises(m.ValidationError):
        m.DecisionRequest(decision_id="d", action={"action": "fold"}, logs=[{}] * 201)
