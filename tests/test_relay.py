import asyncio

import httpx
import pytest

from relay.app import create_app
from relay.notifier import NtfyNotifier
from relay.store import Decision, NotFoundError, RequestStore
from tests.conftest import API_KEY, FakeNotifier, make_settings

AUTH = {"x-api-key": API_KEY}
BODY = {"tool_name": "Bash", "summary": "ls -la", "session_id": "s1", "cwd": "/home/me/project"}


async def create(client: httpx.AsyncClient) -> str:
    resp = await client.post("/requests", json=BODY, headers=AUTH)
    assert resp.status_code == 200
    return resp.json()["id"]


async def test_health(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_create_sends_notification(client: httpx.AsyncClient, notifier: FakeNotifier) -> None:
    request_id = await create(client)
    assert len(request_id) >= 32
    assert notifier.sent == [(request_id, "Bash", "ls -la")]


@pytest.mark.parametrize("headers", [{}, {"x-api-key": "wrong"}])
async def test_create_requires_api_key(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    resp = await client.post("/requests", json=BODY, headers=headers)
    assert resp.status_code == 401


@pytest.mark.parametrize("headers", [{}, {"x-api-key": "wrong"}])
async def test_poll_requires_api_key(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    request_id = await create(client)
    resp = await client.get(f"/requests/{request_id}", headers=headers)
    assert resp.status_code == 401


async def test_create_returns_502_when_notifier_fails(client: httpx.AsyncClient, notifier: FakeNotifier) -> None:
    notifier.fail = True
    resp = await client.post("/requests", json=BODY, headers=AUTH)
    assert resp.status_code == 502


async def test_poll_returns_null_on_timeout(client: httpx.AsyncClient) -> None:
    request_id = await create(client)
    resp = await client.get(f"/requests/{request_id}", params={"timeout": 0.05}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"decision": None}


@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_poll_returns_decision_once_set(client: httpx.AsyncClient, decision: str) -> None:
    request_id = await create(client)
    poll = asyncio.create_task(client.get(f"/requests/{request_id}", params={"timeout": 5}, headers=AUTH))
    await asyncio.sleep(0.05)
    resp = await client.post(f"/decide/{request_id}/{decision}")
    assert resp.status_code == 200
    result = await asyncio.wait_for(poll, timeout=2)
    assert result.json() == {"decision": decision}


async def test_poll_after_decision_returns_immediately(client: httpx.AsyncClient) -> None:
    request_id = await create(client)
    await client.post(f"/decide/{request_id}/deny")
    resp = await client.get(f"/requests/{request_id}", params={"timeout": 30}, headers=AUTH)
    assert resp.json() == {"decision": "deny"}


async def test_poll_unknown_id_is_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/requests/nope", params={"timeout": 0}, headers=AUTH)
    assert resp.status_code == 404


async def test_decide_unknown_id_is_404(client: httpx.AsyncClient) -> None:
    resp = await client.post("/decide/nope/allow")
    assert resp.status_code == 404


async def test_decide_twice_is_409(client: httpx.AsyncClient) -> None:
    request_id = await create(client)
    assert (await client.post(f"/decide/{request_id}/allow")).status_code == 200
    assert (await client.post(f"/decide/{request_id}/deny")).status_code == 409


async def test_decide_invalid_value_is_400(client: httpx.AsyncClient) -> None:
    request_id = await create(client)
    resp = await client.post(f"/decide/{request_id}/maybe")
    assert resp.status_code == 400


async def test_rate_limit() -> None:
    app = create_app(settings=make_settings(rate_limit_requests=2), notifier=FakeNotifier())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            codes = [(await c.post("/requests", json=BODY, headers=AUTH)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_store_ttl_expiry() -> None:
    clock = FakeClock()
    store = RequestStore(ttl_seconds=300, clock=clock)
    item = store.create("Bash")
    clock.now += 299
    assert store.get(item.id) is item
    clock.now += 2
    with pytest.raises(NotFoundError):
        store.get(item.id)
    with pytest.raises(NotFoundError):
        store.decide(item.id, Decision.ALLOW)
    assert store.purge_expired() == 1
    assert len(store) == 0


async def test_expired_request_is_404_over_http(client: httpx.AsyncClient) -> None:
    clock = FakeClock()
    client.app.state.store = RequestStore(ttl_seconds=300, clock=clock)  # type: ignore[attr-defined]
    request_id = await create(client)
    clock.now += 301
    assert (await client.post(f"/decide/{request_id}/allow")).status_code == 404
    assert (await client.get(f"/requests/{request_id}", headers=AUTH)).status_code == 404


async def test_ntfy_notifier_headers_and_body() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = NtfyNotifier(
        server="https://ntfy.example/",
        topic="secret-topic",
        public_url="https://relay.example/",
        token="tok123",
        client=mock_client,
    )
    await notifier.send("abc", "Bash", "x" * 2000)
    await notifier.aclose()

    req = captured[0]
    assert str(req.url) == "https://ntfy.example/secret-topic"
    assert req.headers["Title"] == "Claude Code: Bash"
    assert req.headers["Priority"] == "high"
    assert req.headers["Authorization"] == "Bearer tok123"
    assert req.headers["Actions"] == (
        "http, Approve, https://relay.example/decide/abc/allow, method=POST, clear=true; "
        "http, Deny, https://relay.example/decide/abc/deny, method=POST, clear=true"
    )
    assert len(req.content.decode("utf-8")) == 1000


def test_ntfy_notifier_omits_auth_without_token() -> None:
    notifier = NtfyNotifier(server="https://ntfy.sh", topic="t", public_url="https://r")
    assert "Authorization" not in notifier.build_headers("abc", "Read")
