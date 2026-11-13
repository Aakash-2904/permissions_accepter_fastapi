# Phone Approval Relay for Claude Code

Control Claude Code from your phone:

1. **Approve / Deny** Claude Code permission prompts with buttons in a push notification.
2. **Phone console:** send prompts (or shell commands) from your phone to the machine running Claude Code, and get the output back as notifications.

Both use the free [ntfy](https://ntfy.sh) app for push notifications. No Claude Remote Control access is needed.

```
APPROVALS
Claude Code ── PermissionRequest hook (hook/phone_approve.py)
                 ├─ POST /requests ──────► FastAPI relay ──► ntfy ──► phone [Approve] [Deny]
                 │                              ◄── POST /decide/{id}/{allow|deny} ──┘
                 └─ GET /requests/{id} (long-poll, outbound HTTPS only) ◄── decision

PHONE CONSOLE
phone ──(type in ntfy topic)──► ntfy ──► console/phone_console.py ──► claude -p / PowerShell
phone ◄──────(reply in the same topic)─────────────────┘
```

- The hook makes **outbound requests only**. If anything fails, it prints nothing and Claude Code shows its normal terminal prompt. **It never auto-approves.**
- The phone console also only makes outbound connections (to ntfy). It doesn't need the relay to be public.

---

## Contents

1. [Requirements](#1-requirements)
2. [Install](#2-install)
3. [Create your secrets and `.env`](#3-create-your-secrets-and-env)
4. [Run the relay and make it reachable from your phone](#4-run-the-relay-and-make-it-reachable-from-your-phone)
5. [Set up the phone (ntfy app)](#5-set-up-the-phone-ntfy-app)
6. [Install the Claude Code hook](#6-install-the-claude-code-hook)
7. [Test approvals end to end](#7-test-approvals-end-to-end)
8. [Phone console](#8-phone-console)
9. [Start everything with one command](#9-start-everything-with-one-command)
10. [Troubleshooting](#10-troubleshooting)
11. [Security notes](#11-security-notes)
12. [Reference: API, configuration, hook contract](#12-reference)
13. [Known limitations](#13-known-limitations)

---

## 1. Requirements

| What | Why |
|---|---|
| Python **3.11+** | relay, hook and console |
| [Claude Code](https://code.claude.com) installed and logged in | the thing being controlled (`claude` must be on `PATH` for the console) |
| ntfy app on your phone ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/app/ntfy/id1625396347)) | receiving notifications and sending console messages |
| An HTTPS URL for the relay: [ngrok](https://ngrok.com) (free static domain), Cloudflare Tunnel, Render or Fly.io | so the phone's Approve/Deny tap can reach the relay |

The instructions below use **Windows + PowerShell**. Commands for bash/macOS/Linux are shown where they differ.

## 2. Install

```powershell
git clone https://github.com/Aakash-2904/permissions_accepter_fastapi.git
cd permissions_accepter_fastapi

py -3.11 -m venv .venv                        # macOS/Linux: python3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1                  # macOS/Linux: source .venv/bin/activate
pip install -r relay/requirements.txt
pip install pytest pytest-asyncio ruff        # optional: for tests / lint
```

The hook (`hook/phone_approve.py`) uses only the standard library. The console reuses the relay's packages (`httpx`, `pydantic-settings`).

## 3. Create your secrets and `.env`

```powershell
copy .env.example .env                         # macOS/Linux: cp .env.example .env
```

Generate three random values and paste them into `.env`:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"                 # -> RELAY_API_KEY
python -c "import secrets; print('cc-' + secrets.token_urlsafe(24))"         # -> NTFY_TOPIC    (approvals)
python -c "import secrets; print('cc-console-' + secrets.token_urlsafe(24))" # -> CONSOLE_TOPIC (phone console)
```

> **Treat both topic names like passwords.** On ntfy.sh anyone who knows a topic name can read it and publish to it.
> Someone with `CONSOLE_TOPIC` can **run commands on your machine**. Never commit `.env` (it's in `.gitignore`).

The values you must set (everything else has sensible defaults; see [Configuration](#configuration)):

| Variable | Example | Notes |
|---|---|---|
| `RELAY_API_KEY` | `Xy3...` (32+ chars) | shared by relay and hook |
| `NTFY_TOPIC` | `cc-AbC...` | approvals topic |
| `PUBLIC_URL` | `https://your-name.ngrok-free.dev` | the relay's public HTTPS URL (step 4) |
| `RELAY_URL` | same as `PUBLIC_URL` | what the hook calls |
| `NGROK_DOMAIN` | `your-name.ngrok-free.dev` | optional, used by `scripts/start_all.ps1` |
| `CONSOLE_TOPIC` | `cc-console-AbC...` | phone console topic |
| `CONSOLE_WORKDIR` | `C:\Users\you\Desktop` | folder the console starts in |
| `CONSOLE_ALLOW_SHELL` | `true` / `false` | allow `!command` shell messages |

> On Windows PowerShell 5.1, don't write `.env` with `Set-Content -Encoding utf8`, because it adds a BOM that breaks the first line. Edit it in a text editor, or use `Out-File -Encoding ascii`.

## 4. Run the relay and make it reachable from your phone

Start the relay (**exactly one worker**, because pending requests live in memory):

```powershell
.\.venv\Scripts\python.exe -m uvicorn relay.app:app --host 127.0.0.1 --port 8000 --workers 1
curl.exe http://127.0.0.1:8000/health          # {"status":"ok"}
```

Your phone has to reach it over **HTTPS**. Pick one option:

**Option A: ngrok (simplest, free static domain)**
1. Sign up at ngrok.com, run `ngrok config add-authtoken <token>`.
2. In the ngrok dashboard → *Domains*, claim your free static domain (e.g. `your-name.ngrok-free.dev`).
3. Run `ngrok http --url=your-name.ngrok-free.dev 8000`.
4. In `.env` set `PUBLIC_URL=https://your-name.ngrok-free.dev`, `RELAY_URL=` the same, and `NGROK_DOMAIN=your-name.ngrok-free.dev`. Restart the relay.

**Option B: Cloudflare quick tunnel:** `cloudflared tunnel --url http://localhost:8000`, then put the printed `https://...trycloudflare.com` URL in `PUBLIC_URL` / `RELAY_URL`. The URL changes every restart.

**Option C: deploy (always on)**
- **Render:** New → Web Service → this repo → Runtime *Docker*. Add the env vars from `.env.example`; `PUBLIC_URL=https://<service>.onrender.com`. The free tier sleeps when idle, so the first request can be slow.
- **Fly.io:**
  ```bash
  fly launch --no-deploy            # uses the Dockerfile; internal_port = 8000
  fly secrets set RELAY_API_KEY=... NTFY_TOPIC=... PUBLIC_URL=https://<app>.fly.dev
  fly scale count 1                 # single instance only
  fly deploy
  ```

Check it from outside: open `https://<your-url>/health` in the phone's browser. It should show `{"status":"ok"}`.

## 5. Set up the phone (ntfy app)

1. Install **ntfy** and open it.
2. Tap **+** → *Subscribe to topic* → enter your **`NTFY_TOPIC`** (server `https://ntfy.sh`, or your own). Approve/Deny notifications arrive here.
3. Tap **+** again → subscribe to your **`CONSOLE_TOPIC`**. You'll type prompts here and get replies (section 8).
4. Android: allow notifications for ntfy and turn off battery optimisation for it, or notifications can arrive late.

Optional hardening: self-host ntfy (`NTFY_SERVER`) or use a reserved topic with access control and set `NTFY_TOKEN`.

## 6. Install the Claude Code hook

1. Copy the hook script into your Claude config folder:
   ```powershell
   mkdir $HOME\.claude\hooks -Force
   copy hook\phone_approve.py $HOME\.claude\hooks\phone_approve.py
   ```
   (macOS/Linux: `mkdir -p ~/.claude/hooks && cp hook/phone_approve.py ~/.claude/hooks/`)

2. **Back up** your settings, then open `~/.claude/settings.json` (`C:\Users\<you>\.claude\settings.json`) and merge in the following. Keep anything that's already there:
   ```json
   {
     "env": {
       "RELAY_URL": "https://your-name.ngrok-free.dev",
       "RELAY_API_KEY": "<same value as in .env>",
       "WAIT_SECONDS": "240"
     },
     "hooks": {
       "PermissionRequest": [
         {
           "matcher": "*",
           "hooks": [
             {
               "type": "command",
               "command": "python \"C:/Users/<you>/.claude/hooks/phone_approve.py\"",
               "timeout": 300
             }
           ]
         }
       ]
     }
   }
   ```
   - On macOS/Linux the command can be `python3 ~/.claude/hooks/phone_approve.py` (see `examples/settings.json`).
   - The hook `timeout` (300) must be **larger** than `WAIT_SECONDS` + about 20 s.
   - Do **not** add `onFailure: "block"`, because that turns every relay failure into a denial.

3. Restart Claude Code so it picks up the new settings.

## 7. Test approvals end to end

With the relay and tunnel running:

```bash
./scripts/simulate_hook.sh        # Git Bash / macOS / Linux; reads .env
```

Your phone should buzz. Tap **Approve** or **Deny**, and the script prints the decision JSON. Then try it for real: in Claude Code, ask for something that needs permission (e.g. "create test.txt with hello"). The approval should arrive on your phone.

If nothing arrives, the terminal prompt appears after `WAIT_SECONDS`. That's the fail-safe; see [Troubleshooting](#10-troubleshooting).

## 8. Phone console

`console/phone_console.py` listens on `CONSOLE_TOPIC`. Whatever you type into that topic in the ntfy app runs on the machine, and the reply is posted back into the same topic, so it reads like a chat.

Start it (from the repo folder):

```powershell
.\.venv\Scripts\python.exe -m console.phone_console
```

It posts **"phone console started"** to the topic when it's ready. Then type in the topic:

| You send | What happens |
|---|---|
| any text, e.g. `what changed in this repo today?` | runs `claude -p` in the current folder; follow-ups continue the same conversation |
| `!git status` | runs the command in PowerShell (`sh` elsewhere) and sends the output; needs `CONSOLE_ALLOW_SHELL=true` |
| `/cd C:\path\to\project` | change folder (also starts a fresh conversation) |
| `/new` | start a fresh Claude conversation |
| `/stop` | kill the running job |
| `/status` | show folder, session, running job, queue |
| `/help` | list commands |

How it behaves:
- **Replies:** you get "running..." first, then Claude's answer. Output over about 3.5 KB is cut, and the full text is attached as `output.txt`.
- **Footer** (`- 2 turns, $0.459`): how many tool rounds Claude took, and what the run **would cost at API prices**. On a claude.ai subscription you aren't billed per message; it shows how much of your usage limit it took.
- **Permissions:** tools the remote Claude wants to use go through the approval hook. They show up in the approvals topic as **"Claude Code: [phone console] Bash"**, so you can tell them from approvals for your own terminal session. Unanswered approvals are **denied** after `WAIT_SECONDS`, because headless mode has no terminal prompt to fall back to.
- **Tip:** `/cd` into the project first, so Claude doesn't have to search for it (each search needs an approval). Use `!` commands for quick checks that don't need Claude.
- Jobs run one at a time. Extra messages are queued, and each job is killed after `CONSOLE_TIMEOUT_SECONDS` (default 900).
- To run Claude without approvals, set `CONSOLE_PERMISSION_MODE=acceptEdits` (auto-accepts file edits only). Only do this if you trust what you'll send.

## 9. Start everything with one command

Windows: starts the relay, ngrok (using `NGROK_DOMAIN` if set) and the phone console as hidden background processes. It first stops old copies.

```powershell
powershell -File scripts\start_all.ps1
```

They stop when the machine reboots or sleeps, so run it again afterwards. Logs: `relay_*.log`, `ngrok_*.log`, `console_*.log` in the repo folder (gitignored).

To start it automatically at login, create a Task Scheduler task: *At log on* → `powershell.exe -WindowStyle Hidden -File "C:\path\to\repo\scripts\start_all.ps1"`.

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| No approval notification | Is the phone subscribed to the exact `NTFY_TOPIC`? Is `https://<url>/health` OK? Run the hook with `PHONE_APPROVE_DEBUG=1` and read stderr. A relay 502 means ntfy rejected the push (check `NTFY_TOKEN`); a 429 means the rate limit was hit. |
| Tapping Approve does nothing | The phone can't reach `PUBLIC_URL` (it's localhost, not HTTPS, or the tunnel is down). Or the request expired (`REQUEST_TTL_SECONDS`) or was already decided (409). |
| Terminal prompt still appears | Fail-safe kicked in: relay or tunnel down, wrong `RELAY_API_KEY` (401), `RELAY_URL` not https, env vars not visible to Claude Code, or no tap within `WAIT_SECONDS`. |
| Prompts stall for about 4 minutes | Relay/ngrok aren't running (e.g. after sleep), so the hook waits then falls back. Run `scripts\start_all.ps1`. |
| Console never replies | Is `console.phone_console` running (check `console_err.log`)? Is the phone subscribed to `CONSOLE_TOPIC` (not the approvals topic)? It may be waiting on an approval: look in the approvals topic for "[phone console]". |
| Console says `claude` not found | Make sure `claude` is on `PATH` for the process that starts the console. |
| Tool still blocked after Approve | A matching deny rule in your settings still applies. Hook approval doesn't override deny rules. |

## 11. Security notes

- **Topics are credentials.** `NTFY_TOPIC` lets someone see and answer your approvals; `CONSOLE_TOPIC` lets someone **run Claude and shell commands** on your machine. Use long random names, keep them out of git, and rotate them if they leak (change `.env`, restart, re-subscribe).
- HTTPS only in production. The hook refuses plain `http://` except for localhost. The API key is compared with `secrets.compare_digest`.
- Request IDs come from `secrets.token_urlsafe(32)` and act as capability URLs for the phone buttons.
- `/requests` is rate-limited (`RATE_LIMIT_REQUESTS` per `RATE_LIMIT_WINDOW_SECONDS`).
- The hook **redacts** likely secrets (API keys, tokens, bearer/basic auth, URL credentials, `KEY=value` pairs, long opaque strings) before sending a summary. The relay logs only the tool name, a 6-character ID prefix and the decision.
- Console replies contain real command output, and that output passes through ntfy. Don't use it to print secrets.

## 12. Reference

### API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/requests` | `x-api-key` | Create a request and send a push. Body `{tool_name, summary, session_id?, cwd?}` → `{id}`. Rate-limited (429). |
| GET | `/requests/{id}?timeout=25` | `x-api-key` | Long-poll → `{decision: "allow" \| "deny" \| null}`. 404 if unknown or expired. |
| POST | `/decide/{id}/{allow\|deny}` | unguessable id | Phone button. 400 bad value, 404 unknown, 409 already decided. |
| GET | `/health` | none | Liveness check. |

### Configuration

All of these come from `.env` (see `.env.example`).

| Variable | Default | Used by |
|---|---|---|
| `RELAY_API_KEY` | required | relay, hook |
| `PUBLIC_URL` | `http://localhost:8000` | relay (button URLs) |
| `NTFY_SERVER` | `https://ntfy.sh` | relay, console |
| `NTFY_TOPIC` | required | relay |
| `NTFY_TOKEN` | empty | relay, console |
| `REQUEST_TTL_SECONDS` | 300 | relay |
| `CLEANUP_INTERVAL_SECONDS` | 30 | relay |
| `MAX_POLL_TIMEOUT_SECONDS` | 60 | relay |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | 20 / 60 | relay |
| `RELAY_URL` | required | hook |
| `WAIT_SECONDS` | 240 | hook |
| `PHONE_APPROVE_DEBUG` | 0 | hook |
| `NGROK_DOMAIN` | empty | start script |
| `CONSOLE_TOPIC` | required | console |
| `CONSOLE_WORKDIR` | `~/Desktop` | console |
| `CONSOLE_ALLOW_SHELL` | false | console |
| `CONSOLE_TIMEOUT_SECONDS` | 900 | console |
| `CONSOLE_PERMISSION_MODE` | `default` | console (`claude --permission-mode`) |

The hook reads its variables from the environment Claude Code passes it, i.e. the `"env"` block in `settings.json`, not from `.env`.

### Hook contract (checked against the [hooks docs](https://code.claude.com/docs/en/hooks))

- Output: `{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}`. `message` is sent on deny only (`"Denied from phone"`).
- Stdin includes `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `tool_name` and `tool_input`. The hook uses only what it needs.
- Exit code 2 is not honored for PermissionRequest. The failure path here is "no output, exit 0".
- `timeout` is in seconds. If it's exceeded, Claude Code discards the hook output and shows the normal prompt.
- In `claude -p` (headless) mode, no decision means the tool call is **denied**.
- This project uses a `command` hook, because `http` hooks fail open on connection errors.

### Project layout

```
relay/        FastAPI relay (app, config, ntfy notifier, in-memory store)
hook/         phone_approve.py: the Claude Code PermissionRequest hook (stdlib only)
console/      phone_console.py: phone → claude -p / shell → phone
scripts/      start_all.ps1 (start everything), simulate_hook.sh (manual test)
examples/     settings.json snippet
tests/        pytest suite (pytest -q)
Dockerfile    relay container for Render / Fly.io
```

Run the checks with `pytest -q` and `ruff check .`.

## 13. Known limitations

- In-memory state: restarting the relay drops pending requests (the hook falls back to the terminal prompt).
- Single worker / single instance only.
- Redaction is pattern-based and best-effort.
- The console runs a separate Claude conversation. It can't type into a terminal session that's already open.
- Background processes stop on sleep/reboot unless you set up Task Scheduler (section 9).
