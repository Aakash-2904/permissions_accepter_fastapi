from collections.abc import AsyncIterator

import httpx
import pytest

from relay.app import create_app
from relay.config import Settings

API_KEY = "test-key-0123456789abcdef"


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail = False

    async def send(self, request_id: str, tool_name: str, summary: str) -> None:
        if self.fail:
            raise RuntimeError("ntfy down")
        self.sent.append((request_id, tool_name, summary))


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "relay_api_key": API_KEY,
        "ntfy_topic": "test-topic",
        "public_url": "https://relay.example",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
async def client(notifier: FakeNotifier) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings=make_settings(), notifier=notifier)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.app = app  # type: ignore[attr-defined]
            yield c
