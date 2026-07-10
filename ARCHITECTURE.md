# Architecture & Operations

Design, gotchas, and failure modes for the local, LLM-reasoned briefing.

---

## Why local (and not a hosted/cloud routine)

Sandboxed cloud agent environments commonly run behind an egress proxy that denies
outbound `CONNECT` to consumer SMTP providers and some calendar hosts (an org network
policy, not a bug). A hosted email connector may be limited to *drafting*, not sending.
Net effect: a cloud-scheduled version of this can end up unable to either fetch your
calendar or send the email — sometimes silently.

Running on your own machine with normal internet access sidesteps this entirely, at the
cost of needing your machine to be on (or wake itself) at send time.

## The two layers

- **Reasoning (the LLM).** `run_briefing.sh` calls `claude -p "$(cat briefing_prompt.md)"`.
  It runs `briefing.py --collect`, reasons like an assistant, and **writes
  `payload.json`** (`{subject, text, html}`). It's instructed NOT to send and NOT to
  fabricate — if it can't read real data, it leaves no payload.
- **Data + delivery (`briefing.py`, stdlib only).**
  - ICS parser: RFC 5545 unfolding, `DTSTART/DTEND` with `TZID`/UTC/all-day, text
    unescaping, and `RRULE` expansion (`DAILY`/`WEEKLY`+`BYDAY`/`MONTHLY`/`YEARLY`,
    `INTERVAL`/`UNTIL`).
  - IMAP: unread in the last day, newest first, + total-unread count.
  - SMTP: multipart text+HTML via `smtp.gmail.com:465`.
  - A deterministic `build_briefing` (today + week-ahead + rule-based proactive notes)
    is the fallback used when reasoning isn't available.

## Delivery flow & fallback

```
run_briefing.sh:
  rm payload.json
  claude -p (briefing_prompt.md)                     # reasoning writes payload.json
  if payload.json exists AND is valid JSON:
      python3 briefing.py --send-input < payload.json # send the reasoned briefing
  else:
      python3 briefing.py                             # deterministic briefing, still sends
```

On macOS, wrap the steps in `caffeinate -i` so a machine that just woke from a
scheduled `pmset` wake can't doze back off mid-run. The wrapper (a shell subprocess)
does the SMTP send itself, so a "deny send" rule in `.claude/settings.json` (which only
constrains the LLM's own tool calls) doesn't block it.

## Scheduling

- macOS: a launchd `.plist` with `StartCalendarInterval` at your chosen time, plus
  `pmset repeat wake` a couple minutes earlier if the machine sleeps overnight.
- Linux: a cron entry or systemd timer.

### DST / timezone

`briefing.py` uses the IANA `TIMEZONE` from `config.env` for all event math, so the
briefing *content* is always correct regardless of DST. The scheduler's time is local
wall-clock (handled by the OS), so a 7:00 AM job stays 7:00 AM across DST transitions —
no cron-in-UTC problem.

## Gotchas (design around these)

| Symptom | Cause | Fix |
|---|---|---|
| Falls back to the deterministic briefing every time | Workspace not **trusted** by Claude Code, so `.claude/settings.json`'s allowlist is ignored → the LLM can't read data | Trust the folder (SETUP §6) |
| Same, even when trusted | Allowlist missing/mismatched | Recreate `.claude/settings.json` (SETUP §7) |
| No email at all | Machine didn't wake / scheduler didn't fire | Keep it plugged in; check `pmset -g sched` (macOS) or `crontab -l`; check `briefing.log` |
| `--self-test` SMTP/IMAP fails | App password rotated / 2FA off | Generate a new Gmail App Password |
| A cloud-hosted version sends nothing | Sandbox egress policy blocks SMTP/calendar host | Don't run this in a cloud sandbox; it's designed to be local |

## Logs

- `briefing.log` — per-run: start, reasoning output, which path sent, `Sent:` line, exit.
- Any scheduler-level stdout/stderr logs your OS gives you (e.g. launchd's
  `StandardOutPath`/`StandardErrorPath` if you configure them).
