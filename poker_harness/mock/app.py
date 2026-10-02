"""The HTTP layer of the mock arena: FastAPI routes for docs/bot-api.md."""

import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from poker_harness.mock.arena import ApiError, MockArena
from poker_harness.protocol import models as m


def create_app(arena: MockArena, keepalive_s: float = 7.0) -> FastAPI:
    app = FastAPI(title="Poker arena (mock)", description="The arena bot API, docs/bot-api.md",
                  version=str(m.API_VERSION))
    app.state.arena = arena

    @app.exception_handler(ApiError)
    async def api_error(request: Request, e: ApiError):
        return JSONResponse({"error": e.message, "code": e.code}, status_code=e.status)

    @app.exception_handler(RequestValidationError)
    async def bad_body(request: Request, e: RequestValidationError):
        problems = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
        return JSONResponse({"error": problems, "code": "invalid_body"}, status_code=400)

    def bot(request: Request):
        return arena.auth(request.headers.get("authorization"))

    def ndjson(queue: asyncio.Queue, on_close) -> StreamingResponse:
        async def lines():
            try:
                while True:
                    get = asyncio.ensure_future(queue.get())
                    try:
                        done, _ = await asyncio.wait({get}, timeout=keepalive_s)
                    finally:
                        if not get.done():
                            get.cancel()
                    if not done:
                        yield b"\n"                      # keepalive
                        continue
                    item = get.result()
                    if item is None:
                        return
                    yield (json.dumps(item) + "\n").encode()
            finally:
                on_close()
        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @app.get("/api/account")
    async def account(request: Request) -> m.Account:
        return arena.account(bot(request))

    @app.get("/api/account/playing")
    async def playing(request: Request):
        return {"matches": [r.model_dump(mode="json") for r in arena.playing(bot(request))]}

    @app.get("/api/token/test")
    async def token_test(request: Request):
        b = bot(request)
        return {"ok": True, "bot": b.name, "scopes": ["bot:play", "bot:read"]}

    @app.get("/api/bot/online")
    async def online(request: Request) -> m.OnlineBots:
        bot(request)
        return arena.online()

    @app.get("/api/stream/event")
    async def events(request: Request):
        b = bot(request)
        q = arena.open_events(b)
        return ndjson(q, lambda: arena.close_events(b, q))

    @app.post("/api/challenge/{name}")
    async def challenge(name: str, body: m.ChallengeRequest, request: Request) -> m.Challenge:
        return arena.challenge(bot(request), name, body)

    @app.post("/api/challenge/{cid}/accept")
    async def accept(cid: str, request: Request):
        arena.accept(bot(request), cid)
        return {"ok": True}

    @app.post("/api/challenge/{cid}/decline")
    async def decline(cid: str, request: Request):
        raw = await request.body()
        body = m.DeclineRequest.model_validate_json(raw) if raw else m.DeclineRequest()
        arena.decline(bot(request), cid, body.reason)
        return {"ok": True}

    @app.post("/api/challenge/{cid}/cancel")
    async def cancel(cid: str, request: Request):
        arena.cancel(bot(request), cid)
        return {"ok": True}

    @app.post("/api/seek")
    async def seek(body: m.SeekRequest, request: Request) -> m.Seek:
        return arena.seek(bot(request), body)

    @app.delete("/api/seek/{sid}")
    async def unseek(sid: str, request: Request):
        arena.cancel_seek(bot(request), sid)
        return {"ok": True}

    @app.get("/api/bot/match/{mid}/stream")
    async def match_stream(mid: str, request: Request):
        b = bot(request)
        q = arena.open_match(b, mid)
        return ndjson(q, lambda: arena.close_match(b, mid, q))

    @app.post("/api/bot/match/{mid}/decision")
    async def decision(mid: str, body: m.DecisionRequest, request: Request) -> m.DecisionAccepted:
        return arena.post_decision(bot(request), mid, body)

    @app.post("/api/bot/match/{mid}/leave")
    async def leave(mid: str, request: Request):
        arena.leave(bot(request), mid)
        return {"ok": True}

    return app
