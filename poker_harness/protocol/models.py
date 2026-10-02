"""
Messages of the arena bot API (docs/bot-api.md), as pydantic models.

These are shared by the bridge client (`arena connect`), the mock server
(`arena serve-mock`) and poker-arena, so all three agree on every field.

Compatibility rules (see the spec): models accept unknown fields, and the
parse_* helpers turn messages of unknown type into UnknownMessage instead
of failing, so servers can add fields and message types without breaking
older clients. Removing or changing a field is a breaking change.
"""

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

API_VERSION = 1

Street = Literal["preflop", "flop", "turn", "river"]
ActionName = Literal["fold", "check", "call", "raise", "all_in"]


class Model(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------

class Clock(Model):
    """Time control. Each decision gets decision_s; past that, time comes out
    of a bank of bank_s for the whole match. When both run out the seat
    checks if it can, otherwise folds."""
    decision_s: float = Field(30, gt=0)
    bank_s: float = Field(300, ge=0)


class Format(Model):
    """What kind of match: table size, length, blinds, stacks, clock."""
    seats: int = Field(2, ge=2, le=9)
    hands: int = Field(100, ge=1, le=10_000)
    small_blind: int = Field(50, gt=0)
    big_blind: int = Field(100, gt=0)
    stack: int = Field(10_000, gt=0)
    reset_stacks: bool = Field(False, description="every hand starts from `stack` (no busting)")
    clock: Clock = Field(default_factory=Clock)

    @model_validator(mode="after")
    def _blinds(self):
        if self.small_blind > self.big_blind:
            raise ValueError("small_blind must be <= big_blind")
        return self


class BotRef(Model):
    """A bot's stable public identity."""
    name: str
    rating: Optional[float] = None
    title: Literal["BOT"] = "BOT"


class SeatInfo(Model):
    seat: int = Field(ge=0, le=8)
    bot: BotRef


class Challenge(Model):
    id: str
    challenger: BotRef
    dest: BotRef
    format: Format
    rated: bool = False
    status: Literal["created", "accepted", "declined", "canceled", "expired"] = "created"
    expires_at: Optional[str] = Field(None, description="ISO 8601 time the challenge lapses")


class Seek(Model):
    id: str
    format: Format
    rated: bool = False
    status: Literal["open", "matched", "canceled", "expired"] = "open"


class MatchRef(Model):
    id: str
    format: Format
    rated: bool = False
    seats: list[SeatInfo]
    your_seat: Optional[int] = Field(None, description="absent for spectators")
    status: Literal["started", "finished", "aborted"] = "started"
    challenge_id: Optional[str] = Field(None, description="the challenge this match came from")
    seek_id: Optional[str] = Field(None, description="your seek this match came from")


class Winner(Model):
    seat: int
    bot: str
    amount: int
    pot_type: Literal["main", "side"]


class Uncalled(Model):
    seat: int
    bot: str
    amount: int


class LegalActions(Model):
    can_fold: bool
    can_check: bool
    call_amount: int
    can_raise: bool
    min_raise_to: Optional[int]
    max_raise_to: Optional[int]


class PublicPlayer(Model):
    seat: int
    bot_id: str
    stack: int
    state: Literal["active", "folded", "all_in", "busted"]
    is_folded: bool
    is_all_in: bool
    bet_this_street: int
    hole_cards: None = None


class LoggedAction(Model):
    seat: int
    action: str
    amount: int


class MatchLogEntry(Model):
    hand_num: int
    seat: int
    bot_id: str
    action: str
    amount: Optional[int] = None


class DecideState(Model):
    """Exactly the state a bot's decide(state, ctx) receives, locally and in
    production."""
    type: Literal["action_request"] = "action_request"
    hand_id: str
    hand_num: int
    street: Street
    seat_to_act: int
    dealer_seat: int
    small_blind: int
    big_blind: int
    pot: int
    community_cards: list[str]
    current_bet: int
    min_raise_to: int
    amount_owed: int
    can_check: bool
    legal_actions: LegalActions
    your_cards: list[str]
    your_stack: int
    your_bet_this_street: int
    players: list[PublicPlayer]
    action_log: list[LoggedAction]
    match_action_log: list[MatchLogEntry] = Field(default_factory=list)


class ActionBody(Model):
    action: ActionName
    amount: Optional[int] = Field(None, description="raise: total bet for the street, not the increase")


class LogEntry(Model):
    msg: Optional[str] = None
    data: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Event stream: GET /api/stream/event
# ---------------------------------------------------------------------------

class ChallengeEvent(Model):
    type: Literal["challenge"]
    challenge: Challenge


class ChallengeCanceledEvent(Model):
    type: Literal["challenge_canceled"]
    challenge: Challenge


class ChallengeDeclinedEvent(Model):
    type: Literal["challenge_declined"]
    challenge: Challenge
    reason: str = "generic"


class SeekExpiredEvent(Model):
    type: Literal["seek_expired"]
    seek: Seek


class MatchStartEvent(Model):
    type: Literal["match_start"]
    match: MatchRef


class MatchFinishEvent(Model):
    type: Literal["match_finish"]
    match: MatchRef


Event = Annotated[Union[ChallengeEvent, ChallengeCanceledEvent, ChallengeDeclinedEvent,
                        SeekExpiredEvent, MatchStartEvent, MatchFinishEvent],
                  Field(discriminator="type")]


# ---------------------------------------------------------------------------
# Match stream: GET /api/bot/match/{id}/stream
# ---------------------------------------------------------------------------

class HandStart(Model):
    type: Literal["hand_start"]
    hand_num: int
    hand_id: str
    dealer_seat: int
    positions: dict[str, str] = Field(description="seat number (as a string) -> position name")
    blinds: list[int] = Field(min_length=2, max_length=2)
    stacks: list[int] = Field(description="every seat's stack before blinds; 0 = sitting out")
    your_cards: list[str] = Field(description="only your own hole cards")


class Blind(Model):
    type: Literal["blind"]
    hand_num: int
    seat: int
    bot: str
    kind: Literal["small_blind", "big_blind"]
    amount: int


class StreetStart(Model):
    type: Literal["street"]
    hand_num: int
    street: Street
    board: list[str]


class ActionMessage(Model):
    type: Literal["action"]
    hand_num: int
    street: Street
    seat: int
    bot: str
    action: ActionName
    amount: int
    pot_after: int
    stacks: list[int]


class Decide(Model):
    type: Literal["decide"]
    decision_id: str
    hand_num: int
    decision_s: float = Field(description="time for this decision before the bank is used")
    bank_s: float = Field(description="time left in your bank for the match")
    state: DecideState


class DecisionResult(Model):
    """Sent only to the bot that decided."""
    type: Literal["decision_result"]
    decision_id: str
    applied: ActionBody
    corrected: bool = Field(description="the reply was illegal and the engine adjusted it")
    error: Optional[Literal["timeout", "disconnected", "invalid"]] = None
    bank_s: float


class HandEnd(Model):
    type: Literal["hand_end"]
    hand_num: int
    board: list[str]
    pot: int
    showdown: bool
    winners: list[Winner]
    uncalled: Optional[Uncalled] = None
    revealed: dict[str, list[str]] = Field(
        default_factory=dict, description="seat -> hole cards, only for hands shown at showdown")
    stacks: list[int]
    delta: list[int]


class SeatStatus(Model):
    type: Literal["seat_status"]
    seat: int
    bot: str
    connected: bool


class MatchFull(Model):
    """First message on every (re)connection: the match and where it stands."""
    type: Literal["match_full"]
    match: MatchRef
    hands_played: int
    stacks: list[int]
    bank_s: float
    hand: list[dict] = Field(default_factory=list,
                             description="messages of the hand in progress so far, from hand_start")
    pending: Optional[Decide] = None


class MatchEnd(Model):
    type: Literal["match_end"]
    reason: Literal["hands_complete", "one_player_left", "aborted"]
    hands_played: int
    stacks: list[int]
    chip_delta: dict[str, int]


MatchMessage = Annotated[Union[MatchFull, HandStart, Blind, StreetStart, ActionMessage, Decide,
                               DecisionResult, HandEnd, SeatStatus, MatchEnd],
                         Field(discriminator="type")]


# ---------------------------------------------------------------------------
# Requests and other responses
# ---------------------------------------------------------------------------

class DecisionRequest(Model):
    """POST /api/bot/match/{id}/decision"""
    decision_id: str
    action: ActionBody
    logs: list[LogEntry] = Field(default_factory=list, max_length=200)


class DecisionAccepted(Model):
    ok: Literal[True] = True
    applied: ActionBody
    corrected: bool


class ChallengeRequest(Model):
    """POST /api/challenge/{bot}"""
    format: Format = Field(default_factory=Format)
    rated: bool = False


class DeclineRequest(Model):
    """POST /api/challenge/{id}/decline"""
    reason: Literal["generic", "later", "format", "rated", "casual", "too_many_matches"] = "generic"


class SeekRequest(Model):
    """POST /api/seek"""
    format: Format = Field(default_factory=Format)
    rated: bool = False


class Account(Model):
    """GET /api/account"""
    name: str
    title: Literal["BOT"] = "BOT"
    owner: str
    ratings: dict[str, float] = Field(default_factory=dict)
    created_at: str
    scopes: list[str]
    api_version: int = API_VERSION


class OnlineBots(Model):
    """GET /api/bot/online"""
    bots: list[BotRef]


class ErrorBody(Model):
    error: str
    code: str


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class UnknownMessage(Model):
    """A message of a type this version doesn't know. Clients must ignore
    these rather than fail."""
    type: str


_events = TypeAdapter(Event)
_match_messages = TypeAdapter(MatchMessage)
EVENT_TYPES = {m.model_fields["type"].annotation.__args__[0]
               for m in (ChallengeEvent, ChallengeCanceledEvent, ChallengeDeclinedEvent,
                         SeekExpiredEvent, MatchStartEvent, MatchFinishEvent)}
MATCH_TYPES = {m.model_fields["type"].annotation.__args__[0]
               for m in (MatchFull, HandStart, Blind, StreetStart, ActionMessage, Decide,
                         DecisionResult, HandEnd, SeatStatus, MatchEnd)}


def parse_event(data: dict):
    """An event-stream message -> its model (UnknownMessage for new types)."""
    if data.get("type") not in EVENT_TYPES:
        return UnknownMessage.model_validate(data)
    return _events.validate_python(data)


def parse_match_message(data: dict):
    """A match-stream message -> its model (UnknownMessage for new types)."""
    if data.get("type") not in MATCH_TYPES:
        return UnknownMessage.model_validate(data)
    return _match_messages.validate_python(data)


__all__ = [n for n in dir() if not n.startswith("_")] + ["ValidationError"]
