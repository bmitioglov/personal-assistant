#!/usr/bin/env python3
"""
Morning briefing generator.

Self-contained (Python standard library only). Reads config from a `config.env`
file next to this script (or from environment variables), then:

  1. Fetches today's events from one or more ICS calendar URLs.
  2. Reads recent unread Gmail via IMAP.
  3. Composes a morning briefing.
  4. Sends it to you via Gmail SMTP (arrives as a real email).

Usage:
  python3 briefing.py            # build + send the briefing
  python3 briefing.py --dry-run  # build + print to stdout, do NOT send
  python3 briefing.py --self-test  # verify IMAP+SMTP creds and ICS fetch only
"""

import os
import re
import ssl
import sys
import html
import smtplib
import imaplib
import email
import datetime as dt
import urllib.request
from email.header import decode_header, make_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config():
    cfg = {}
    path = os.path.join(HERE, "config.env")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    # environment overrides file
    for k in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "ICS_URL",
              "BRIEFING_TO", "TIMEZONE", "MAX_EMAILS",
              "APPLE_ID", "ICLOUD_APP_PASSWORD", "CALDAV_CALENDAR_NAMES"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]

    if not cfg.get("GMAIL_ADDRESS"):
        die("Missing GMAIL_ADDRESS in config.")
    if not cfg.get("GMAIL_APP_PASSWORD"):
        die("Missing GMAIL_APP_PASSWORD in config.")
    cfg.setdefault("BRIEFING_TO", cfg["GMAIL_ADDRESS"])
    cfg.setdefault("TIMEZONE", "")
    cfg.setdefault("ICS_URL", "")
    cfg.setdefault("MAX_EMAILS", "12")
    cfg.setdefault("APPLE_ID", "")
    cfg.setdefault("ICLOUD_APP_PASSWORD", "")
    cfg.setdefault("CALDAV_CALENDAR_NAMES", "")
    return cfg


def die(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


def local_tz(cfg):
    name = cfg.get("TIMEZONE", "").strip()
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    # fall back to system local time
    return dt.datetime.now().astimezone().tzinfo


# --------------------------------------------------------------------------- #
# ICS calendar
# --------------------------------------------------------------------------- #
def fetch_url(url):
    url = url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    req = urllib.request.Request(url, headers={"User-Agent": "briefing/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def unfold_ics(text):
    # RFC 5545 line folding: continuation lines start with space or tab.
    out = []
    for line in text.splitlines():
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def parse_ics_dt(value, params, tz):
    """Return an aware datetime, or a date (for all-day)."""
    value = value.strip()
    is_date = params.get("VALUE") == "DATE" or (len(value) == 8 and "T" not in value)
    if is_date:
        return dt.datetime.strptime(value[:8], "%Y%m%d").date()
    if value.endswith("Z"):
        d = dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ")
        return d.replace(tzinfo=dt.timezone.utc)
    d = dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    tzid = params.get("TZID")
    if tzid and ZoneInfo is not None:
        try:
            return d.replace(tzinfo=ZoneInfo(tzid))
        except Exception:
            pass
    return d.replace(tzinfo=tz)  # floating -> assume local


def unescape_ics_text(value):
    # RFC 5545 TEXT escaping: \\ \, \; \n \N
    out = []
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in ("n", "N"):
                out.append(", ")
            elif nxt in ("\\", ",", ";"):
                out.append(nxt)
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out).strip()


def _split_prop(line):
    # NAME;PARAM=x;PARAM2=y:VALUE
    if ":" not in line:
        return None, {}, None
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].upper()
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v
    return name, params, value


def parse_events(ics_text, tz):
    events = []
    cur = None
    for line in unfold_ics(ics_text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None:
            name, params, value = _split_prop(line)
            if name in ("DTSTART", "DTEND"):
                cur[name] = parse_ics_dt(value, params, tz)
            elif name == "SUMMARY":
                cur["SUMMARY"] = unescape_ics_text(value)
            elif name == "LOCATION":
                cur["LOCATION"] = unescape_ics_text(value)
            elif name == "RRULE":
                cur["RRULE"] = params_from_rrule(value)
    return events


def params_from_rrule(value):
    out = {}
    for kv in value.split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k.upper()] = v.upper()
    return out


WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def occurs_on(ev, today, tz):
    """Does this event (possibly recurring) have an instance on `today`?"""
    start = ev.get("DTSTART")
    if start is None:
        return False
    start_date = start.date() if isinstance(start, dt.datetime) else start

    rr = ev.get("RRULE")
    if not rr:
        return start_date == today

    if start_date > today:
        return False
    until = rr.get("UNTIL")
    if until:
        try:
            u = dt.datetime.strptime(until[:8], "%Y%m%d").date()
            if today > u:
                return False
        except Exception:
            pass

    freq = rr.get("FREQ", "")
    interval = int(rr.get("INTERVAL", "1") or "1")
    delta_days = (today - start_date).days

    if freq == "DAILY":
        return delta_days % interval == 0
    if freq == "WEEKLY":
        byday = rr.get("BYDAY")
        if byday:
            days = {WEEKDAYS[d[-2:]] for d in byday.split(",") if d[-2:] in WEEKDAYS}
            if today.weekday() not in days:
                return False
            return (delta_days // 7) % interval == 0
        return today.weekday() == start_date.weekday() and (delta_days // 7) % interval == 0
    if freq == "MONTHLY":
        if today.day != start_date.day:
            return False
        months = (today.year - start_date.year) * 12 + (today.month - start_date.month)
        return months % interval == 0
    if freq == "YEARLY":
        return (today.month, today.day) == (start_date.month, start_date.day) \
            and (today.year - start_date.year) % interval == 0
    # unknown frequency: be conservative, only exact start date
    return start_date == today


def event_sort_key(ev):
    s = ev.get("DTSTART")
    if isinstance(s, dt.datetime):
        return (0, s.hour, s.minute)
    return (-1, 0, 0)  # all-day first


def _caldav_events(cfg, tz):
    """Fetch VEVENTs from additional named iCloud calendars via authenticated
    CalDAV (for calendars that aren't published as a public ICS feed)."""
    names = [n for n in re.split(r"[,\n]", cfg.get("CALDAV_CALENDAR_NAMES", "")) if n.strip()]
    if not names or not cfg.get("APPLE_ID") or not cfg.get("ICLOUD_APP_PASSWORD"):
        return [], []
    sys.path.insert(0, HERE)
    import caldav_write as cw
    evs, errors = [], []
    try:
        home = cw.discover_calendar_home()
        cals = cw.list_calendars(home)
    except SystemExit as e:
        return [], ["CalDAV discovery failed (%s)" % e]
    for name in names:
        url = next((u for n, u in cals.items() if n.strip().lower() == name.strip().lower()), None)
        if not url:
            errors.append("CalDAV calendar '%s' not found" % name)
            continue
        try:
            text = cw.fetch_raw_ics(url)
            evs.extend(parse_events(text, tz))
        except SystemExit as e:
            errors.append("CalDAV calendar '%s' (%s)" % (name, e))
    return evs, errors


def _all_events(cfg, tz):
    """Fetch and parse every VEVENT from all configured ICS URLs and CalDAV
    calendars (once)."""
    urls = [u for u in re.split(r"[,\n]", cfg.get("ICS_URL", "")) if u.strip()]
    evs, errors = [], []
    for url in urls:
        try:
            text = fetch_url(url)
            evs.extend(parse_events(text, tz))
        except Exception as e:
            errors.append("%s (%s)" % (url.strip()[:60], e))
    caldav_evs, caldav_errors = _caldav_events(cfg, tz)
    evs.extend(caldav_evs)
    errors.extend(caldav_errors)
    return evs, errors


def collect_calendar(cfg, today, tz, all_events=None):
    if all_events is not None:
        evs, errors = all_events, []
    else:
        evs, errors = _all_events(cfg, tz)
    todays = [ev for ev in evs if occurs_on(ev, today, tz)]
    todays.sort(key=event_sort_key)
    return todays, errors


def collect_week(cfg, start_day, days, tz, all_events=None):
    """Return list of (date, [events]) for `days` starting the day AFTER start_day."""
    evs = all_events if all_events is not None else _all_events(cfg, tz)[0]
    week = []
    for n in range(1, days + 1):
        d = start_day + dt.timedelta(days=n)
        day_evs = [ev for ev in evs if occurs_on(ev, d, tz)]
        day_evs.sort(key=event_sort_key)
        week.append((d, day_evs))
    return week


def fmt_event(ev):
    s = ev.get("DTSTART")
    summary = ev.get("SUMMARY", "(no title)")
    loc = ev.get("LOCATION", "")
    if isinstance(s, dt.datetime):
        e = ev.get("DTEND")
        t = s.strftime("%-I:%M %p")
        if isinstance(e, dt.datetime):
            t += "–" + e.strftime("%-I:%M %p")
    else:
        t = "All day"
    line = "%s  —  %s" % (t, summary)
    if loc:
        line += "  (%s)" % loc
    return line


# --------------------------------------------------------------------------- #
# Gmail via IMAP
# --------------------------------------------------------------------------- #
def decode_hdr(raw):
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def collect_emails(cfg, tz, max_emails):
    addr = cfg["GMAIL_ADDRESS"]
    pw = cfg["GMAIL_APP_PASSWORD"]
    out = []
    ctx = ssl.create_default_context()
    M = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ctx)
    try:
        M.login(addr, pw)
        M.select("INBOX", readonly=True)
        since = (dt.datetime.now(tz) - dt.timedelta(days=1)).strftime("%d-%b-%Y")
        typ, data = M.search(None, "(UNSEEN SINCE %s)" % since)
        ids = data[0].split() if data and data[0] else []
        ids = ids[-max_emails:][::-1]  # newest first
        for i in ids:
            typ, msg_data = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            hdr = email.message_from_bytes(msg_data[0][1])
            out.append({
                "from": decode_hdr(hdr.get("From", "")),
                "subject": decode_hdr(hdr.get("Subject", "(no subject)")),
            })
        # also grab the total unread count
        typ, data = M.search(None, "UNSEEN")
        total_unread = len(data[0].split()) if data and data[0] else 0
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return out, total_unread


def short_from(f):
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', f)
    if m:
        return m.group(1).strip()
    return f.strip()


# --------------------------------------------------------------------------- #
# Compose + send
# --------------------------------------------------------------------------- #
def _timed(evs):
    return [e for e in evs if isinstance(e.get("DTSTART"), dt.datetime)]


def proactive_notes(today, events, week, tz):
    """Rule-based 'assistant' observations over today + the week ahead."""
    notes = []
    # Today's load.
    n = len(events)
    if n == 0:
        notes.append("Today's clear — a good window to get ahead or tackle the inbox.")
    elif n >= 3:
        notes.append("Busy one today — %d things on the calendar." % n)

    # Early start tomorrow?
    if week:
        tmr_date, tmr_evs = week[0]
        early = [e for e in _timed(tmr_evs) if e["DTSTART"].hour < 9]
        if early:
            e0 = min(early, key=lambda e: e["DTSTART"])
            notes.append("Early start tomorrow — %s at %s. Might be worth winding down tonight."
                         % (e0.get("SUMMARY", "an event"), e0["DTSTART"].strftime("%-I:%M %p")))

    # Double-session / packed days in the week ahead.
    packed = ["%s (%d)" % (d.strftime("%a"), len(_timed(evs)))
              for d, evs in week if len(_timed(evs)) >= 2]
    if packed:
        notes.append("Heavier days ahead: " + ", ".join(packed) + ".")

    # All-day recurring events today are likely anniversaries / family milestones.
    # Highlight them warmly — never treat them as cruft to delete.
    for e in events:
        if e.get("RRULE") and not isinstance(e.get("DTSTART"), dt.datetime):
            name = e.get("SUMMARY", "(untitled)")
            notes.append("💛 %s is today — a meaningful date. Consider blocking some "
                         "evening time to celebrate or remember it." % name)
    return notes


def fmt_day_line(d, evs):
    if not evs:
        return "%s — Clear" % d.strftime("%a %b %-d")
    parts = []
    for e in evs:
        s = e.get("DTSTART")
        t = s.strftime("%-I:%M %p") if isinstance(s, dt.datetime) else "all day"
        parts.append("%s %s" % (e.get("SUMMARY", "(untitled)"), t))
    return "%s — %s" % (d.strftime("%a %b %-d"), " · ".join(parts))


def build_briefing(cfg, today, tz):
    all_events, cal_errors = _all_events(cfg, tz)
    events, _ = collect_calendar(cfg, today, tz, all_events=all_events)
    week = collect_week(cfg, today, 7, tz, all_events=all_events)
    try:
        emails, total_unread = collect_emails(cfg, tz, int(cfg["MAX_EMAILS"]))
        email_error = None
    except Exception as e:
        emails, total_unread, email_error = [], 0, str(e)

    notes = proactive_notes(today, events, week, tz)
    date_str = today.strftime("%A, %B %-d, %Y")

    # ---- plain text ----
    lines = ["Good morning! Here's %s.\n" % date_str]
    if notes:
        lines.append("— THE SHORT VERSION —")
        for nline in notes:
            lines.append("  • " + nline)
        lines.append("")
    lines.append("📅 TODAY (%d)" % len(events))
    if events:
        for ev in events:
            lines.append("  • " + fmt_event(ev))
    else:
        lines.append("  • Nothing scheduled.")
    if cal_errors:
        lines.append("  ⚠ Could not read some calendars: " + "; ".join(cal_errors))
    lines.append("")
    lines.append("🗓️  THE WEEK AHEAD")
    for d, evs in week:
        lines.append("  " + fmt_day_line(d, evs))
    lines.append("")
    lines.append("✉️  UNREAD EMAIL (%d recent, %d total unread)"
                 % (len(emails), total_unread))
    if email_error:
        lines.append("  ⚠ Could not read inbox: " + email_error)
    elif emails:
        for m in emails:
            lines.append("  • %s — %s" % (short_from(m["from"]), m["subject"]))
    else:
        lines.append("  • No new unread email.")
    lines.append("")
    lines.append("Have a great day!")
    text_body = "\n".join(lines)

    # ---- html ----
    def esc(s):
        return html.escape(s, quote=False)

    h = ['<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
         'font-size:15px;color:#1a1a1a;line-height:1.55;max-width:620px;">']
    h.append('<p style="font-size:17px;margin:0 0 4px;"><b>Good morning</b> — %s</p>' % esc(date_str))
    if notes:
        h.append('<div style="background:#f5f7fb;border-left:4px solid #4a86e8;padding:10px 14px;border-radius:4px;margin:14px 0;">')
        h.append("<b>The short version</b><ul style='margin:6px 0 0;padding-left:20px;'>")
        for nline in notes:
            h.append("<li>%s</li>" % esc(nline))
        h.append("</ul></div>")
    h.append('<h3 style="margin:18px 0 6px;">📅 Today <span style="color:#888;font-weight:normal;">(%d)</span></h3>' % len(events))
    if events:
        h.append("<ul style='margin:0;padding-left:20px;'>")
        for ev in events:
            h.append("<li>%s</li>" % esc(fmt_event(ev)))
        h.append("</ul>")
    else:
        h.append("<p style='color:#666;'>Nothing scheduled.</p>")
    if cal_errors:
        h.append("<p style='color:#b00;'>⚠ Could not read some calendars: %s</p>" % esc("; ".join(cal_errors)))
    h.append('<h3 style="margin:18px 0 6px;">🗓️ The week ahead</h3>')
    h.append("<table style='border-collapse:collapse;font-size:14px;'>")
    for d, evs in week:
        label = d.strftime("%a %b %-d")
        if evs:
            body = " · ".join("%s %s" % (esc(e.get("SUMMARY", "(untitled)")),
                              e["DTSTART"].strftime("%-I:%M %p") if isinstance(e.get("DTSTART"), dt.datetime) else "all day")
                              for e in evs)
        else:
            body = "<span style='color:#999;'>Clear</span>"
        h.append("<tr><td style='padding:3px 12px 3px 0;color:#888;white-space:nowrap;'>%s</td><td>%s</td></tr>" % (esc(label), body))
    h.append("</table>")
    h.append('<h3 style="margin:18px 0 6px;">✉️ Unread email <span style="color:#888;font-weight:normal;">(%d recent, %d total)</span></h3>' % (len(emails), total_unread))
    if email_error:
        h.append("<p style='color:#b00;'>⚠ Could not read inbox: %s</p>" % esc(email_error))
    elif emails:
        h.append("<ul style='margin:0;padding-left:20px;'>")
        for m in emails:
            h.append("<li><b>%s</b> — %s</li>" % (esc(short_from(m["from"])), esc(m["subject"])))
        h.append("</ul>")
    else:
        h.append("<p style='color:#666;'>No new unread email.</p>")
    h.append("<p style='margin-top:18px;color:#666;'>Have a great day!</p>")
    h.append("</div>")
    html_body = "\n".join(h)

    subject = "☀️ Morning Briefing — %s" % today.strftime("%a %b %-d")
    return subject, text_body, html_body


def event_to_dict(ev):
    s = ev.get("DTSTART")
    e = ev.get("DTEND")
    if isinstance(s, dt.datetime):
        start = s.isoformat()
        all_day = False
        time_label = s.strftime("%-I:%M %p")
        if isinstance(e, dt.datetime):
            time_label += "–" + e.strftime("%-I:%M %p")
    else:
        start = s.isoformat() if s else None
        all_day = True
        time_label = "All day"
    return {
        "summary": ev.get("SUMMARY", "(no title)"),
        "location": ev.get("LOCATION", ""),
        "start": start,
        "all_day": all_day,
        "time_label": time_label,
        "recurring": bool(ev.get("RRULE")),
    }


def collect_json(cfg, today, tz):
    """Structured snapshot for the reasoning layer: today + next 7 days + inbox."""
    all_events, cal_errors = _all_events(cfg, tz)
    todays, _ = collect_calendar(cfg, today, tz, all_events=all_events)
    week = collect_week(cfg, today, 7, tz, all_events=all_events)
    try:
        emails, total_unread = collect_emails(cfg, tz, int(cfg["MAX_EMAILS"]))
        email_error = None
    except Exception as e:
        emails, total_unread, email_error = [], 0, str(e)
    return {
        "generated_for": today.isoformat(),
        "weekday": today.strftime("%A"),
        "timezone": str(tz),
        "today": [event_to_dict(ev) for ev in todays],
        "week_ahead": [
            {"date": d.isoformat(), "weekday": d.strftime("%A"),
             "events": [event_to_dict(ev) for ev in evs]}
            for d, evs in week
        ],
        "unread_email": [
            {"from": short_from(m["from"]), "subject": m["subject"]} for m in emails
        ],
        "total_unread": total_unread,
        "email_error": email_error,
        "calendar_errors": cal_errors,
    }


def send_email(cfg, subject, text_body, html_body):
    addr = cfg["GMAIL_ADDRESS"]
    pw = cfg["GMAIL_APP_PASSWORD"]
    to = cfg["BRIEFING_TO"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = addr
    msg["To"] = to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(addr, pw)
        s.sendmail(addr, [to], msg.as_string())


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cfg = load_config()
    tz = local_tz(cfg)
    today = dt.datetime.now(tz).date()

    if "--self-test" in sys.argv:
        print("Config loaded. Account:", cfg["GMAIL_ADDRESS"])
        # test IMAP
        try:
            emails, unread = collect_emails(cfg, tz, 3)
            print("IMAP OK — %d total unread, sample: %d fetched" % (unread, len(emails)))
        except Exception as e:
            print("IMAP FAILED:", e)
        # test SMTP login (no send)
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
                s.login(cfg["GMAIL_ADDRESS"], cfg["GMAIL_APP_PASSWORD"])
            print("SMTP OK — login succeeded")
        except Exception as e:
            print("SMTP FAILED:", e)
        # test ICS
        events, errs = collect_calendar(cfg, today, tz)
        print("ICS OK — %d events today" % len(events))
        for e in errs:
            print("  ICS error:", e)
        return

    if "--collect" in sys.argv:
        import json
        print(json.dumps(collect_json(cfg, today, tz), indent=2, ensure_ascii=False))
        return

    if "--send-input" in sys.argv:
        # Read a JSON payload from stdin: {"subject","text","html"}.
        import json
        payload = json.load(sys.stdin)
        subject = payload.get("subject", "Morning Briefing")
        text_body = payload.get("text", "")
        html_body = payload.get("html", "") or ("<pre>%s</pre>" % html.escape(text_body))
        send_email(cfg, subject, text_body, html_body)
        print("Sent:", subject, "->", cfg["BRIEFING_TO"])
        return

    # Default: fully self-contained briefing (fallback if the brain layer is down).
    subject, text_body, html_body = build_briefing(cfg, today, tz)
    if "--dry-run" in sys.argv:
        print("SUBJECT:", subject)
        print("-" * 60)
        print(text_body)
        return
    send_email(cfg, subject, text_body, html_body)
    print("Sent:", subject, "->", cfg["BRIEFING_TO"])


if __name__ == "__main__":
    main()
