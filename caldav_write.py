#!/usr/bin/env python3
"""
caldav_write.py — write events to your iCloud calendar over CalDAV.

The published ICS feed (see ICS_URL in config.env) is read-only. This module uses
iCloud's CalDAV endpoint with an app-specific password to actually create / update /
delete events. It writes to a dedicated calendar (ASSISTANT_CALENDAR_NAME, default
"Assistant") so anything the assistant creates is visually distinct and reversible.

Config (config.env):
    APPLE_ID              your Apple ID email  (CalDAV username)
    ICLOUD_APP_PASSWORD   app-specific password from appleid.apple.com
    ASSISTANT_CALENDAR_NAME   target calendar name (created if missing)

Usage:
    python3 caldav_write.py --self-test
        Verify auth + discover the calendar home. Creates the Assistant calendar
        if missing. Makes no events.

    python3 caldav_write.py --add \
        --summary "Schedule tooth removal (Super Dentist referral)" \
        --start 2026-07-06T09:00 --duration 15 \
        --desc "Call to book the extraction. Have insurance + availability ready." \
        --alarm 10
        Create a timed event (local time, TIMEZONE from config.env). --duration in
        minutes. --alarm N adds an N-minute-before reminder (omit for none).
        --all-day with --start YYYY-MM-DD makes an all-day event.

    python3 caldav_write.py --list [--days 14]
        List upcoming events on the Assistant calendar.

    python3 caldav_write.py --delete --uid <uid>
        Delete an event by UID from the Assistant calendar.

Stdlib only. Callable from the morning run or ad hoc.
"""

import argparse
import http.client
import os
import ssl
import sys
import urllib.parse
from base64 import b64encode
from datetime import datetime, timedelta

# --- config loading (mirrors briefing.py's simple .env reader) ---------------

def _load_config():
    cfg = {}
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "config.env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    # env overrides file
    for k in ("APPLE_ID", "ICLOUD_APP_PASSWORD", "ASSISTANT_CALENDAR_NAME", "TIMEZONE"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg

CFG = _load_config()
APPLE_ID = CFG.get("APPLE_ID", "")
APP_PW = CFG.get("ICLOUD_APP_PASSWORD", "")
CAL_NAME = CFG.get("ASSISTANT_CALENDAR_NAME", "Assistant")
TZID = CFG.get("TIMEZONE", "America/Los_Angeles")

BOOTSTRAP_HOST = "caldav.icloud.com"

# --- low-level CalDAV over http.client ---------------------------------------

_CTX = ssl.create_default_context()

def _auth_header():
    if not APPLE_ID or not APP_PW:
        sys.exit("ERROR: APPLE_ID and ICLOUD_APP_PASSWORD must be set in config.env.")
    raw = f"{APPLE_ID}:{APP_PW}".encode()
    return "Basic " + b64encode(raw).decode()

def _request(method, url, body="", extra_headers=None, _redirects=0):
    """Issue a CalDAV request. Follows redirects (preserving auth for icloud hosts)."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    path = parts.path + (("?" + parts.query) if parts.query else "")
    headers = {
        "Authorization": _auth_header(),
        "Content-Type": "application/xml; charset=utf-8",
        "User-Agent": "personal-assistant-caldav/1.0",
    }
    if extra_headers:
        headers.update(extra_headers)
    conn = http.client.HTTPSConnection(host, context=_CTX, timeout=30)
    try:
        conn.request(method, path, body=body.encode("utf-8") if body else None,
                     headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", "replace")
        status = resp.status
        location = resp.getheader("Location")
    finally:
        conn.close()
    # iCloud bounces the bootstrap host to a per-user shard (pNN-caldav...).
    if status in (301, 302, 307, 308) and location and _redirects < 5:
        nxt = location if "://" in location else f"https://{host}{location}"
        return _request(method, nxt, body, extra_headers, _redirects + 1)
    return status, data, location

# --- discovery ----------------------------------------------------------------

def _extract(tag, xml):
    """Pull inner text of the first <...tag>...</tag> (namespace-agnostic)."""
    import re
    m = re.search(rf"<[^>]*{tag}[^>]*>(.*?)</[^>]*{tag}[^>]*>", xml, re.S | re.I)
    return m.group(1).strip() if m else None

def _href(xml_fragment):
    import re
    m = re.search(r"<[^>]*href[^>]*>(.*?)</[^>]*href[^>]*>", xml_fragment, re.S | re.I)
    return m.group(1).strip() if m else None

def discover_calendar_home():
    """Return the absolute URL of the user's calendar-home-set collection."""
    principal_body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:"><d:prop>'
        '<d:current-user-principal/></d:prop></d:propfind>'
    )
    status, data, _ = _request(
        "PROPFIND", f"https://{BOOTSTRAP_HOST}/", principal_body,
        {"Depth": "0"})
    if status not in (207, 200):
        sys.exit(f"ERROR: principal PROPFIND failed ({status}). Check Apple ID / "
                 f"app-specific password.\n{data[:500]}")
    principal_href = None
    import re
    m = re.search(r"<[^>]*current-user-principal[^>]*>(.*?)</[^>]*current-user-principal[^>]*>",
                  data, re.S | re.I)
    if m:
        principal_href = _href(m.group(1))
    if not principal_href:
        sys.exit(f"ERROR: could not find current-user-principal.\n{data[:500]}")
    principal_url = principal_href if "://" in principal_href \
        else f"https://{BOOTSTRAP_HOST}{principal_href}"

    home_body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        '<d:prop><c:calendar-home-set/></d:prop></d:propfind>'
    )
    status, data, _ = _request("PROPFIND", principal_url, home_body, {"Depth": "0"})
    if status not in (207, 200):
        sys.exit(f"ERROR: calendar-home PROPFIND failed ({status}).\n{data[:500]}")
    m = re.search(r"<[^>]*calendar-home-set[^>]*>(.*?)</[^>]*calendar-home-set[^>]*>",
                  data, re.S | re.I)
    home_href = _href(m.group(1)) if m else None
    if not home_href:
        sys.exit(f"ERROR: could not find calendar-home-set.\n{data[:500]}")
    home_url = home_href if "://" in home_href else f"https://{BOOTSTRAP_HOST}{home_href}"
    return home_url

def list_calendars(home_url):
    """Return {display_name: collection_url} for calendar collections in the home."""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:"><d:prop>'
        '<d:resourcetype/><d:displayname/></d:prop></d:propfind>'
    )
    status, data, _ = _request("PROPFIND", home_url, body, {"Depth": "1"})
    if status not in (207, 200):
        sys.exit(f"ERROR: listing calendars failed ({status}).\n{data[:500]}")
    import re
    cals = {}
    # split into <response> blocks
    for block in re.split(r"</[^>]*response[^>]*>", data, flags=re.I):
        if "calendar" not in block.lower():
            continue
        if "resourcetype" not in block.lower() or "<c:calendar" not in block.lower() \
                and "calendar/>" not in block.lower() and ":calendar" not in block.lower():
            # be permissive; require the resourcetype to mention calendar
            if not re.search(r"resourcetype.*calendar", block, re.S | re.I):
                continue
        href = _href(block)
        name = _extract("displayname", block)
        if href:
            url = href if "://" in href else urllib.parse.urljoin(home_url, href)
            cals[(name or href).strip()] = url
    return cals

def ensure_assistant_calendar():
    """Discover home, find or create the Assistant calendar, return its URL."""
    home = discover_calendar_home()
    cals = list_calendars(home)
    for name, url in cals.items():
        if name.lower() == CAL_NAME.lower():
            return url
    # create it — iCloud requires a fresh UUID path for the new collection
    import uuid
    new_url = home.rstrip("/") + "/" + str(uuid.uuid4()).upper() + "/"
    mk_body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        '<d:set><d:prop>'
        f'<d:displayname>{CAL_NAME}</d:displayname>'
        '<c:supported-calendar-component-set>'
        '<c:comp name="VEVENT"/></c:supported-calendar-component-set>'
        '</d:prop></d:set></c:mkcalendar>'
    )
    status, data, _ = _request("MKCALENDAR", new_url, mk_body)
    if status not in (201, 200):
        sys.exit(f"ERROR: could not create '{CAL_NAME}' calendar ({status}).\n{data[:500]}")
    return new_url

# --- event building / writing -------------------------------------------------

def _fold(line):
    """RFC5545 line folding at 75 octets."""
    out = []
    while len(line.encode()) > 75:
        # naive but safe enough for ASCII-ish content
        out.append(line[:73])
        line = " " + line[73:]
    out.append(line)
    return "\r\n".join(out)

def _esc(text):
    return (text.replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))

def build_vevent(summary, start, duration_min=15, desc="", location="",
                 all_day=False, alarm_min=None, uid=None):
    if uid is None:
        # deterministic-ish uid without Date.now(): derive from start+summary
        base = f"{start}-{summary}".encode().hex()[:24]
        uid = f"{base}@personal-assistant"
    # DTSTAMP: use start as a stable stamp to avoid needing wall-clock now.
    if all_day:
        d = datetime.strptime(start, "%Y-%m-%d")
        dtstart = f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}"
        dtend = f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}"
        stamp = d.strftime("%Y%m%dT000000Z")
    else:
        d = datetime.strptime(start, "%Y-%m-%dT%H:%M")
        end = d + timedelta(minutes=duration_min)
        dtstart = f"DTSTART;TZID={TZID}:{d.strftime('%Y%m%dT%H%M%S')}"
        dtend = f"DTEND;TZID={TZID}:{end.strftime('%Y%m%dT%H%M%S')}"
        stamp = d.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//Personal Assistant//CalDAV//EN",
        "CALSCALE:GREGORIAN", "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{stamp}", dtstart, dtend,
        f"SUMMARY:{_esc(summary)}",
    ]
    if desc:
        lines.append(f"DESCRIPTION:{_esc(desc)}")
    if location:
        lines.append(f"LOCATION:{_esc(location)}")
    if alarm_min is not None:
        lines += ["BEGIN:VALARM", f"TRIGGER:-PT{int(alarm_min)}M",
                  "ACTION:DISPLAY", f"DESCRIPTION:{_esc(summary)}", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return uid, "\r\n".join(_fold(l) for l in lines) + "\r\n"

def put_event(calendar_url, uid, ics_text):
    url = calendar_url.rstrip("/") + "/" + urllib.parse.quote(uid) + ".ics"
    status, data, _ = _request(
        "PUT", url, ics_text,
        {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"})
    if status in (412,):  # already exists — overwrite without If-None-Match
        status, data, _ = _request(
            "PUT", url, ics_text, {"Content-Type": "text/calendar; charset=utf-8"})
    if status not in (200, 201, 204):
        sys.exit(f"ERROR: PUT event failed ({status}).\n{data[:500]}")
    return url

def delete_event(calendar_url, uid):
    url = calendar_url.rstrip("/") + "/" + urllib.parse.quote(uid) + ".ics"
    status, data, _ = _request("DELETE", url, "")
    if status not in (200, 204, 404):
        sys.exit(f"ERROR: DELETE failed ({status}).\n{data[:500]}")
    return status

def fetch_raw_ics(calendar_url):
    """REPORT calendar-query and return concatenated raw ICS text (VEVENT blocks,
    XML-unescaped) for downstream parsing, e.g. by briefing.py's parse_events()."""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        '<d:prop><d:getetag/><c:calendar-data/></d:prop>'
        '<c:filter><c:comp-filter name="VCALENDAR">'
        '<c:comp-filter name="VEVENT"/></c:comp-filter></c:filter>'
        '</c:calendar-query>'
    )
    status, data, _ = _request("REPORT", calendar_url, body, {"Depth": "1"})
    if status not in (207, 200):
        sys.exit(f"ERROR: calendar-query failed ({status}).\n{data[:500]}")
    import re
    import xml.sax.saxutils as saxutils
    chunks = []
    for m in re.finditer(r"<[^>]*calendar-data[^>]*>(.*?)</[^>]*calendar-data[^>]*>",
                          data, re.S | re.I):
        chunks.append(saxutils.unescape(m.group(1)))
    return "\n".join(chunks)


def list_events(calendar_url, days=14):
    """REPORT calendar-query for VEVENTs; print SUMMARY/UID/DTSTART lines."""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        '<d:prop><d:getetag/><c:calendar-data/></d:prop>'
        '<c:filter><c:comp-filter name="VCALENDAR">'
        '<c:comp-filter name="VEVENT"/></c:comp-filter></c:filter>'
        '</c:calendar-query>'
    )
    status, data, _ = _request("REPORT", calendar_url, body, {"Depth": "1"})
    if status not in (207, 200):
        sys.exit(f"ERROR: calendar-query failed ({status}).\n{data[:500]}")
    import re
    events = []
    for block in re.split(r"BEGIN:VEVENT", data)[1:]:
        summ = re.search(r"SUMMARY:(.*)", block)
        uid = re.search(r"UID:(.*)", block)
        dt = re.search(r"DTSTART[^:]*:(.*)", block)
        events.append((
            (dt.group(1).strip() if dt else "?"),
            (summ.group(1).strip() if summ else "?"),
            (uid.group(1).strip() if uid else "?"),
        ))
    events.sort()
    return events

# --- CLI ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Write events to iCloud via CalDAV.")
    ap.add_argument("--self-test", action="store_true",
                    help="Verify auth + ensure the Assistant calendar exists.")
    ap.add_argument("--add", action="store_true", help="Create an event.")
    ap.add_argument("--list", action="store_true", help="List Assistant-calendar events.")
    ap.add_argument("--delete", action="store_true", help="Delete an event by --uid.")
    ap.add_argument("--summary")
    ap.add_argument("--start", help="YYYY-MM-DDTHH:MM (local) or YYYY-MM-DD with --all-day")
    ap.add_argument("--duration", type=int, default=15, help="minutes (timed events)")
    ap.add_argument("--desc", default="")
    ap.add_argument("--location", default="")
    ap.add_argument("--all-day", action="store_true")
    ap.add_argument("--alarm", type=int, default=None, help="minutes before to alert")
    ap.add_argument("--uid")
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    if args.self_test:
        print(f"Authenticating as {APPLE_ID} ...")
        url = ensure_assistant_calendar()
        print(f"OK. '{CAL_NAME}' calendar ready at:\n  {url}")
        return

    if args.add:
        if not (args.summary and args.start):
            sys.exit("--add requires --summary and --start")
        cal = ensure_assistant_calendar()
        uid, ics = build_vevent(
            args.summary, args.start, args.duration, args.desc, args.location,
            args.all_day, args.alarm, args.uid)
        url = put_event(cal, uid, ics)
        print(f"Created: {args.summary}\n  uid: {uid}\n  {url}")
        return

    if args.list:
        cal = ensure_assistant_calendar()
        for dt, summ, uid in list_events(cal, args.days):
            print(f"{dt}  {summ}  [{uid}]")
        return

    if args.delete:
        if not args.uid:
            sys.exit("--delete requires --uid")
        cal = ensure_assistant_calendar()
        code = delete_event(cal, args.uid)
        print(f"Deleted uid {args.uid} (status {code}).")
        return

    ap.print_help()

if __name__ == "__main__":
    main()
