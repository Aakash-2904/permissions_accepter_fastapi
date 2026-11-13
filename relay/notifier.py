"""Push-notification backends. Add new ones (e.g. Telegram) by implementing Notifier."""

from typing import Protocol

import httpx

MAX_BODY_CHARS = 1000


class Notifier(Protocol):
    async def send(self, request_id: str, tool_name: str, summary: str) -> None: ...


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class NtfyNotifier:
    def __init__(
        self,
        server: str,
        topic: str,
        public_url: str,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = f"{server.rstrip('/')}/{topic}"
        self._public_url = public_url.rstrip("/")
        self._token = token
        self._client = client or httpx.AsyncClient(timeout=10)

    def build_headers(self, request_id: str, tool_name: str) -> dict[str, str]:
        base = f"{self._public_url}/decide/{request_id}"
        actions = (
            f"http, Approve, {base}/allow, method=POST, clear=true; http, Deny, {base}/deny, method=POST, clear=true"
        )
        headers = {
            "Title": f"Claude Code: {tool_name}",
            "Priority": "high",
            "Actions": actions,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def send(self, request_id: str, tool_name: str, summary: str) -> None:
        response = await self._client.post(
            self._url,
            content=truncate(summary, MAX_BODY_CHARS).encode("utf-8"),
            headers=self.build_headers(request_id, tool_name),
        )
        response.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()
