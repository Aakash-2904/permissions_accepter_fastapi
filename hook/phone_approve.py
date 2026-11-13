#!/usr/bin/env python3
"""Claude Code PermissionRequest hook: ask the phone relay for a decision.

Standard library only. Contract:
  * stdout carries ONLY the decision JSON, printed once a decision arrives.
  * On any error, timeout, or unreachable relay: print nothing, exit 0, so
    Claude Code falls back to its normal terminal prompt. Never auto-approve.

Env vars: RELAY_URL, RELAY_API_KEY, WAIT_SECONDS (default 240),
PHONE_APPROVE_DEBUG=1 for debug logs on stderr.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("phone_approve")

DEFAULT_WAIT_SECONDS = 240.0
MAX_POLL_SECONDS = 25.0
HTTP_SLACK_SECONDS = 10.0
GENERIC_SUMMARY_CHARS = 500
SHELL_TOOLS = {"Bash", "PowerShell"}
PATH_TOOLS = {"Edit": "file_path", "Write": "file_path", "Read": "file_path", "NotebookEdit": "notebook_path"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

MASK = "***"
_SECRET_WORDS = r"(?:api[_-]?key|token|secret|passw(?:or)?d|pwd|auth|credential|private[_-]?key)"  # noqa: S105
REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # Well-known token formats.
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), MASK),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), MASK),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), MASK),  # JWT
    # Authorization headers / bearer tokens.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]+"), rf"\1 {MASK}"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)\S+"), rf"\1{MASK}"),
    # Credentials embedded in URLs: scheme://user:pass@host
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), rf"\1{MASK}@"),
    # .env-style KEY=value (any UPPER_SNAKE name).
    (re.compile(r"\b([A-Z][A-Z0-9_]*)=(\"[^\"]*\"|'[^']*'|\S+)"), rf"\1={MASK}"),
    # key: value / "key": "value" / key=value / --password value, for secret-ish key names.
    (re.compile(rf"(?i)([\"']?\w*{_SECRET_WORDS}\w*[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}}]+)"), rf"\1{MASK}"),
    (re.compile(rf"(?i)(--?\w*{_SECRET_WORDS}\w*[\s=]+)(\S+)"), rf"\1{MASK}"),
    # Long opaque strings mixing letters and digits (likely keys/hashes).
    (re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}\b"), MASK),
]


class HookError(Exception):
    """Any condition where we must fall back to the terminal prompt."""


def redact(text: str) -> str:
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_summary(tool_name: str, tool_input: dict[str, Any]) -> str:
    if tool_name in SHELL_TOOLS and isinstance(tool_input.get("command"), str):
        return redact(tool_input["command"])
    path_key = PATH_TOOLS.get(tool_name)
    if path_key and isinstance(tool_input.get(path_key), str):
        return redact(tool_input[path_key])
    compact = json.dumps(tool_input, separators=(",", ":"), ensure_ascii=False, default=str)
    return truncate(redact(compact), GENERIC_SUMMARY_CHARS)


def decision_output(decision: str) -> dict[str, Any]:
    body: dict[str, Any] = {"behavior": decision}
    if decision == "deny":
        body["message"] = "Denied from phone"
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": body}}


class RelayClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS):
            raise HookError("RELAY_URL must be https (plain http allowed only for localhost)")
        self._base = base_url.rstrip("/")
        self._api_key = api_key

    def _call(self, method: str, path: str, timeout: float, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self._base + path, data=data, method=method)
        req.add_header("x-api-key", self._api_key)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise HookError(f"{method} {path.split('?')[0][:16]}: {type(exc).__name__}: {exc}") from exc

    def create(self, payload: dict[str, Any]) -> str:
        body = self._call("POST", "/requests", HTTP_SLACK_SECONDS, payload)
        request_id = body.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise HookError("relay returned no id")
        return request_id

    def poll(self, request_id: str, wait: float) -> str | None:
        path = f"/requests/{urllib.parse.quote(request_id)}?timeout={wait:.0f}"
        body = self._call("GET", path, wait + HTTP_SLACK_SECONDS)
        decision = body.get("decision")
        if decision is None:
            return None
        if decision not in ("allow", "deny"):
            raise HookError("relay returned an invalid decision")
        return decision


def wait_for_decision(client: RelayClient, request_id: str, wait_seconds: float) -> str | None:
    deadline = time.monotonic() + wait_seconds
    while (remaining := deadline - time.monotonic()) > 0:
        decision = client.poll(request_id, max(1.0, min(MAX_POLL_SECONDS, remaining)))
        if decision is not None:
            return decision
    return None


def run(stdin_text: str, env: dict[str, str]) -> dict[str, Any] | None:
    """Return the decision JSON, or None to fall back to the terminal prompt."""
    relay_url, api_key = env.get("RELAY_URL"), env.get("RELAY_API_KEY")
    if not relay_url or not api_key:
        raise HookError("RELAY_URL and RELAY_API_KEY must be set")
    try:
        wait_seconds = float(env.get("WAIT_SECONDS", DEFAULT_WAIT_SECONDS))
    except ValueError as exc:
        raise HookError("WAIT_SECONDS must be a number") from exc

    event = json.loads(stdin_text)
    tool_name = str(event.get("tool_name") or "unknown")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}

    client = RelayClient(relay_url, api_key)
    request_id = client.create(
        {
            "tool_name": (("[phone console] " if env.get("PHONE_CONSOLE") == "1" else "") + tool_name)[:200],
            "summary": build_summary(tool_name, tool_input),
            "session_id": event.get("session_id"),
            "cwd": redact(str(event["cwd"]))[:1000] if event.get("cwd") else None,
        }
    )
    log.debug("created request %s for %s", request_id[:6], tool_name)
    decision = wait_for_decision(client, request_id, wait_seconds)
    log.debug("decision for %s: %s", request_id[:6], decision)
    return decision_output(decision) if decision else None


def main() -> int:
    debug = os.environ.get("PHONE_APPROVE_DEBUG") == "1"
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if debug else logging.WARNING)
    try:
        result = run(sys.stdin.read(), dict(os.environ))
    except Exception as exc:  # noqa: BLE001 - fail safe: fall back to the terminal prompt
        log.warning("phone approval unavailable, falling back to terminal prompt: %s", exc)
        return 0
    if result is not None:
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
