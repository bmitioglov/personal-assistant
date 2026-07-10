# Your Personal Assistant

A daily morning briefing that reads your **live calendar and Gmail**, has an LLM
**reason over them like an assistant** — triaging your inbox, flagging what matters,
giving proactive recommendations — and emails it to you every morning. Runs entirely on
your own machine. No cloud service holds your data.

This isn't a calendar/email parser. The parsing is just plumbing. The point is a
briefing that thinks: *"you have back-to-back workouts today, and an early flight
tomorrow — wind down tonight,"* not a printout of your events.

Built with [Claude Code](https://claude.com/claude-code) as the reasoning engine, but
the pattern (separate the reasoning layer from the data/delivery layer, run it locally
on a schedule) works with any LLM CLI that can run non-interactively.

---

## How it works (all local)

```
pmset repeat wake HH:MM        →  wakes the machine (if it sleeps overnight)
launchd / cron (7:00 AM)       →  runs run_briefing.sh
  run_briefing.sh:
    1. claude -p (briefing_prompt.md)  →  reasons over live data, writes payload.json
    2. briefing.py --send-input        →  sends payload.json via Gmail SMTP
    if the reasoning step fails/skips  →  briefing.py (deterministic fallback) still sends
```

- **Reasoning = the LLM.** `claude -p` runs `briefing_prompt.md`, which calls
  `briefing.py --collect` for the data, thinks like an assistant, and writes the email
  content to `payload.json`.
- **Data + delivery = `briefing.py`** (Python stdlib only): fetches an ICS calendar,
  reads Gmail over IMAP, sends over SMTP.
- **Delivery is split from reasoning on purpose.** If the reasoning step ever fails, a
  plain deterministic briefing still goes out — an assistant that sometimes just
  doesn't show up isn't an assistant. The reasoning step is instructed never to
  fabricate; if it can't read real data it leaves no payload, and the fallback takes
  over.

## Why local, not a cloud/hosted routine

Sandboxed cloud environments commonly block outbound connections to consumer SMTP
providers and calendar hosts (org network policy), so a scheduled cloud job may be able
to *draft* but not *send*, or may not be able to reach your calendar feed at all. Running
on your own machine with normal internet access sidesteps this entirely — see
`ARCHITECTURE.md` for the specifics that bit us.

---

## Files

| File | Role |
|---|---|
| `CLAUDE.md.example` | Starting point for the assistant context your AI CLI loads when the folder is opened. Copy to `CLAUDE.md` and fill in your own details. |
| `briefing_prompt.md` | The morning reasoning instructions — edit this to change the briefing's tone, sections, and judgment calls. |
| `briefing.py` | Data + delivery engine (ICS parsing, IMAP read, SMTP send). Modes below. |
| `caldav_write.py` | Writes events to a CalDAV calendar (for "put X on my calendar" asks) — writes to a separate calendar so it's easy to review/undo. |
| `tasks.py` | A simple action-item ledger so the briefing can remind you of follow-ups a calendar can't confirm you actually did. |
| `inbox_cleanup.py` | Inbox hygiene via IMAP — archives known bulk senders, reversibly (never deletes). Customize the `NOISE` list for your own inbox. |
| `run_briefing.sh` | The wrapper script: declutter inbox → LLM reasons → Python sends, with fallback. |
| `config.env.example` | Copy to `config.env` and fill in your Gmail/ICS/timezone. Never commit the real file. |
| `.claude/settings.json` | Scoped permission allowlist so an unattended `claude -p` run can operate without prompting, and can't accidentally send email itself. |
| `SETUP.md` | Step-by-step setup from scratch. |
| `ARCHITECTURE.md` | Design deep-dive: why local, the fallback flow, gotchas. |

`briefing.py` modes: `--self-test` (check IMAP+SMTP+ICS), `--collect` (JSON snapshot of
today + next 7 days + unread, for the reasoning layer), `--send-input` (send a
`{subject,text,html}` JSON from stdin), `--dry-run` (build the deterministic briefing,
print it), and default (build + send the deterministic briefing).

---

## Quick start

1. Read `SETUP.md` and follow it end to end.
2. Copy `config.env.example` → `config.env`, fill in your Gmail app password and ICS
   calendar link.
3. Copy `notes.md.example` → `notes.md` — this is where *you* tell the assistant your
   standing preferences (what's noise, whose events are whose, things to watch for).
   Edit `briefing_prompt.md` for anything more structural.
4. `python3 briefing.py --self-test` to confirm IMAP/SMTP/ICS all connect.
5. Run `./run_briefing.sh` manually once, confirm the email arrives, then wire up the
   scheduler (launchd on macOS, cron/systemd timer on Linux).

## Change it by just talking to your AI CLI

Once this is running, open the folder and describe changes in plain English — that's
the whole point of the design:
- *"Make it shorter"* / *"more inbox detail"* → edits `briefing_prompt.md`
- *"Add my work calendar"* → adds an ICS URL to `config.env`
- *"Move it to 6:30"* → adjusts the scheduler time
- *"Stop flagging X"*, *"remind me about Y"* → gets added to `notes.md`

## Privacy

Nothing here calls out to a third-party server except your own Gmail (IMAP/SMTP) and
your own calendar provider (ICS/CalDAV). No data is sent to any service other than
whichever LLM CLI you choose to run for the reasoning step — read that tool's own data
handling policy before pointing it at your real inbox.
