"""Hook tests against a mocked relay on 127.0.0.1. No external network calls."""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

HOOK_PATH = Path(__file__).resolve().parents[1] / "hook" / "phone_approve.py"
_spec = importlib.util.spec_from_file_location("phone_approve", HOOK_PATH)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

API_KEY = "hook-test-key-0123456789"
PAYLOAD = {
    "session_id": "abc123",
    "cwd": "/home/me/project",
    "permission_mode": "default",
    "hook_event_name": "PermissionRequest",
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf node_modules", "description": "Remove node_modules"},
}


class MockRelay:
    def __init__(self, decision: str | None) -> None:
        self.decision = decision
        self.created: list[dict[str, Any]] = []
        self.api_keys: list[str | None] = []
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def _send(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802
                relay.api_keys.append(self.headers.get("x-api-key"))
                length = int(self.headers.get("Content-Length", 0))
                relay.created.append(json.loads(self.rfile.read(length)))
                self._send(200, {"id": "req-id-123"})

            def do_GET(self) -> None:  # noqa: N802
                relay.api_keys.append(self.headers.get("x-api-key"))
                self._send(200, {"decision": relay.decision})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def relay_factory() -> Iterator[Any]:
    relays: list[MockRelay] = []

    def make(decision: str | None) -> MockRelay:
        relays.append(MockRelay(decision))
        return relays[-1]

    yield make
    for r in relays:
        r.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_hook(
    relay_url: str, wait_seconds: str = "5", payload: dict[str, Any] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        "RELAY_URL": relay_url,
        "RELAY_API_KEY": API_KEY,
        "WAIT_SECONDS": wait_seconds,
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),  # Windows sockets need it
    }
    return subprocess.run(  # noqa: S603
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload or PAYLOAD),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def test_allow_path(relay_factory: Any) -> None:
    relay = relay_factory("allow")
    result = run_hook(relay.url)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}
    }
    assert relay.created[0]["tool_name"] == "Bash"
    assert relay.created[0]["summary"] == "rm -rf node_modules"
    assert set(relay.api_keys) == {API_KEY}


def test_deny_path(relay_factory: Any) -> None:
    relay = relay_factory("deny")
    result = run_hook(relay.url)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "deny", "message": "Denied from phone"},
        }
    }


def test_relay_down_prints_nothing() -> None:
    result = run_hook(f"http://127.0.0.1:{free_port()}")
    assert result.returncode == 0
    assert result.stdout == ""


def test_timeout_prints_nothing(relay_factory: Any) -> None:
    relay = relay_factory(None)
    result = run_hook(relay.url, wait_seconds="1")
    assert result.returncode == 0
    assert result.stdout == ""


def test_invalid_decision_prints_nothing(relay_factory: Any) -> None:
    relay = relay_factory("yes-please")
    result = run_hook(relay.url)
    assert result.returncode == 0
    assert result.stdout == ""


def test_bad_stdin_prints_nothing(relay_factory: Any) -> None:
    relay = relay_factory("allow")
    env = {"RELAY_URL": relay.url, "RELAY_API_KEY": API_KEY, "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(HOOK_PATH)], input="not json", capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert relay.created == []


def test_missing_env_raises() -> None:
    with pytest.raises(hook.HookError):
        hook.run(json.dumps(PAYLOAD), {})


def test_plain_http_rejected_for_remote_host() -> None:
    with pytest.raises(hook.HookError):
        hook.RelayClient("http://relay.example.com", API_KEY)
    hook.RelayClient("https://relay.example.com", API_KEY)
    hook.RelayClient("http://localhost:8000", API_KEY)


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "expected"),
    [
        ("Bash", {"command": "ls -la"}, "ls -la"),
        ("PowerShell", {"command": "Get-ChildItem"}, "Get-ChildItem"),
        ("Edit", {"file_path": "/src/app.py", "old_string": "a", "new_string": "b"}, "/src/app.py"),
        ("Write", {"file_path": "/src/new.py", "content": "print(1)"}, "/src/new.py"),
        ("Read", {"file_path": "/etc/hosts"}, "/etc/hosts"),
        ("WebFetch", {"url": "https://example.com"}, '{"url":"https://example.com"}'),
    ],
)
def test_build_summary(tool_name: str, tool_input: dict[str, Any], expected: str) -> None:
    assert hook.build_summary(tool_name, tool_input) == expected


def test_generic_summary_truncated() -> None:
    summary = hook.build_summary("mcp__x__y", {"q": "a b " * 400})
    assert len(summary) == 500


@pytest.mark.parametrize(
    ("raw", "leaked"),
    [
        ("export OPENAI_API_KEY=sk-proj-abcdefghijklmnop1234567890", "sk-proj-abcdefghijklmnop1234567890"),
        ("DATABASE_URL=postgres://user:hunter2@db/x", "hunter2"),
        ("curl https://admin:s3cr3tpw@host/api", "s3cr3tpw"),
        ('curl -H "Authorization: Bearer abc.def.ghi" https://x', "abc.def.ghi"),
        ("git push https://ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123@github.com", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"),
        ("aws configure set key AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("mysql --password hunter2 -u root", "hunter2"),
        ('{"password": "hunter2"}', "hunter2"),
        ("token: xoxb-123456789012-abcdefghij", "xoxb-123456789012-abcdefghij"),
        ("echo a8f5f167f44f4964e6c998dee827110c3e6f1a2b", "a8f5f167f44f4964e6c998dee827110c3e6f1a2b"),
    ],
)
def test_redaction(raw: str, leaked: str) -> None:
    redacted = hook.redact(raw)
    assert leaked not in redacted
    assert hook.MASK in redacted


def test_redaction_leaves_normal_commands_alone() -> None:
    for cmd in ["ls -la /tmp", "git status", "npm run build", "python -m pytest tests/"]:
        assert hook.redact(cmd) == cmd


def test_redaction_applied_before_sending(relay_factory: Any) -> None:
    relay = relay_factory("deny")
    payload = {**PAYLOAD, "tool_input": {"command": "API_TOKEN=supersecretvalue ./deploy.sh"}}
    run_hook(relay.url, payload=payload)
    assert "supersecretvalue" not in json.dumps(relay.created[0])
