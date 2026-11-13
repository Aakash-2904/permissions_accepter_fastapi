# Instructions for Claude Code: set up this project

> **For the human:** open Claude Code in this repo folder and say
> *"Follow instructions_for_claude_code_setup.md to set this up for me."*
> Claude will do the technical steps and stop at each point where it needs you.
> Budget about 15 minutes. You'll need your phone and a free ngrok account.

---

## Claude: read this first

You are setting up **Phone Approval Relay** for the person you're talking to. When you're done they'll have:
1. Claude Code permission prompts arriving on their phone with **Approve / Deny** buttons.
2. A **phone console**: messages they type into an ntfy topic run on this machine (`claude -p` or shell), and the replies come back to the phone.

`README.md` is the full reference. This file is the step-by-step procedure. Follow it **in order**.

### Rules (apply to every step)

- **Stop at every `🛑 STOP` marker.** Tell the person exactly what to do, then wait for them to confirm. Never pretend a phone step was done.
- **Generate new secrets for this person.** Never reuse topic names, keys or domains from anywhere else (examples, git history, other projects). Don't ask the person for someone else's topics.
- **Secrets stay local.** `.env` must never be committed, pasted into issues, or sent to any service except ntfy/ngrok as designed. Before any `git commit`, check that `git status` doesn't list `.env`.
- **Don't overwrite `~/.claude/settings.json`.** Back it up, read it, and *merge* the new keys. Show the person the diff and get their OK before writing.
- **Ask before enabling shell commands** in the phone console (`CONSOLE_ALLOW_SHELL`). Default to `false`.
- **Detect the OS** (Windows / macOS / Linux) and use matching commands. On Windows PowerShell 5.1, don't write `.env` with `Set-Content -Encoding utf8` (it adds a BOM). Use your file-writing tool, or `Out-File -Encoding ascii`.
- If a step fails, show the actual error, fix the cause, and retry that step. Don't skip ahead.
- Keep the person informed with one short line per step ("Step 4/10: starting the relay…").

---

## Step 1: Check prerequisites

Run and report:
- Python version: `py -3.11 --version` (Windows), or `python3 --version`. **3.11 or newer is required.** If it's missing, 🛑 STOP and ask the person to install Python 3.11+.
- `claude --version`: Claude Code must be on `PATH` (the phone console calls it).
- `ngrok version`: if it's missing, note it for Step 5 (don't stop yet).
- That you are in the repo root (`relay/`, `hook/`, `console/` exist).

## Step 2: Create the virtual environment and install

```powershell
py -3.11 -m venv .venv                          # macOS/Linux: python3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r relay/requirements.txt pytest pytest-asyncio
```
(macOS/Linux: use `.venv/bin/python`.)

Run the tests: `.venv\Scripts\python.exe -m pytest -q`. All should pass. If they don't, stop and fix the problem before continuing.

From here on, `PY` means the venv's Python (`.venv\Scripts\python.exe` or `.venv/bin/python`).

## Step 3: Generate secrets and write `.env`

1. Copy `.env.example` to `.env` (if `.env` already exists, ask before replacing it).
2. Generate three values with `PY`:
   ```
   python -c "import secrets; print(secrets.token_urlsafe(32))"                 # RELAY_API_KEY
   python -c "import secrets; print('cc-' + secrets.token_urlsafe(24))"         # NTFY_TOPIC
   python -c "import secrets; print('cc-console-' + secrets.token_urlsafe(24))" # CONSOLE_TOPIC
   ```
3. Put them in `.env`. Leave `PUBLIC_URL`, `RELAY_URL` and `NGROK_DOMAIN` for Step 5.
4. Set `CONSOLE_WORKDIR` to a sensible absolute folder (ask the person which folder their projects live in; their Desktop or home is fine as a default).
5. Ask: *"Do you want to be able to run shell commands from your phone (messages starting with `!`)? Anyone with the console topic could then run commands on this machine."* Set `CONSOLE_ALLOW_SHELL` to `true` or `false` based on the answer.
6. Confirm `.env` is gitignored: `git check-ignore .env` should print `.env`.

## Step 4: Phone setup

🛑 **STOP.** Show the person both topic names in a code block and tell them:

> 1. Install the **ntfy** app (Android: Play Store / F-Droid, iPhone: App Store).
> 2. Tap **+** → subscribe to **`<NTFY_TOPIC>`** (server `ntfy.sh`). Approvals arrive here.
> 3. Tap **+** → subscribe to **`<CONSOLE_TOPIC>`**. You'll type commands here.
> 4. Android: allow ntfy notifications and turn off battery optimisation for ntfy.
> 5. Treat both names like passwords. Don't share them.
>
> Tell me when both are subscribed.

When they confirm, send a test push to each topic (`curl -d "setup test" https://ntfy.sh/<topic>`) and ask whether both arrived. If one didn't, have them check that the topic was typed exactly.

## Step 5: Give the relay a public HTTPS URL (ngrok)

The phone's Approve/Deny tap must reach the relay over HTTPS.

🛑 **STOP** (if ngrok isn't set up yet). Tell the person:

> 1. Sign up free at https://ngrok.com and install ngrok (Windows: `winget install ngrok.ngrok`).
> 2. Copy your authtoken from the dashboard (*Your Authtoken*).
> 3. In the dashboard → *Domains*, claim your free static domain (looks like `something.ngrok-free.dev`).
> 4. Then tell me your domain. For the token, either paste it here or run this yourself:
>    `ngrok config add-authtoken <token>`

If they paste the token, run `ngrok config add-authtoken <token>` for them and don't repeat the token back afterwards.

Then set in `.env`:
```
PUBLIC_URL=https://<domain>
RELAY_URL=https://<domain>
NGROK_DOMAIN=<domain>
```

*(Alternative without an account: `cloudflared tunnel --url http://localhost:8000` gives a temporary `https://…trycloudflare.com` URL that changes every restart. Only offer it if they refuse ngrok.)*

## Step 6: Start the relay and tunnel, then verify

- **Windows:** `powershell -File scripts\start_all.ps1` (starts relay, ngrok and console hidden; logs go to `*.log` in the repo).
- **macOS/Linux:** start these in the background yourself:
  `PY -m uvicorn relay.app:app --host 127.0.0.1 --port 8000 --workers 1` and `ngrok http --url=<domain> 8000`.
  The console comes in Step 9.

Verify:
1. `curl http://127.0.0.1:8000/health` → `{"status":"ok"}`
2. `curl -H "ngrok-skip-browser-warning: 1" https://<domain>/health` → `{"status":"ok"}`

If (2) fails, check `ngrok_out.log` / the ngrok output (wrong domain, or a missing authtoken).

## Step 7: Install the hook into Claude Code

1. Copy `hook/phone_approve.py` to `~/.claude/hooks/phone_approve.py` (create the folder if needed).
2. Back up the settings: copy `~/.claude/settings.json` to `~/.claude/settings.json.bak-before-phone-hook` (if the file exists).
3. Read the existing settings, then prepare a **merged** version that adds:
   ```json
   {
     "env": {
       "RELAY_URL": "https://<domain>",
       "RELAY_API_KEY": "<RELAY_API_KEY from .env>",
       "WAIT_SECONDS": "240"
     },
     "hooks": {
       "PermissionRequest": [
         { "matcher": "*",
           "hooks": [ { "type": "command", "command": "<HOOK COMMAND>", "timeout": 300 } ] }
       ]
     }
   }
   ```
   - `<HOOK COMMAND>` on Windows: `python "C:/Users/<user>/.claude/hooks/phone_approve.py"`, using the absolute path with forward slashes and a `python` that is 3.11+ (the full path to a 3.11 interpreter is fine). On macOS/Linux: `python3 ~/.claude/hooks/phone_approve.py`.
   - Keep every existing key. If a `PermissionRequest` hook already exists, ask the person before adding another.
   - Do **not** add `onFailure: "block"`.
4. 🛑 **STOP.** Show the person the diff and ask for approval. Write the file only after they say yes. Validate that it's valid JSON afterwards.
5. Tell the person: **restart Claude Code** (exit and reopen in this folder) so the hook loads, then say *"continue setup from Step 8"*.

## Step 8: Test approvals end to end

Run the hook directly with a fake event. Set `RELAY_URL` and `RELAY_API_KEY` (values from `.env`) in the environment of this one command:

```powershell
# Windows PowerShell
$env:RELAY_URL="https://<domain>"; $env:RELAY_API_KEY="<key>"
'{"tool_name":"Bash","tool_input":{"command":"echo setup-test"},"session_id":"setup","cwd":"."}' | python "$HOME\.claude\hooks\phone_approve.py"
```
```bash
# macOS / Linux / Git Bash (or just run ./scripts/simulate_hook.sh, which reads .env)
echo '{"tool_name":"Bash","tool_input":{"command":"echo setup-test"},"session_id":"setup","cwd":"."}' \
  | RELAY_URL="https://<domain>" RELAY_API_KEY="<key>" python3 ~/.claude/hooks/phone_approve.py
```

🛑 **STOP.** Tell the person: *"Your phone should show 'Claude Code: Bash – echo setup-test'. Tap **Approve**."*
The command should print `{"hookSpecificOutput": … "behavior": "allow" …}`. Repeat once with **Deny** and check for `"behavior": "deny"`.

Then do a real check: try a harmless action that needs permission (e.g. create `phone_setup_test.txt`). The approval should arrive on the phone. Delete the file afterwards.

## Step 9: Start and test the phone console

- Windows: it's already running from Step 6 (check that `console_err.log` ends with `listening on topic`).
- Otherwise start it in the background: `PY -m console.phone_console` (from the repo root).

🛑 **STOP.** Tell the person:

> In the ntfy app, open the **console topic** and send: `/status`
> Then send: `what folder are you in?`
> (If shell is enabled, also try `!hostname`.)

You should see the replies posted back to the topic (you can confirm with `curl -s "https://ntfy.sh/<CONSOLE_TOPIC>/json?poll=1&since=5m"`). Explain to the person:
- Approvals for console jobs show up in the **approvals** topic labelled **"[phone console]"**. If they aren't answered within 4 minutes, they're denied.
- `/cd <path>` moves to a project first; `/new` starts a fresh conversation; `/stop` kills a job.
- The footer `- N turns, $X` is the API-equivalent cost. On a claude.ai subscription it isn't billed per message.

## Step 10: Finish

1. Optional: offer to make it start automatically at login (Windows Task Scheduler: *At log on* → `powershell.exe -WindowStyle Hidden -File "<repo>\scripts\start_all.ps1"`). Do this only if they say yes.
2. Run `git status` and confirm that `.env`, `*.log` and `console_state.json` are **not** listed as changes to commit.
3. Give the person a short summary:
   - which topic is for approvals and which is for the console (by role, not by pasting the secret again)
   - how to restart after a reboot or sleep (`scripts\start_all.ps1`)
   - that if Claude Code prompts stall for about 4 minutes, the relay or ngrok is down
   - where the settings backup is
   - pointers to `README.md` sections 8 (console) and 10 (troubleshooting)

## How to undo everything

If the person wants to remove it:
1. Restore `~/.claude/settings.json` from `settings.json.bak-before-phone-hook`, or delete just the `PermissionRequest` hook and the three `env` keys.
2. Delete `~/.claude/hooks/phone_approve.py`.
3. Stop the processes (`python … relay.app:app`, `python … console.phone_console`, `ngrok`) and remove any Task Scheduler entry.
4. Unsubscribe from both topics in the ntfy app.
