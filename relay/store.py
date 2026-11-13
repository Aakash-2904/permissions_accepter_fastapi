"""Pending-request store.

In-memory implementation. Keep the `RequestStore` method surface stable so a
Redis-backed store can replace it later without touching app.py.
"""

import asyncio
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class NotFoundError(Exception):
    """Request ID is unknown or expired."""


class AlreadyDecidedError(Exception):
    """A decision was already recorded for this request."""


@dataclass
class PendingRequest:
    id: str
    tool_name: str
    created_at: float
    decision: Decision | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)


class RequestStore:
    def __init__(self, ttl_seconds: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._items: dict[str, PendingRequest] = {}

    def create(self, tool_name: str) -> PendingRequest:
        request_id = secrets.token_urlsafe(32)
        item = PendingRequest(id=request_id, tool_name=tool_name, created_at=self._clock())
        self._items[request_id] = item
        return item

    def get(self, request_id: str) -> PendingRequest:
        item = self._items.get(request_id)
        if item is None or self._is_expired(item):
            raise NotFoundError(request_id)
        return item

    def decide(self, request_id: str, decision: Decision) -> PendingRequest:
        item = self.get(request_id)
        if item.decision is not None:
            raise AlreadyDecidedError(request_id)
        item.decision = decision
        item.event.set()
        return item

    async def wait(self, request_id: str, timeout: float) -> Decision | None:
        item = self.get(request_id)
        if item.decision is None:
            try:
                await asyncio.wait_for(item.event.wait(), timeout=timeout)
            except TimeoutError:
                return None
        return item.decision

    def purge_expired(self) -> int:
        expired = [rid for rid, item in self._items.items() if self._is_expired(item)]
        for rid in expired:
            del self._items[rid]
        return len(expired)

    def __len__(self) -> int:
        return len(self._items)

    def _is_expired(self, item: PendingRequest) -> bool:
        return self._clock() - item.created_at > self._ttl
