# Arena bot API

Version 1. This is the API that poker-arena serves and that bots use to
play there, through the bridge (`arena connect`) or their own client. It
follows the [lichess Bot API](https://lichess.org/api#tag/Bot): one
long-lived event stream per bot, one stream per match, and plain HTTP
POSTs for actions.

The message models in `poker_harness/protocol/` are the machine-readable
version of this document. Every JSON example below is checked against
them by the test suite, so the two can't drift apart.

**Contents:** [Conventions](#conventions) ·
[Lifecycle](#lifecycle) · [Endpoints](#endpoints) ·
[Event stream](#event-stream) · [Match stream](#match-stream) ·
[The decide state](#the-decide-state) · [Time controls](#time-controls) ·
[Reconnecting](#reconnecting) · [What each message reveals](#what-each-message-reveals) ·
[Errors and rate limits](#errors-and-rate-limits) ·
[Compatibility](#compatibility) · [Mapping from lichess](#mapping-from-lichess)

---

## Conventions

- **Base URL:** the arena's origin, e.g. `https://arena.example.com`. All
  paths below start with `/api`.
- **Authentication:** every request carries `Authorization: Bearer
  <token>`. A token belongs to exactly one bot. Scopes: `bot:play`
  (streams, decisions, challenges, seeks) and `bot:read` (account and
  history).
- **JSON:** request and response bodies are JSON objects with snake_case
  keys. Message types are snake_case strings in a `type` field.
- **Streams** are long-lived `GET` responses in
  [NDJSON](https://github.com/ndjson/ndjson-spec): one JSON object per
  line. The server writes an empty line at least every 10 seconds as a
  keepalive. Clients ignore empty lines and should reconnect after 30
  seconds of silence.
- **Bot names** are a bot's stable public identity. They appear wherever
  a bot is referred to (`bot`, `bot_id`, `name`).
- **Seats** are numbered 0-8 and fixed for a whole match. Lists indexed by
  seat (such as `stacks`) have one entry per seat.
- **Cards** are two-character strings: rank `23456789TJQKA` then suit
  `shdc`, e.g. `"As"`, `"Td"`.
- **Amounts** are integer chips. A raise amount is always the **total**
  bet for the street, not the increase.

## Lifecycle

```
bot                                   arena
 │  GET /api/stream/event  ─────────────►│  (stays open)
 │◄───────────── {"type":"challenge"} ───│  someone challenges you
 │  POST /api/challenge/{id}/accept ────►│
 │◄─────────── {"type":"match_start"} ───│
 │  GET /api/bot/match/{id}/stream ─────►│  (stays open for the match)
 │◄──── match_full, hand_start, blind,   │
 │      street, action, ...              │
 │◄────────────── {"type":"decide"} ─────│  your turn
 │  POST /api/bot/match/{id}/decision ──►│
 │◄────── decision_result, action, ...   │
 │◄───────────── {"type":"hand_end"} ────│  ... repeated for every hand
 │◄──────────── {"type":"match_end"} ────│  match stream closes
 │◄────────── {"type":"match_finish"} ───│  (event stream)
```

A bot is **online** while its event stream is open. A bot can play
several matches at once: it opens one match stream per `match_start`.

---

## Endpoints

| Method | Path | Scope | Purpose |
|---|---|---|---|
| GET | `/api/account` | `bot:read` | This bot's profile ([Account](#account)) |
| GET | `/api/account/playing` | `bot:read` | Matches in progress (`{"matches": [MatchRef...]}`) |
| GET | `/api/token/test` | any | `200 {"ok": true, "bot": name, "scopes": [...]}` if the token works |
| GET | `/api/stream/event` | `bot:play` | The [event stream](#event-stream) |
| GET | `/api/bot/online` | `bot:read` | Bots online now ([OnlineBots](#online-bots)) |
| POST | `/api/challenge/{bot}` | `bot:play` | Challenge another bot heads-up or to a table ([ChallengeRequest](#challenge)) |
| POST | `/api/challenge/{id}/accept` | `bot:play` | Accept a challenge |
| POST | `/api/challenge/{id}/decline` | `bot:play` | Decline, with a reason ([DeclineRequest](#challenge)) |
| POST | `/api/challenge/{id}/cancel` | `bot:play` | Cancel your own challenge |
| POST | `/api/seek` | `bot:play` | Queue for a table of a format ([SeekRequest](#seek)) |
| DELETE | `/api/seek/{id}` | `bot:play` | Leave the queue |
| GET | `/api/bot/match/{id}/stream` | `bot:play` | The [match stream](#match-stream) |
| POST | `/api/bot/match/{id}/decision` | `bot:play` | Answer a `decide` ([DecisionRequest](#decision)) |
| POST | `/api/bot/match/{id}/leave` | `bot:play` | Stop playing: your seat checks/folds for the rest of the match |

Owner history endpoints (your own matches, hands and decisions) are
specified separately with the production client (toolkit T6, arena A5).

### Account

```json model=Account
{"name": "river-rat", "title": "BOT", "owner": "fred", "ratings": {"hu": 1512.0},
 "created_at": "2026-10-02T12:00:00Z", "scopes": ["bot:play", "bot:read"], "api_version": 1}
```

### Online bots

```json model=OnlineBots
{"bots": [{"name": "river-rat", "rating": 1512.0, "title": "BOT"},
          {"name": "house-shark", "rating": null, "title": "BOT"}]}
```

### Challenge

A challenge names an opponent and a [format](#formats). Heads-up formats
start when the challenge is accepted. The response is the created
[Challenge](#challenge-object).

```json model=ChallengeRequest
{"format": {"seats": 2, "hands": 200, "small_blind": 50, "big_blind": 100, "stack": 10000,
            "reset_stacks": true, "clock": {"decision_s": 30, "bank_s": 300}},
 "rated": true}
```

Decline reasons: `generic`, `later`, `format`, `rated`, `casual`,
`too_many_matches`.

```json model=DeclineRequest
{"reason": "format"}
```

### Seek

A seek joins the queue for a format, usually a multi-seat table. When
enough bots are queued the arena starts a match and sends each of them
`match_start`. Seeks expire after 10 minutes (`seek_expired`) and are
removed when the bot's event stream closes. The response is the created
`Seek`.

```json model=SeekRequest
{"format": {"seats": 6, "hands": 300, "small_blind": 50, "big_blind": 100, "stack": 10000,
            "clock": {"decision_s": 30, "bank_s": 600}},
 "rated": true}
```

```json model=Seek
{"id": "sk_9f2", "format": {"seats": 6, "hands": 300, "small_blind": 50, "big_blind": 100,
 "stack": 10000, "clock": {"decision_s": 30, "bank_s": 600}}, "rated": true, "status": "open"}
```

### Decision

The answer to a `decide`. `decision_id` must be the id of the pending
decision; `logs` are optional notes stored with the decision (up to 200
entries, 16 KB each), the same as `ctx.log` locally.

```json model=DecisionRequest
{"decision_id": "m_7Kq2:d41",
 "action": {"action": "raise", "amount": 900},
 "logs": [{"msg": "3-bet for value", "data": {"equity": 0.64}}]}
```

The response says what was applied. An illegal action is adjusted the
same way as in local matches (raises below the minimum go up to it,
`check` facing a bet becomes `call`, unknown actions fold), with
`corrected: true`.

```json model=DecisionAccepted
{"ok": true, "applied": {"action": "raise", "amount": 900}, "corrected": false}
```

### Formats

```json model=Format
{"seats": 2, "hands": 200, "small_blind": 50, "big_blind": 100, "stack": 10000,
 "reset_stacks": true, "clock": {"decision_s": 30, "bank_s": 300}}
```

| Field | Meaning |
|---|---|
| `seats` | 2-9 |
| `hands` | match length (the match also ends when only one bot has chips) |
| `small_blind`, `big_blind`, `stack` | chips |
| `reset_stacks` | every hand starts from `stack`, so nobody busts (results are summed) |
| `clock` | see [Time controls](#time-controls) |

---

## Event stream

`GET /api/stream/event`: one per bot. A new connection replaces the
previous one. On connecting, the server first sends `match_start` for
every match the bot is still in.

<a id="challenge-object"></a>
`challenge`: someone challenged you, or your own challenge was created.

```json model=ChallengeEvent
{"type": "challenge", "challenge": {"id": "ch_31x", "challenger": {"name": "house-shark"},
 "dest": {"name": "river-rat"}, "format": {"seats": 2, "hands": 100}, "rated": false,
 "status": "created", "expires_at": "2026-10-02T12:01:00Z"}}
```

`challenge_canceled` and `challenge_declined` (with a `reason`):

```json model=ChallengeDeclinedEvent
{"type": "challenge_declined", "reason": "format", "challenge": {"id": "ch_31x",
 "challenger": {"name": "river-rat"}, "dest": {"name": "house-shark"},
 "format": {"seats": 2, "hands": 100}, "status": "declined"}}
```

`seek_expired`:

```json model=SeekExpiredEvent
{"type": "seek_expired", "seek": {"id": "sk_9f2", "format": {"seats": 6}, "status": "expired"}}
```

`match_start`: open the match stream now. `your_seat` is your seat;
`challenge_id` or `seek_id` says which challenge or seek of yours the
match came from, if any.

```json model=MatchStartEvent
{"type": "match_start", "match": {"id": "m_7Kq2", "format": {"seats": 2, "hands": 100},
 "rated": false, "your_seat": 1, "status": "started", "challenge_id": "ch_31x",
 "seats": [{"seat": 0, "bot": {"name": "house-shark"}}, {"seat": 1, "bot": {"name": "river-rat"}}]}}
```

`match_finish`: the match is over (also sent if it was aborted).

```json model=MatchFinishEvent
{"type": "match_finish", "match": {"id": "m_7Kq2", "format": {"seats": 2}, "status": "finished",
 "seats": [{"seat": 0, "bot": {"name": "house-shark"}}, {"seat": 1, "bot": {"name": "river-rat"}}]}}
```

---

## Match stream

`GET /api/bot/match/{id}/stream`: one per match. The first message is
always `match_full`; after that, messages arrive as the match plays. The
stream closes after `match_end`.

`match_full`: the match, the stacks, your remaining bank, the messages of
the hand in progress so far (empty between hands), and the pending
`decide` if it's your turn.

```json model=MatchFull
{"type": "match_full", "match": {"id": "m_7Kq2", "format": {"seats": 2, "hands": 100},
 "your_seat": 1, "seats": [{"seat": 0, "bot": {"name": "house-shark"}},
 {"seat": 1, "bot": {"name": "river-rat"}}]},
 "hands_played": 0, "stacks": [10000, 10000], "bank_s": 300.0, "hand": [], "pending": null}
```

`hand_start`: a new hand. `your_cards` are yours only.

```json model=HandStart
{"type": "hand_start", "hand_num": 0, "hand_id": "m_7Kq2_h0000", "dealer_seat": 0,
 "positions": {"0": "BTN", "1": "BB"}, "blinds": [50, 100], "stacks": [10000, 10000],
 "your_cards": ["As", "Kh"]}
```

`blind`:

```json model=Blind
{"type": "blind", "hand_num": 0, "seat": 0, "bot": "house-shark", "kind": "small_blind", "amount": 50}
```

`street`: a street begins, with the board so far.

```json model=StreetStart
{"type": "street", "hand_num": 0, "street": "flop", "board": ["Kd", "7c", "2s"]}
```

`action`: any seat acted (as applied by the engine). `amount` is the
chips put in for a call, the total bet for a raise, and the street total
for an all-in.

```json model=ActionMessage
{"type": "action", "hand_num": 0, "street": "preflop", "seat": 0, "bot": "house-shark",
 "action": "raise", "amount": 300, "pot_after": 400, "stacks": [9700, 9900]}
```

`decide`: your turn. `state` is exactly what `decide(state, ctx)`
receives locally ([The decide state](#the-decide-state)). Answer with
`POST /api/bot/match/{id}/decision`.

```json model=Decide
{"type": "decide", "decision_id": "m_7Kq2:d2", "hand_num": 0, "decision_s": 30, "bank_s": 300.0,
 "state": {"type": "action_request", "hand_id": "m_7Kq2_h0000", "hand_num": 0, "street": "preflop",
  "seat_to_act": 1, "dealer_seat": 0, "small_blind": 50, "big_blind": 100, "pot": 400,
  "community_cards": [], "current_bet": 300, "min_raise_to": 500, "amount_owed": 200,
  "can_check": false,
  "legal_actions": {"can_fold": true, "can_check": false, "call_amount": 200, "can_raise": true,
                    "min_raise_to": 500, "max_raise_to": 10000},
  "your_cards": ["As", "Kh"], "your_stack": 9900, "your_bet_this_street": 100,
  "players": [
   {"seat": 0, "bot_id": "house-shark", "stack": 9700, "state": "active", "is_folded": false,
    "is_all_in": false, "bet_this_street": 300, "hole_cards": null},
   {"seat": 1, "bot_id": "river-rat", "stack": 9900, "state": "active", "is_folded": false,
    "is_all_in": false, "bet_this_street": 100, "hole_cards": null}],
  "action_log": [{"seat": 0, "action": "small_blind", "amount": 50},
                 {"seat": 1, "action": "big_blind", "amount": 100},
                 {"seat": 0, "action": "raise", "amount": 300}],
  "match_action_log": []}}
```

`decision_result`: sent only to you, after your decision is applied (or
after the clock ran out). `error` is `timeout`, `disconnected` or
`invalid` when the arena acted for you.

```json model=DecisionResult
{"type": "decision_result", "decision_id": "m_7Kq2:d2", "applied": {"action": "raise", "amount": 900},
 "corrected": false, "error": null, "bank_s": 300.0}
```

`hand_end`: results. `revealed` holds the hole cards of every hand that
reached showdown, and nothing else. Uncalled chips returned to a bettor
are reported separately from the pot.

```json model=HandEnd
{"type": "hand_end", "hand_num": 0, "board": ["Kd", "7c", "2s", "9h", "3c"], "pot": 1800,
 "showdown": true, "winners": [{"seat": 1, "bot": "river-rat", "amount": 1800, "pot_type": "main"}],
 "uncalled": null, "revealed": {"0": ["Qd", "Qc"], "1": ["As", "Kh"]},
 "stacks": [9100, 10900], "delta": [-900, 900]}
```

`seat_status`: a bot's match stream connected or disconnected.

```json model=SeatStatus
{"type": "seat_status", "seat": 0, "bot": "house-shark", "connected": false}
```

`match_end`: final result; the stream closes after it.

```json model=MatchEnd
{"type": "match_end", "reason": "hands_complete", "hands_played": 100, "stacks": [8450, 11550],
 "chip_delta": {"house-shark": -1550, "river-rat": 1550}}
```

---

## The decide state

`decide.state` is the same object a bot receives locally, built by the
same engine, so a bot runs unchanged in both places.

| Field | Meaning |
|---|---|
| `hand_id`, `hand_num` | which hand |
| `street` | `preflop`, `flop`, `turn` or `river` |
| `seat_to_act` | your seat |
| `dealer_seat`, `small_blind`, `big_blind` | table position and blinds |
| `pot` | chips in the pot, including this street's bets |
| `community_cards` | the board so far |
| `current_bet` | the highest bet this street |
| `min_raise_to` | the smallest legal raise total |
| `amount_owed`, `can_check` | chips needed to call; whether checking is free |
| `legal_actions` | `can_fold`, `can_check`, `call_amount`, `can_raise`, `min_raise_to`, `max_raise_to` (all-in) |
| `your_cards`, `your_stack`, `your_bet_this_street` | yours |
| `players` | public info for every seat, including `bot_id` (the bot's name); `hole_cards` is always null |
| `action_log` | every action this hand, including blinds |
| `match_action_log` | the last 200 applied actions of the match: `hand_num`, `seat`, `bot_id`, `action`, `amount` |

Valid answers: `fold`, `check`, `call`, `raise` with `amount` (the total
bet), `all_in`.

## Time controls

A format's `clock` has `decision_s` and `bank_s`.

- The clock for a decision starts when the arena sends `decide`.
- Up to `decision_s` seconds are free. Time beyond that comes out of the
  bot's bank, which starts at `bank_s` for the match and doesn't refill.
- If no decision arrives within `decision_s` plus the remaining bank, the
  arena acts for the bot: check if it can, otherwise fold
  (`decision_result.error = "timeout"`), and the bank is empty from then
  on.
- `decide` carries `decision_s` and the current `bank_s`. Answer with
  some margin for network latency: the arena times the decision from when
  it sends `decide` to when it receives the POST.

## Reconnecting

- **Event stream:** reconnect at any time. The server resends
  `match_start` for every match still in progress.
- **Match stream:** reconnect at any time. `match_full` brings the bot up
  to date, including the hand in progress and a `pending` decide if it's
  the bot's turn. The clock keeps running while a bot is disconnected.
- **Missing bots:** if a seat's match stream hasn't connected within 60
  seconds of `match_start`, the match is aborted. A bot that disconnects
  later is acted for (check/fold) until it comes back; other bots see
  `seat_status`.
- Decisions are idempotent by `decision_id`: repeating a POST for an
  already-applied decision returns `409 decision_stale` and changes
  nothing.

## What each message reveals

| Information | Who receives it |
|---|---|
| Your own hole cards | you (`hand_start`, `decide.state`) |
| Opponents' hole cards | everyone at the table, only if their hand reaches showdown (`hand_end.revealed`) |
| Board cards | everyone, as they are dealt (`street`) |
| Actions, bets, stacks | everyone (`blind`, `action`, `hand_end`) |
| Undealt cards, folded or unshown hands, the seed | nobody (stored only by the arena) |
| Your decisions' notes (`logs`) | your owner, through the history API |
| Other bots' notes | nobody but their owners |

## Errors and rate limits

Errors use an HTTP status and a JSON body:

```json model=ErrorBody
{"error": "decision m_7Kq2:d41 is not pending", "code": "decision_stale"}
```

| Status | `code` examples | When |
|---|---|---|
| 400 | `invalid_body`, `invalid_format` | malformed request |
| 401 | `bad_token` | missing, unknown or revoked token |
| 403 | `missing_scope`, `not_your_match` | the token can't do this |
| 404 | `not_found` | no such bot, challenge, seek or match |
| 409 | `decision_stale`, `challenge_closed`, `already_seeking` | conflicts with current state |
| 429 | `rate_limited` | too many requests; wait for `Retry-After` seconds |

Limits are per token and published with the arena. Clients back off
exponentially on 429 and 5xx responses.

## Compatibility

- The server may add fields to any message and add new message types.
  Clients must ignore fields and types they don't know (the toolkit's
  parsers return `UnknownMessage` for unknown types).
- Removing or renaming a field, or changing its meaning, is a breaking
  change and gets a new major version under `/api/v2/...`. `api_version`
  in `GET /api/account` says which version the server speaks.

## Mapping from lichess

| lichess | This API |
|---|---|
| `GET /api/stream/event` (`challenge`, `gameStart`, `gameFinish`) | `GET /api/stream/event` (`challenge`, `match_start`, `match_finish`) |
| `POST /api/challenge/{user}`, `.../accept`, `.../decline`, `.../cancel` | the same |
| `GET /api/bot/game/stream/{id}` (`gameFull`, `gameState`) | `GET /api/bot/match/{id}/stream` (`match_full`, then per-hand messages) |
| `POST /api/bot/game/{id}/move/{uci}` | `POST /api/bot/match/{id}/decision` with a JSON body |
| `POST /api/bot/game/{id}/resign` | `POST /api/bot/match/{id}/leave` |
| Seeks via `POST /api/board/seek` | `POST /api/seek`, needed for multi-seat tables |
| Chess clock | per-decision time plus a match bank |
