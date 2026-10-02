"""JSON Schema for every bot API message, for clients not written in
Python. `python -m poker_harness.protocol.schema > docs/bot-api.schema.json`
regenerates the published copy (a test checks it's current)."""

import json

from poker_harness.protocol import models as m

MODELS = [
    # event stream
    m.ChallengeEvent, m.ChallengeCanceledEvent, m.ChallengeDeclinedEvent, m.SeekExpiredEvent,
    m.MatchStartEvent, m.MatchFinishEvent,
    # match stream
    m.MatchFull, m.HandStart, m.Blind, m.StreetStart, m.ActionMessage, m.Decide,
    m.DecisionResult, m.HandEnd, m.SeatStatus, m.MatchEnd,
    # requests and responses
    m.DecisionRequest, m.DecisionAccepted, m.ChallengeRequest, m.DeclineRequest, m.SeekRequest,
    m.Seek, m.Challenge, m.Account, m.OnlineBots, m.ErrorBody, m.Format,
]


def json_schema() -> dict:
    return {
        "title": "Poker arena bot API",
        "api_version": m.API_VERSION,
        "spec": "docs/bot-api.md",
        "messages": {model.__name__: model.model_json_schema() for model in MODELS},
    }


if __name__ == "__main__":
    print(json.dumps(json_schema(), indent=2, sort_keys=True))
