#!/bin/zsh
# Invoked by your scheduler (launchd/cron) at send time.
#
# Design: the LLM does the *reasoning* and writes payload.json; the Python script
# always does the *sending*. If the LLM is unavailable or fails, we fall back to
# the deterministic Python briefing so an email always goes out.

cd "$(dirname "$0")" || exit 1
DIR="$(pwd)"
LOG="$DIR/briefing.log"

# Point this at your `claude` (or other LLM CLI) binary, and make sure PATH covers
# wherever python3 lives on this machine.
CLAUDE="$(command -v claude)"
export PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] run start" >> "$LOG"
rm -f "$DIR/payload.json" "$DIR/archive_uids.txt"

# --- 0. Keep the inbox clean: archive+label known bulk/noise senders. ---
# Reversible (archived mail stays in All Mail). Sender list lives in inbox_cleanup.py.
/usr/bin/python3 inbox_cleanup.py --declutter --yes >> "$LOG" 2>&1

# --- 1. LLM reasons and writes payload.json (keep the machine awake meanwhile) ---
if [ -n "$CLAUDE" ] && [ -x "$CLAUDE" ]; then
    # Tools are pre-authorized via .claude/settings.json (scoped allowlist).
    # No blanket permission bypass — anything outside the allowlist is auto-denied.
    if command -v caffeinate >/dev/null 2>&1; then
        caffeinate -i "$CLAUDE" -p "$(cat "$DIR/briefing_prompt.md")" >> "$LOG" 2>&1
    else
        "$CLAUDE" -p "$(cat "$DIR/briefing_prompt.md")" >> "$LOG" 2>&1
    fi
fi

# --- 1b. Apply the LLM's content-aware archive list (reversible; wrapper does it). ---
if [ -s "$DIR/archive_uids.txt" ]; then
    N=$(grep -c . "$DIR/archive_uids.txt")
    echo "[$(date '+%H:%M:%S')] archiving $N content-triaged emails" >> "$LOG"
    /usr/bin/python3 inbox_cleanup.py --archive-file "$DIR/archive_uids.txt" --label "Auto-archived" >> "$LOG" 2>&1
fi

# --- 2. Send. Prefer the LLM's payload; else deterministic fallback. ---
if [ -s "$DIR/payload.json" ] && /usr/bin/python3 -c "import json,sys; json.load(open('$DIR/payload.json'))" 2>/dev/null; then
    echo "[$(date '+%H:%M:%S')] sending reasoned briefing" >> "$LOG"
    /usr/bin/python3 briefing.py --send-input < "$DIR/payload.json" >> "$LOG" 2>&1
else
    echo "[$(date '+%H:%M:%S')] payload missing/invalid — deterministic fallback" >> "$LOG"
    /usr/bin/python3 briefing.py >> "$LOG" 2>&1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] exit $?" >> "$LOG"
