"""Phone console: type prompts/commands into a private ntfy topic, get the output back.

Run:  .venv\\Scripts\\python.exe -m console.phone_console   (cwd = project root)

Messages you publish to CONSOLE_TOPIC from the ntfy app:
  <text>          -> sent to Claude Code headless (`claude -p`), conversation continues
  !<command>      -> run in PowerShell (only if CONSOLE_ALLOW_SHELL=true)
  /new            -> start a fresh Claude conversation
  /cd <path>      -> change working directory (also starts a fresh conversation)
  /stop           -> kill the running job
  /status, /help

Replies are posted back to the same topic tagged `cc_out` (and ignored by the listener).
The topic name is the credential: anyone who knows it can run Claude on this machine.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("phone_console")

OUT_TAG = "cc_out"
PREVIEW_BYTES = 3500
STATE_FILE = Path("console_state.json")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HELP = (
    "<text>  -> Claude (continues conversation)\n"
    "!<cmd>  -> PowerShell (if enabled)\n"
    "/new  /cd <path>  /stop  /status  /help"
)


class ConsoleSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ntfy_server: str = "https://ntfy.sh"
    ntfy_token: str | None = None
    console_topic: str = Field(min_length=16)
    console_workdir: str = str(Path.home() / "Desktop")
    console_allow_shell: bool = False
    console_timeout_seconds: int = Field(default=900, gt=0)
    # "default" = tool permissions go through the phone approval hook; or acceptEdits / plan / etc.
    console_permission_mode: str = "default"


@dataclass
class State:
    workdir: str
    session_id: str | None = None

    @classmethod
    def load(cls, default_workdir: str) -> State:
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return cls(workdir=data.get("workdir") or default_workdir, session_id=data.get("session_id"))
        except (OSError, ValueError):
            return cls(workdir=default_workdir)

    def save(self) -> None:
        STATE_FILE.write_text(json.dumps({"workdir": self.workdir, "session_id": self.session_id}), encoding="utf-8")


class Ntfy:
    def __init__(self, server: str, topic: str, token: str | None) -> None:
        self.url = f"{server.rstrip('/')}/{topic}"
        self.auth = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(timeout=20)

    def publish(self, text: str, title: str, tags: str = "") -> None:
        body = text.encode("utf-8") or b"(empty)"
        headers = {
            **self.auth,
            "Title": title.encode("ascii", "replace").decode(),
            "Tags": f"{OUT_TAG},{tags}".strip(","),
        }
        try:
            if len(body) <= PREVIEW_BYTES:
                self.client.post(self.url, content=body, headers=headers).raise_for_status()
                return
            preview = body[:PREVIEW_BYTES].decode("utf-8", "ignore") + "\n... (truncated, full output attached)"
            self.client.post(self.url, content=preview.encode("utf-8"), headers=headers).raise_for_status()
            self.client.put(
                self.url, content=body, headers={**headers, "Filename": "output.txt", "Title": "full output"}
            ).raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("publish failed: %s", exc)

    def listen(self, on_message) -> None:
        """Stream messages forever, reconnecting with since=<last id> so nothing is lost or repeated."""
        since: str | None = None
        backoff = 2.0
        while True:
            params = {"since": since} if since else {}
            try:
                with httpx.stream(
                    "GET", self.url + "/json", params=params, headers=self.auth, timeout=httpx.Timeout(10, read=90)
                ) as r:
                    r.raise_for_status()
                    log.info("listening on topic")
                    backoff = 2.0
                    for line in r.iter_lines():
                        if not line:
                            continue
                        event = json.loads(line)
                        if event.get("event") != "message":
                            continue
                        since = event.get("id") or since
                        if OUT_TAG in (event.get("tags") or []):
                            continue
                        on_message((event.get("message") or "").strip())
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("stream dropped (%s); reconnecting in %.0fs", type(exc).__name__, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                since = since or "30s"


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)  # noqa: S603,S607
    else:
        proc.kill()


class Console:
    def __init__(self, cfg: ConsoleSettings, ntfy: Ntfy) -> None:
        self.cfg = cfg
        self.ntfy = ntfy
        self.state = State.load(cfg.console_workdir)
        self.jobs: queue.Queue[str] = queue.Queue()
        self.current: subprocess.Popen | None = None
        self.current_label = ""
        self.lock = threading.Lock()
        self.claude = shutil.which("claude")

    # ---- listener side (fast; never blocks on a job) ----
    def on_message(self, text: str) -> None:
        if not text:
            return
        log.info("received %s", text[:40].replace("\n", " "))
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower()
        if cmd == "/help":
            self.ntfy.publish(HELP, "help")
        elif cmd == "/status":
            running = self.current_label or "idle"
            self.ntfy.publish(
                f"cwd: {self.state.workdir}\nsession: {self.state.session_id or 'new'}\n"
                f"running: {running}\nqueued: {self.jobs.qsize()}\nshell: {self.cfg.console_allow_shell}",
                "status",
            )
        elif cmd == "/stop":
            with self.lock:
                proc = self.current
            if proc and proc.poll() is None:
                kill_tree(proc)
                self.ntfy.publish("killed running job", "stopped", "stop_sign")
            else:
                self.ntfy.publish("nothing running", "stopped")
        elif cmd == "/new":
            self.state.session_id = None
            self.state.save()
            self.ntfy.publish("next prompt starts a fresh conversation", "new session")
        elif cmd == "/cd":
            path = Path(os.path.expandvars(os.path.expanduser(arg.strip().strip('"'))))
            if not path.is_absolute():
                path = Path(self.state.workdir) / path
            if path.is_dir():
                self.state.workdir, self.state.session_id = str(path.resolve()), None
                self.state.save()
                self.ntfy.publish(f"cwd: {self.state.workdir}\n(fresh conversation)", "cd")
            else:
                self.ntfy.publish(f"not a directory: {path}", "cd failed", "warning")
        else:
            self.jobs.put(text)
            if self.current_label or self.jobs.qsize() > 1:
                self.ntfy.publish(f"queued ({self.jobs.qsize()} waiting)", "queued", "hourglass")

    # ---- worker side ----
    def worker(self) -> None:
        while True:
            text = self.jobs.get()
            try:
                if text.startswith("!"):
                    self.run_shell(text[1:].strip())
                else:
                    self.run_claude(text)
            except Exception as exc:  # noqa: BLE001 - report any failure to the phone, keep running
                log.exception("job failed")
                self.ntfy.publish(f"{type(exc).__name__}: {exc}", "job error", "warning")
            finally:
                with self.lock:
                    self.current, self.current_label = None, ""

    def _run(self, args: list[str], stdin_text: str | None, label: str) -> tuple[int | None, str, str]:
        proc = subprocess.Popen(  # noqa: S603 - args are built here, prompt goes via stdin
            args,
            cwd=self.state.workdir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            env={**os.environ, "PHONE_CONSOLE": "1"},
        )
        with self.lock:
            self.current, self.current_label = proc, label
        try:
            out, err = proc.communicate(stdin_text, timeout=self.cfg.console_timeout_seconds)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            out, err = proc.communicate()
            return None, out, err
        return proc.returncode, out, err

    def run_shell(self, command: str) -> None:
        if not self.cfg.console_allow_shell:
            self.ntfy.publish("shell commands are disabled (CONSOLE_ALLOW_SHELL=false)", "shell disabled", "no_entry")
            return
        if not command:
            return
        args = (
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
            if os.name == "nt"
            else ["sh", "-c", command]
        )
        code, out, err = self._run(args, None, f"shell: {command[:40]}")
        text = (out + (f"\n[stderr]\n{err}" if err.strip() else "")).strip() or "(no output)"
        title = "timed out" if code is None else f"$ exit {code}"
        self.ntfy.publish(text, f"{title}: {command[:60]}", "computer")

    def run_claude(self, prompt: str) -> None:
        if not self.claude:
            self.ntfy.publish("`claude` not found on PATH", "error", "warning")
            return
        self.ntfy.publish(f"cwd: {self.state.workdir}", "running...", "hourglass_flowing_sand")
        args = [self.claude, "-p", "--output-format", "json", "--permission-mode", self.cfg.console_permission_mode]
        if self.state.session_id:
            args += ["--resume", self.state.session_id]
        code, out, err = self._run(args, prompt, f"claude: {prompt[:40]}")
        if code is None:
            self.ntfy.publish(
                f"no reply within {self.cfg.console_timeout_seconds}s; job killed", "timed out", "warning"
            )
            return
        try:
            data = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            self.ntfy.publish((out + "\n" + err).strip() or f"exit {code}, no output", "claude error", "warning")
            return
        if data.get("session_id"):
            self.state.session_id = data["session_id"]
            self.state.save()
        result = str(data.get("result") or "").strip() or "(no text reply)"
        cost = data.get("total_cost_usd")
        footer = f"\n\n- {data.get('num_turns', '?')} turns" + (
            f", ${cost:.3f}" if isinstance(cost, int | float) else ""
        )
        title = "Claude (error)" if data.get("is_error") else "Claude"
        self.ntfy.publish(result + footer, title, "robot")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = ConsoleSettings()  # type: ignore[call-arg]
    ntfy = Ntfy(cfg.ntfy_server, cfg.console_topic, cfg.ntfy_token)
    console = Console(cfg, ntfy)
    threading.Thread(target=console.worker, daemon=True).start()
    ntfy.publish(f"online\ncwd: {console.state.workdir}\n\n{HELP}", "phone console started", "green_circle")
    ntfy.listen(console.on_message)


if __name__ == "__main__":
    main()
