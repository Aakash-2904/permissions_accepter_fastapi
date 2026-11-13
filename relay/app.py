"""FastAPI relay between the Claude Code hook and the phone.

State is in memory: run with a single worker (`uvicorn relay.app:app --workers 1`).
"""

import asyncio
import contextlib
import logging
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from relay.config import Settings, get_settings
from relay.notifier import Notifier, NtfyNotifier
from relay.store import AlreadyDecidedError, Decision, NotFoundError, RequestStore

logger = logging.getLogger("relay")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class CreateRequest(BaseModel):
    tool_name: str = Field(min_length=1, max_length=200)
    summary: str = Field(max_length=5000)
    session_id: str | None = Field(default=None, max_length=200)
    cwd: str | None = Field(default=None, max_length=1000)


class CreateResponse(BaseModel):
    id: str


class PollResponse(BaseModel):
    decision: Decision | None


class RateLimiter:
    """Sliding-window limiter. Global on purpose: one leaked key = one bucket."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._hits and now - self._hits[0] > self._window:
            self._hits.popleft()
        if len(self._hits) >= self._limit:
            return False
        self._hits.append(now)
        return True


def short_id(request_id: str) -> str:
    return request_id[:6]


async def _cleanup_loop(store: RequestStore, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        removed = store.purge_expired()
        if removed:
            logger.info("purged %d expired request(s)", removed)


def create_app(settings: Settings | None = None, notifier: Notifier | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or get_settings()
        app.state.settings = cfg
        app.state.store = RequestStore(ttl_seconds=cfg.request_ttl_seconds)
        app.state.limiter = RateLimiter(cfg.rate_limit_requests, cfg.rate_limit_window_seconds)
        owned = (
            None
            if notifier
            else NtfyNotifier(
                server=cfg.ntfy_server,
                topic=cfg.ntfy_topic,
                public_url=cfg.public_url,
                token=cfg.ntfy_token,
            )
        )
        app.state.notifier = notifier or owned
        cleanup = asyncio.create_task(_cleanup_loop(app.state.store, cfg.cleanup_interval_seconds))
        try:
            yield
        finally:
            cleanup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup
            if owned is not None:
                await owned.aclose()

    app = FastAPI(title="Phone Approval Relay", lifespan=lifespan)

    def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
        expected = request.app.state.settings.relay_api_key
        if x_api_key is None or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="invalid api key")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/requests", response_model=CreateResponse, dependencies=[Depends(require_api_key)])
    async def create_request(body: CreateRequest, request: Request) -> CreateResponse:
        state = request.app.state
        if not state.limiter.allow():
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        item = state.store.create(body.tool_name)
        try:
            await state.notifier.send(item.id, body.tool_name, body.summary)
        except Exception as exc:  # noqa: BLE001 - any notifier failure is a 502
            logger.warning("notify failed tool=%s id=%s err=%s", body.tool_name, short_id(item.id), type(exc).__name__)
            raise HTTPException(status_code=502, detail="notification failed") from exc
        logger.info("created tool=%s id=%s", body.tool_name, short_id(item.id))
        return CreateResponse(id=item.id)

    @app.get("/requests/{request_id}", response_model=PollResponse, dependencies=[Depends(require_api_key)])
    async def poll_request(request_id: str, request: Request, timeout: float = Query(default=25, ge=0)) -> PollResponse:
        state = request.app.state
        wait_for = min(timeout, state.settings.max_poll_timeout_seconds)
        try:
            decision = await state.store.wait(request_id, wait_for)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="unknown or expired request") from exc
        return PollResponse(decision=decision)

    @app.post("/decide/{request_id}/{decision}")
    async def decide(request_id: str, decision: str, request: Request) -> dict[str, str]:
        try:
            parsed = Decision(decision)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="decision must be 'allow' or 'deny'") from exc
        try:
            item = request.app.state.store.decide(request_id, parsed)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="unknown or expired request") from exc
        except AlreadyDecidedError as exc:
            raise HTTPException(status_code=409, detail="already decided") from exc
        logger.info("decided tool=%s id=%s decision=%s", item.tool_name, short_id(request_id), parsed.value)
        return {"status": "ok", "decision": parsed.value}

    return app


app = create_app()
