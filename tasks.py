#!/usr/bin/env python3
"""
tasks.py — a thin action-item ledger on top of the calendar.

A calendar event can't tell whether you actually DID the thing — it just sits in
the past. This ledger tracks follow-through: each action item is `open` until
confirmed `done`, and the morning briefing chases down anything overdue-and-open and
offers to reschedule.

Every task is linked to a real calendar event (via caldav_write.py) by its UID, so
adding/rescheduling/cancelling a task keeps the calendar in sync.

Ledger file: tasks.json (list of task dicts). Fields:
    id        short stable id (t1, t2, ...)
    title     one-line summary
    due       "YYYY-MM-DDTHH:MM" local (Pacific), or "YYYY-MM-DD" for all-day
    status    open | done | rescheduled | cancelled
    notes     free text (phone numbers, context)
    cal_uid   linked calendar event UID (None if not on calendar)
    history   list of {ts, action, detail}

Usage:
    python3 tasks.py --add --title "Call Fidelity" --due 2026-07-06T14:00 \
        [--notes "401k: ..."] [--duration 30] [--alarm 15] [--no-calendar]
    python3 tasks.py --list [--all]          # open by default; --all shows everything
    python3 tasks.py --overdue [--json]      # open items past due (for the briefing)
    python3 tasks.py --done <id>             # mark complete
    python3 tasks.py --reschedule <id> --due 2026-07-08T10:00
    python3 tasks.py --cancel <id>           # cancel + remove calendar event
    python3 tasks.py --note <id> --notes "…" # append/replace notes

Stdlib only. Uses caldav_write.py for calendar sync (best-effort; a calendar failure
never loses the ledger entry).
"""

import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "tasks.json")

# caldav_write is optional at import time so `--list` works even if calendar is down.
try:
    import caldav_write as cal
except Exception:  # pragma: no cover
    cal = None


def _now():
    return datetime.now()

def _stamp():
    return _now().strftime("%Y-%m-%dT%H:%M:%S")

def _parse_due(due):
    """Return a datetime for comparison; supports timed and all-day forms."""
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(due, fmt)
        except ValueError:
            continue
    return None

def load():
    if not os.path.exists(LEDGER):
        return []
    with open(LEDGER) as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

def save(tasks):
    with open(LEDGER, "w") as f:
        json.dump(tasks, f, indent=2, ensure_ascii=False)
        f.write("\n")

def next_id(tasks):
    n = 0
    for t in tasks:
        if t["id"].startswith("t") and t["id"][1:].isdigit():
            n = max(n, int(t["id"][1:]))
    return f"t{n + 1}"

def find(tasks, tid):
    for t in tasks:
        if t["id"] == tid:
            return t
    return None

def _log(task, action, detail=""):
    task.setdefault("history", []).append(
        {"ts": _stamp(), "action": action, "detail": detail})


# --- calendar sync (best effort) ---------------------------------------------

def _cal_add(title, due, notes, duration, alarm):
    if cal is None:
        return None
    all_day = "T" not in due
    try:
        calendar_url = cal.ensure_assistant_calendar()
        uid, ics = cal.build_vevent(
            title, due, duration_min=duration, desc=notes or "",
            location="", all_day=all_day, alarm_min=alarm)
        cal.put_event(calendar_url, uid, ics)
        return uid
    except SystemExit as e:
        print(f"  (calendar sync failed: {e})", file=sys.stderr)
        return None

def _cal_delete(uid):
    if cal is None or not uid:
        return
    try:
        calendar_url = cal.ensure_assistant_calendar()
        cal.delete_event(calendar_url, uid)
    except SystemExit as e:
        print(f"  (calendar delete failed: {e})", file=sys.stderr)


# --- commands -----------------------------------------------------------------

def cmd_add(args):
    tasks = load()
    tid = next_id(tasks)
    uid = None if args.no_calendar else _cal_add(
        args.title, args.due, args.notes, args.duration, args.alarm)
    task = {
        "id": tid, "title": args.title, "due": args.due, "status": "open",
        "notes": args.notes or "", "cal_uid": uid,
        "history": [{"ts": _stamp(), "action": "created", "detail": args.due}],
    }
    tasks.append(task)
    save(tasks)
    where = "calendar + ledger" if uid else "ledger only"
    print(f"Added {tid}: {args.title} @ {args.due}  ({where})")

def cmd_list(args):
    tasks = load()
    rows = tasks if args.all else [t for t in tasks if t["status"] == "open"]
    if not rows:
        print("No tasks." if args.all else "No open tasks.")
        return
    now = _now()
    for t in sorted(rows, key=lambda x: x["due"]):
        due_dt = _parse_due(t["due"])
        flag = ""
        if t["status"] == "open" and due_dt and due_dt < now:
            flag = "  ⚠ OVERDUE"
        note = f"  — {t['notes']}" if t["notes"] else ""
        print(f"[{t['id']}] {t['status']:11} {t['due']}  {t['title']}{flag}{note}")

def cmd_overdue(args):
    tasks = load()
    now = _now()
    od = []
    for t in tasks:
        if t["status"] != "open":
            continue
        due_dt = _parse_due(t["due"])
        if due_dt and due_dt < now:
            od.append(t)
    od.sort(key=lambda x: x["due"])
    if args.json:
        print(json.dumps(od, ensure_ascii=False, indent=2))
    elif not od:
        print("Nothing overdue.")
    else:
        for t in od:
            print(f"[{t['id']}] {t['due']}  {t['title']}"
                  + (f"  — {t['notes']}" if t["notes"] else ""))

def cmd_reap(args):
    """Assume-done model: auto-close any past-due open items as done (assumed).
    The default is to assume you handled them; reschedule if not.
    Prints the items it closed (so the briefing can give a light FYI)."""
    tasks = load()
    now = _now()
    reaped = []
    for t in tasks:
        if t["status"] != "open":
            continue
        due_dt = _parse_due(t["due"])
        if due_dt and due_dt < now:
            t["status"] = "done"
            _log(t, "auto-done", "assumed complete (past due)")
            reaped.append(t)
    if reaped:
        save(tasks)
    if args.json:
        print(json.dumps(reaped, ensure_ascii=False, indent=2))
    elif not reaped:
        print("Nothing to auto-close.")
    else:
        for t in reaped:
            print(f"assumed done [{t['id']}] {t['title']} (was due {t['due']})")

def cmd_done(args):
    tasks = load()
    t = find(tasks, args.id)
    if not t:
        sys.exit(f"No task {args.id}")
    t["status"] = "done"
    _log(t, "done")
    save(tasks)
    print(f"✓ {args.id} marked done: {t['title']}")

def cmd_reschedule(args):
    if not args.due:
        sys.exit("--reschedule requires --due")
    tasks = load()
    t = find(tasks, args.id)
    if not t:
        sys.exit(f"No task {args.id}")
    old_due = t["due"]
    # move the calendar event: delete old, create new
    _cal_delete(t.get("cal_uid"))
    new_uid = _cal_add(t["title"], args.due, t["notes"],
                       args.duration, args.alarm)
    t["due"] = args.due
    t["cal_uid"] = new_uid
    t["status"] = "open"
    _log(t, "rescheduled", f"{old_due} -> {args.due}")
    save(tasks)
    print(f"↻ {args.id} rescheduled: {old_due} -> {args.due}")

def cmd_cancel(args):
    tasks = load()
    t = find(tasks, args.id)
    if not t:
        sys.exit(f"No task {args.id}")
    _cal_delete(t.get("cal_uid"))
    t["status"] = "cancelled"
    t["cal_uid"] = None
    _log(t, "cancelled")
    save(tasks)
    print(f"✗ {args.id} cancelled: {t['title']}")

def cmd_note(args):
    tasks = load()
    t = find(tasks, args.id)
    if not t:
        sys.exit(f"No task {args.id}")
    t["notes"] = args.notes or ""
    _log(t, "note", t["notes"])
    save(tasks)
    print(f"✎ {args.id} notes updated.")


def main():
    ap = argparse.ArgumentParser(description="Action-item ledger synced to the calendar.")
    ap.add_argument("--add", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--overdue", action="store_true")
    ap.add_argument("--reap", action="store_true",
                    help="auto-close past-due open items as done (assumed complete)")
    ap.add_argument("--done", metavar="ID")
    ap.add_argument("--reschedule", metavar="ID")
    ap.add_argument("--cancel", metavar="ID")
    ap.add_argument("--note", metavar="ID")
    ap.add_argument("--title")
    ap.add_argument("--due")
    ap.add_argument("--notes")
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--alarm", type=int, default=15)
    ap.add_argument("--no-calendar", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.add:
        if not (args.title and args.due):
            sys.exit("--add requires --title and --due")
        return cmd_add(args)
    if args.list:
        return cmd_list(args)
    if args.overdue:
        return cmd_overdue(args)
    if args.reap:
        return cmd_reap(args)
    if args.done:
        args.id = args.done
        return cmd_done(args)
    if args.reschedule:
        args.id = args.reschedule
        return cmd_reschedule(args)
    if args.cancel:
        args.id = args.cancel
        return cmd_cancel(args)
    if args.note:
        args.id = args.note
        return cmd_note(args)
    ap.print_help()


if __name__ == "__main__":
    main()
