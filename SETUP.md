# Setup / Rebuild Guide (local, on your machine)

Secrets live only in `config.env` (and, if you add any, `CREDENTIALS.md`) — both
`chmod 600`, never committed. `config.env` is already gitignored here.

---

## 1. Prerequisites

- macOS or Linux with Python 3.9+ (`python3 --version`)
- The [`claude` CLI](https://claude.com/claude-code) installed & signed in
  (`claude --version`), or another LLM CLI that can run non-interactively
- A Gmail account with **2-Step Verification** on
- A calendar published as an **ICS** link (e.g. an iCloud or Google Calendar public link)

## 2. Gmail App Password (for IMAP read + SMTP send)

1. https://myaccount.google.com/security → enable **2-Step Verification**.
2. https://myaccount.google.com/apppasswords → create one, copy the 16 chars.
3. Put it in `config.env` as `GMAIL_APP_PASSWORD` (no spaces).

## 3. ICS calendar link

- **iCloud:** iPhone → **Calendar** → **Calendars** → tap **ⓘ** next to a calendar →
  **Public Calendar** on → **Share Link**.
- **Google Calendar:** Settings → your calendar → **Integrate calendar** → **Secret
  address in iCal format**.

Copy the `webcal://…` or `https://…ics` URL into `config.env` as `ICS_URL`
(comma-separate if you have more than one — e.g. a personal + work calendar).

## 4. Fill `config.env`

```bash
cp config.env.example config.env
chmod 600 config.env
# set GMAIL_ADDRESS, GMAIL_APP_PASSWORD, ICS_URL, TIMEZONE (e.g. America/New_York)
python3 briefing.py --self-test    # expect IMAP OK / SMTP OK / ICS OK
```

## 5. Set up your notes and assistant context

```bash
cp notes.md.example notes.md
cp CLAUDE.md.example CLAUDE.md   # if your AI CLI uses a project-context file
```

Edit both with your own preferences — whose events are whose, what's noise in your
inbox, what you want flagged. `briefing_prompt.md` reads `notes.md` every run, so you
rarely need to touch the prompt itself once it's set up.

## 6. Trust the workspace (critical if using Claude Code)

Headless `claude -p` **ignores `.claude/settings.json` unless the folder is trusted.**
Do one of:
- Run `claude` interactively in the folder once and accept "Do you trust the files in
  this folder?", or
- Set it directly:
  ```bash
  python3 - <<'PY'
  import json, os
  p = os.path.expanduser("~/.claude.json")
  d = json.load(open(p))
  d.setdefault("projects", {}).setdefault(os.getcwd(), {})["hasTrustDialogAccepted"] = True
  json.dump(d, open(p, "w"), indent=2)
  print("trusted:", os.getcwd())
  PY
  ```

## 7. Permission allowlist

`.claude/settings.json` (included) pre-authorizes exactly what the unattended run needs,
and explicitly denies the send commands so only the wrapper script can send:

```json
{
  "permissions": {
    "allow": ["Bash(python3 briefing.py --collect:*)", "Read", "Write"],
    "deny":  ["Bash(python3 briefing.py --send-input:*)", "Bash(python3 briefing.py)"]
  }
}
```

## 8. Schedule it

**macOS (launchd):**

```bash
mkdir -p ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/com.yourname.morningbriefing.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.yourname.morningbriefing</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>$(pwd)/run_briefing.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>7</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
PLIST
launchctl load -w ~/Library/LaunchAgents/com.yourname.morningbriefing.plist
launchctl list | grep morningbriefing     # confirm registered
```

If your machine sleeps overnight, wake it a couple minutes before the job runs:

```bash
sudo pmset repeat wake MTWRFSU 06:58:00
pmset -g sched                              # confirm the scheduled wake
```

Keep it plugged in overnight for a reliable scheduled wake.

**Linux (cron):**

```bash
crontab -e
# add:
0 7 * * * /bin/bash /path/to/run_briefing.sh
```

## 9. Test end-to-end

```bash
./run_briefing.sh && tail -25 briefing.log
```

Success = the log shows the reasoning step ran, then a `Sent:` line, and the email
arrives. If it falls back to the deterministic briefing, the reasoning step failed —
usually the workspace isn't trusted (step 6) or the allowlist is missing (step 7).
