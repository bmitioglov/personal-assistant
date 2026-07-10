#!/usr/bin/env python3
"""
Inbox cleanup for your Gmail (via IMAP — full access through the app password).

Two jobs:
  1. Backlog reset  — mark long-unread mail as read (one bulk IMAP command).
  2. Declutter      — for known bulk/noise senders: mark read + archive (remove
                      from Inbox; stays in All Mail) and optionally apply a label.

Everything is reversible: "read" can be unmarked, "archived" mail lives in All Mail.
Nothing is ever deleted/trashed.

Usage:
  python3 inbox_cleanup.py                 # read-only REPORT (default; changes nothing)
  python3 inbox_cleanup.py --report
  python3 inbox_cleanup.py --backlog-reset [--days 90]   # mark old unread as read
  python3 inbox_cleanup.py --declutter                   # read+archive+label noise
  python3 inbox_cleanup.py --declutter --dry-run         # show what declutter WOULD do
Add --yes to skip the confirmation prompt (for the unattended morning run).
"""

import re
import sys
import ssl
import imaplib
import datetime as dt

import briefing  # reuse load_config()

IMAP_HOST = "imap.gmail.com"

# --------------------------------------------------------------------------- #
# Noise spec: (label, [IMAP search criteria]). A message matching any criteria
# in a bucket is decluttered (marked read + archived) and filed under `label`.
# Criteria are raw IMAP SEARCH terms. Keep these to CLEARLY bulk/automated
# senders — never a real person.
# --------------------------------------------------------------------------- #
NOISE = [
    ("Recruiters", [
        # If recruiter spam floods a specific alias/tag you use, target it here, e.g.:
        # 'TO "yourname+jobs@gmail.com"',
    ]),
    # NOTE: some bulk senders (e.g. real-estate alerts, deal alerts) are worth reading
    # occasionally rather than blind-archiving. Leave those out of NOISE so they flow to
    # the content-aware triage in briefing_prompt.md (STEP 2.5) instead, which reads each
    # one and only surfaces what's actually notable.
    ("LinkedIn", [
        'FROM "linkedin.com"',
    ]),
    ("Finance alerts", [
        # 'FROM "noreply@yourbrokerage.com"',
    ]),
    ("Promotions", [
        # Add the bulk marketing senders that clutter your own inbox, e.g.:
        # 'FROM "e.somestore.com"',
        # 'FROM "news.somepromo.com"',
    ]),
]


def connect(cfg, readonly):
    ctx = ssl.create_default_context()
    M = imaplib.IMAP4_SSL(IMAP_HOST, 993, ssl_context=ctx)
    M.login(cfg["GMAIL_ADDRESS"], cfg["GMAIL_APP_PASSWORD"])
    M.select("INBOX", readonly=readonly)
    return M


def uid_search(M, *criteria):
    typ, data = M.uid("SEARCH", None, *criteria)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def chunked(seq, n=400):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def imap_date(days_ago):
    d = dt.date.today() - dt.timedelta(days=days_ago)
    return d.strftime("%d-%b-%Y")


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def report(cfg):
    M = connect(cfg, readonly=True)
    try:
        total = len(uid_search(M, "UNSEEN"))
        old = len(uid_search(M, "UNSEEN", "BEFORE", imap_date(90)))
        print("INBOX unread total:      %d" % total)
        print("  unread > 90 days old:  %d  (backlog-reset target)" % old)
        print("  unread <= 90 days:     %d" % (total - old))
        print("\nNoise buckets (unread in inbox):")
        bucket_total = 0
        seen = set()
        for label, crits in NOISE:
            uids = set()
            for c in crits:
                uids.update(uid_search(M, "UNSEEN", *c.split(" ", 1)))
            fresh = uids - seen
            seen |= uids
            bucket_total += len(fresh)
            print("  %-16s %4d" % (label + ":", len(fresh)))
        print("  %-16s %4d" % ("— declutter total:", bucket_total))
    finally:
        M.logout()


# --------------------------------------------------------------------------- #
# Backlog reset — mark old unread as read (bulk)
# --------------------------------------------------------------------------- #
def backlog_reset(cfg, days, assume_yes):
    M = connect(cfg, readonly=True)
    uids = uid_search(M, "UNSEEN", "BEFORE", imap_date(days))
    M.logout()
    n = len(uids)
    print("Found %d unread messages older than %d days." % (n, days))
    if n == 0:
        return
    if not assume_yes and not _confirm("Mark all %d as READ (reversible, not deleted)?" % n):
        print("Aborted.")
        return
    M = connect(cfg, readonly=False)
    try:
        done = 0
        for batch in chunked(uids):
            M.uid("STORE", b",".join(batch), "+FLAGS", "(\\Seen)")
            done += len(batch)
            print("  marked %d/%d read" % (done, n))
    finally:
        M.logout()
    print("Backlog reset complete: %d marked read." % n)


# --------------------------------------------------------------------------- #
# Declutter — mark read + archive noise (optionally label)
# --------------------------------------------------------------------------- #
def declutter(cfg, dry_run, assume_yes):
    # First, gather per-bucket UID sets (read-only).
    M = connect(cfg, readonly=True)
    plan = []
    seen = set()
    for label, crits in NOISE:
        uids = set()
        for c in crits:
            uids.update(uid_search(M, "UNSEEN", *c.split(" ", 1)))
        # de-dupe across buckets by UID string
        uids = {u for u in uids if u not in seen}
        seen |= uids
        plan.append((label, sorted(uids)))
    M.logout()

    total = sum(len(u) for _, u in plan)
    print("Declutter plan (unread inbox mail from bulk senders):")
    for label, uids in plan:
        print("  %-16s %4d  → mark read + archive + label '%s'" % (label + ":", len(uids), label))
    print("  total: %d" % total)
    if dry_run:
        print("\n(dry-run — nothing changed)")
        return
    if total == 0:
        return
    if not assume_yes and not _confirm("Apply to %d messages? (reversible; archived stays in All Mail)" % total):
        print("Aborted.")
        return

    M = connect(cfg, readonly=False)
    try:
        for label, uids in plan:
            if not uids:
                continue
            # Ensure the label exists (Gmail maps an IMAP folder to a label).
            try:
                M.create('"%s"' % label)
            except Exception:
                pass
            for batch in chunked(uids):
                idset = b",".join(batch)
                # apply label by copying into the label "folder"
                try:
                    M.uid("COPY", idset, '"%s"' % label)
                except Exception:
                    pass
                # mark read + remove from inbox (archive)
                M.uid("STORE", idset, "+FLAGS", "(\\Seen \\Deleted)")
            M.expunge()  # finalizes the INBOX removal for this pass
            print("  %s: %d decluttered" % (label, len(uids)))
    finally:
        M.logout()
    print("Declutter complete: %d messages archived + read." % total)


def _clean_snippet(payload):
    """Best-effort readable text from a raw (often quoted-printable/HTML) body part."""
    import quopri
    try:
        text = quopri.decodestring(payload).decode("utf-8", "replace")
    except Exception:
        text = payload.decode("utf-8", "replace")
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)         # strip tags
    text = re.sub(r"&[a-z#0-9]+;", " ", text)          # strip entities
    text = re.sub(r"\s+", " ", text).strip()
    return text[:200]


def list_unread(cfg, limit=0):
    """Export unread inbox mail as JSON (uid, date, from, subject, snippet) for
    content-aware triage by the reasoning layer."""
    import json
    import email as emailmod
    from email.header import decode_header, make_header
    from email.utils import parseaddr
    M = connect(cfg, readonly=True)
    out = []
    try:
        uids = uid_search(M, "UNSEEN")
        if limit:
            uids = uids[-limit:]  # most recent
        for batch in chunked(uids, 200):
            typ, data = M.uid("FETCH", b",".join(batch),
                              "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] BODY.PEEK[1]<0.400>)")
            if typ != "OK":
                continue
            # responses interleave; walk tuples
            cur_uid = None
            for item in data:
                if isinstance(item, tuple):
                    raw = item[0].decode("ascii", "replace") if item[0] else ""
                    m = re.search(r"UID (\d+)", raw)
                    if m:
                        cur_uid = m.group(1)
                    payload = item[1] or b""
                    if b"Subject" in payload or b"From" in payload:
                        hdr = emailmod.message_from_bytes(payload)
                        frm = str(make_header(decode_header(hdr.get("From", ""))))
                        subj = str(make_header(decode_header(hdr.get("Subject", ""))))
                        name, addr = parseaddr(frm)
                        list_unread._pending = {
                            "uid": cur_uid, "from": name or addr, "addr": addr.lower(),
                            "subject": subj, "date": hdr.get("Date", ""), "snippet": "",
                        }
                    else:
                        # this tuple is the body snippet for the pending message
                        if getattr(list_unread, "_pending", None):
                            list_unread._pending["snippet"] = _clean_snippet(payload)
                            out.append(list_unread._pending)
                            list_unread._pending = None
        # flush any pending without a body part
        if getattr(list_unread, "_pending", None):
            out.append(list_unread._pending)
            list_unread._pending = None
    finally:
        M.logout()
    print(json.dumps(out, ensure_ascii=False))


def archive_uids(cfg, uids, label=None):
    """Mark read + archive (+optional label) a specific list of UIDs."""
    if not uids:
        print("no uids given")
        return
    M = connect(cfg, readonly=False)
    try:
        if label:
            try:
                M.create('"%s"' % label)
            except Exception:
                pass
        n = 0
        for batch in chunked([u.encode() if isinstance(u, str) else u for u in uids]):
            idset = b",".join(batch)
            if label:
                try:
                    M.uid("COPY", idset, '"%s"' % label)
                except Exception:
                    pass
            M.uid("STORE", idset, "+FLAGS", "(\\Seen \\Deleted)")
            n += len(batch)
        M.expunge()
        print("archived + read: %d" % n)
    finally:
        M.logout()


def unsub_report(cfg, days=60, min_count=3, as_json=False):
    """Find recurring bulk senders (those with a List-Unsubscribe header) over the
    last `days`, ranked by volume, with their unsubscribe link. These are the best
    'unsubscribe' candidates. Scans All Mail so auto-archived promo mail is included."""
    import json
    import email as emailmod
    from email.header import decode_header, make_header
    from email.utils import parseaddr
    from collections import defaultdict
    ctx = ssl.create_default_context()
    M = imaplib.IMAP4_SSL(IMAP_HOST, 993, ssl_context=ctx)
    M.login(cfg["GMAIL_ADDRESS"], cfg["GMAIL_APP_PASSWORD"])
    stats = defaultdict(lambda: {"count": 0, "name": "", "subject": "", "unsub": ""})
    try:
        # All Mail = every received message (incl. archived); '\\All' via Gmail.
        typ, _ = M.select('"[Gmail]/All Mail"', readonly=True)
        if typ != "OK":
            M.select("INBOX", readonly=True)
        typ, data = M.uid("SEARCH", None, "SINCE", imap_date(days))
        uids = data[0].split() if data and data[0] else []
        for batch in chunked(uids, 400):
            typ, resp = M.uid("FETCH", b",".join(batch),
                              "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT LIST-UNSUBSCRIBE)])")
            if typ != "OK":
                continue
            for item in resp:
                if not isinstance(item, tuple):
                    continue
                hdr = emailmod.message_from_bytes(item[1])
                unsub = hdr.get("List-Unsubscribe", "")
                if not unsub:
                    continue  # only mailing-list mail is an unsubscribe candidate
                try:
                    unsub = str(make_header(decode_header(unsub)))
                except Exception:
                    pass
                frm = str(make_header(decode_header(hdr.get("From", ""))))
                name, addr = parseaddr(frm)
                addr = addr.lower()
                if not addr:
                    continue
                s = stats[addr]
                s["count"] += 1
                s["name"] = name or addr
                if not s["subject"]:
                    s["subject"] = str(make_header(decode_header(hdr.get("Subject", ""))))[:70]
                if not s["unsub"]:
                    s["unsub"] = _parse_unsub(unsub)
    finally:
        M.logout()
    ranked = sorted(
        ((a, v) for a, v in stats.items() if v["count"] >= min_count),
        key=lambda kv: kv[1]["count"], reverse=True)
    if as_json:
        print(json.dumps([
            {"sender": v["name"], "addr": a, "count": v["count"],
             "example": v["subject"], "unsubscribe": v["unsub"]}
            for a, v in ranked], ensure_ascii=False))
        return
    print("Unsubscribe candidates — recurring senders in the last %d days" % days)
    print("(count = messages; only senders offering an unsubscribe link shown)\n")
    for a, v in ranked:
        print("  %3d  %-32s  %s" % (v["count"], a[:32], v["unsub"][:60]))


def _parse_unsub(header):
    """Extract the best unsubscribe target from a List-Unsubscribe header."""
    urls = re.findall(r"<([^>]+)>", header) or [header]
    https = [u for u in urls if u.lower().startswith("http")]
    mailto = [u for u in urls if u.lower().startswith("mailto")]
    return (https or mailto or urls)[0].strip()


def analyze_top(cfg, limit=30):
    """Tally remaining unread inbox mail by sender domain (read-only)."""
    import email as emailmod
    from email.utils import parseaddr
    from collections import Counter
    M = connect(cfg, readonly=True)
    try:
        uids = uid_search(M, "UNSEEN")
        dom = Counter()
        addr = Counter()
        for batch in chunked(uids, 300):
            typ, data = M.uid("FETCH", b",".join(batch),
                              "(BODY.PEEK[HEADER.FIELDS (FROM)])")
            if typ != "OK":
                continue
            for item in data:
                if not isinstance(item, tuple):
                    continue
                hdr = emailmod.message_from_bytes(item[1])
                _, a = parseaddr(hdr.get("From", ""))
                a = a.lower()
                addr[a] += 1
                if "@" in a:
                    dom[a.split("@", 1)[1]] += 1
        print("Top sender DOMAINS among %d remaining unread:" % len(uids))
        for d, c in dom.most_common(limit):
            print("  %4d  %s" % (c, d))
    finally:
        M.logout()


def _confirm(msg):
    try:
        return input(msg + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main():
    cfg = briefing.load_config()
    args = sys.argv[1:]
    yes = "--yes" in args
    dry = "--dry-run" in args
    if "--backlog-reset" in args:
        days = 90
        if "--days" in args:
            days = int(args[args.index("--days") + 1])
        backlog_reset(cfg, days, yes)
    elif "--declutter" in args:
        declutter(cfg, dry, yes)
    elif "--top" in args:
        analyze_top(cfg)
    elif "--unsub" in args:
        days = 60
        if "--days" in args:
            days = int(args[args.index("--days") + 1])
        unsub_report(cfg, days, as_json=("--json" in args))
    elif "--list-unread" in args:
        limit = 0
        if "--limit" in args:
            limit = int(args[args.index("--limit") + 1])
        list_unread(cfg, limit)
    elif "--archive-file" in args:
        path = args[args.index("--archive-file") + 1]
        label = None
        if "--label" in args:
            label = args[args.index("--label") + 1]
        with open(path) as f:
            uids = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        archive_uids(cfg, uids, label)
    else:
        report(cfg)


if __name__ == "__main__":
    main()
