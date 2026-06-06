#!/usr/bin/env python3
"""
RGM Lead Watcher
----------------
Runs on GitHub Actions every 15 minutes (cloud-hosted, works with your laptop off).
Each run it checks Facebook Messenger, Instagram DMs, and Gmail for NEW leads that
arrived in the last ~20 minutes, and sends one Telegram message per new lead.

It reuses the connections already set up in Composio, so the only secret needed is
your Composio API key (free). Everything is driven through Composio's REST API.

Env vars (set as GitHub repo Secrets):
  COMPOSIO_API_KEY   - required, your Composio API key
  TELEGRAM_CHAT_ID   - optional, defaults to the value below
"""

import os
import re
import sys
import json
import datetime as dt
import urllib.request
import urllib.error

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
COMPOSIO_BASE = "https://backend.composio.dev/api/v3"
API_KEY = os.environ.get("COMPOSIO_API_KEY", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8295197275").strip()

# How far back to look (minutes). Cron runs every 30 min; 35 gives a safety overlap
# so a lead is never missed if a run is slightly delayed. Worst case = a rare repeat.
LOOKBACK_MIN = int(os.environ.get("LOOKBACK_MIN", "35"))

# Connected-account IDs from Composio (the accounts we wired up).
FB_ACCOUNT = "facebook_cyanol-tret"
FB_PAGE_ID = "114357208375877"          # RGM page
IG_ACCOUNT = "instagram_hova-versus"    # @rgm_marketing_  (alias: rgm-business)
IG_SELF_USERNAME = "rgm_marketing_"
GMAIL_ACCOUNTS = [                      # the three ghiasi@ inboxes
    "gmail_glady-emmer",                # ghiasi@rghiasi.ca
    "gmail_seeder-soally",              # ghiasi@rohamresults.ca
    "gmail_michel-burrow",              # ghiasi@rohamresultsrg.ca
]
TELEGRAM_ACCOUNT = "telegram_butane-hatful"

NOW = dt.datetime.now(dt.timezone.utc)
CUTOFF = NOW - dt.timedelta(minutes=LOOKBACK_MIN)


# ----------------------------------------------------------------------------
# Composio REST helper
# ----------------------------------------------------------------------------
def composio_execute(tool_slug, arguments, connected_account_id=None):
    """POST /api/v3/tools/execute/{tool_slug} -> returns the tool's `data` dict."""
    url = f"{COMPOSIO_BASE}/tools/execute/{tool_slug}"
    body = {"arguments": arguments}
    if connected_account_id:
        body["connected_account_id"] = connected_account_id
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[ERROR] {tool_slug} HTTP {e.code}: {e.read().decode('utf-8')[:500]}")
        return {}
    except Exception as e:  # noqa
        print(f"[ERROR] {tool_slug}: {e}")
        return {}
    if not payload.get("successful", True):
        print(f"[WARN] {tool_slug} not successful: {str(payload.get('error'))[:300]}")
    return payload.get("data", {}) or {}


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def parse_ts(value):
    """Parse Graph (...+0000) / Gmail (...Z) timestamps -> aware datetime, or None."""
    if not value:
        return None
    v = str(value).strip()
    if v.isdigit():  # epoch ms
        return dt.datetime.fromtimestamp(int(v) / 1000, dt.timezone.utc)
    v = v.replace("Z", "+00:00")
    # +0000 -> +00:00
    m = re.search(r"([+-]\d{2})(\d{2})$", v)
    if m:
        v = v[: m.start()] + m.group(1) + ":" + m.group(2)
    try:
        d = dt.datetime.fromisoformat(v)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except Exception:
        return None


LEAD_FIELDS = {
    "name": re.compile(r"Full name:\s*(.+)", re.I),
    "company": re.compile(r"Company name:\s*(.+)", re.I),
    "phone": re.compile(r"Phone number:\s*(.+)", re.I),
    "city": re.compile(r"City:\s*(.+)", re.I),
    "email": re.compile(r"Email:\s*(.+)", re.I),
}


def parse_lead_form(text):
    """Return a dict of lead fields if `text` looks like a lead form, else None."""
    if not text or "phone number:" not in text.lower():
        return None
    out = {}
    for key, rx in LEAD_FIELDS.items():
        m = rx.search(text)
        if m:
            out[key] = m.group(1).strip()
    return out or None


def maps_link(company, city):
    q = " ".join(x for x in [company, city] if x).replace(" ", "+")
    return f"https://www.google.com/maps/search/?api=1&query={q}"


def send_telegram(source, lead):
    name = lead.get("name", "?")
    company = lead.get("company", "")
    city = lead.get("city", "")
    phone = lead.get("phone", "")
    who = " — ".join(x for x in [company, city] if x)
    text = (
        f"NEW LEAD ({source})\n"
        f"{name}" + (f"\n{who}" if who else "") + "\n"
        f"Phone: {phone}\n"
        f"GBP check: {maps_link(company, city)}"
    )
    composio_execute(
        "TELEGRAM_SEND_MESSAGE",
        {"chat_id": TELEGRAM_CHAT_ID, "text": text},
        TELEGRAM_ACCOUNT,
    )
    print(f"[SENT] {source}: {name} / {company} / {phone}")


# ----------------------------------------------------------------------------
# Source pollers  (each returns a list of (source, lead, dedup_id))
# ----------------------------------------------------------------------------
def poll_facebook():
    leads = []
    data = composio_execute(
        "FACEBOOK_GET_PAGE_CONVERSATIONS",
        {"page_id": FB_PAGE_ID, "fields": "id,updated_time", "limit": 25},
        FB_ACCOUNT,
    )
    for conv in data.get("data", []):
        if (parse_ts(conv.get("updated_time")) or NOW) < CUTOFF:
            continue
        msgs = composio_execute(
            "FACEBOOK_GET_CONVERSATION_MESSAGES",
            {"page_id": FB_PAGE_ID, "conversation_id": conv["id"],
             "fields": "id,created_time,from,message", "limit": 15},
            FB_ACCOUNT,
        )
        for m in msgs.get("data", []):
            if (m.get("from") or {}).get("id") == FB_PAGE_ID:
                continue  # our own message
            if (parse_ts(m.get("created_time")) or CUTOFF - dt.timedelta(days=1)) < CUTOFF:
                continue
            lead = parse_lead_form(m.get("message", ""))
            if lead:
                leads.append(("Facebook", lead, m.get("id")))
    return leads


def poll_instagram():
    leads = []
    data = composio_execute(
        "INSTAGRAM_LIST_ALL_CONVERSATIONS", {"limit": 50}, IG_ACCOUNT
    )
    for conv in data.get("data", []):
        if (parse_ts(conv.get("updated_time")) or NOW) < CUTOFF:
            continue
        msgs = composio_execute(
            "INSTAGRAM_LIST_ALL_MESSAGES",
            {"conversation_id": conv["id"], "limit": 15},
            IG_ACCOUNT,
        )
        for m in msgs.get("data", []):
            if (m.get("from") or {}).get("username") == IG_SELF_USERNAME:
                continue
            if (parse_ts(m.get("created_time")) or CUTOFF - dt.timedelta(days=1)) < CUTOFF:
                continue
            lead = parse_lead_form(m.get("message", ""))
            if lead:
                leads.append(("Instagram", lead, m.get("id")))
    return leads


def poll_gmail():
    leads = []
    for acct in GMAIL_ACCOUNTS:
        data = composio_execute(
            "GMAIL_FETCH_EMAILS",
            {"query": 'subject:"Top 10 in" newer_than:1h', "label_ids": ["INBOX"],
             "max_results": 15, "verbose": True},
            acct,
        )
        for msg in data.get("messages", []) or []:
            ts = parse_ts(msg.get("messageTimestamp"))
            if ts is None or ts < CUTOFF:
                continue
            sender = (msg.get("sender") or "")
            if "roham" in sender.lower() or "ghiasi" in sender.lower():
                continue  # our own sent mail
            lead = {
                "name": sender,
                "company": "",
                "city": "",
                "phone": "(see email)",
            }
            # subject like "Re: Top 10 in Toronto" -> grab the city
            subj = msg.get("subject", "")
            mcity = re.search(r"Top 10 in\s+(.+)", subj, re.I)
            if mcity:
                lead["city"] = mcity.group(1).strip()
            leads.append(("Gmail reply", lead, msg.get("messageId")))
    return leads


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def selftest():
    """Ping every connected account + send a Telegram confirmation.
    Proves the full chain works without needing a real lead to have just arrived."""
    print("[SELFTEST] verifying every connection...")
    results = []

    fb = composio_execute(
        "FACEBOOK_GET_PAGE_CONVERSATIONS",
        {"page_id": FB_PAGE_ID, "fields": "id,updated_time", "limit": 1}, FB_ACCOUNT)
    results.append(("Facebook", "data" in fb, len(fb.get("data", []))))

    ig = composio_execute("INSTAGRAM_LIST_ALL_CONVERSATIONS", {"limit": 1}, IG_ACCOUNT)
    results.append(("Instagram", "data" in ig, len(ig.get("data", []))))

    for acct in GMAIL_ACCOUNTS:
        gm = composio_execute(
            "GMAIL_FETCH_EMAILS",
            {"query": 'subject:"Top 10 in"', "label_ids": ["INBOX"],
             "max_results": 1, "verbose": False}, acct)
        results.append((f"Gmail {acct}", "messages" in gm or "nextPageToken" in gm, 0))

    lines = [f"{'OK ' if ok else 'FAIL'}  {name}" for name, ok, _ in results]
    all_ok = all(ok for _, ok, _ in results)
    for ln in lines:
        print("  " + ln)

    composio_execute(
        "TELEGRAM_SEND_MESSAGE",
        {"chat_id": TELEGRAM_CHAT_ID,
         "text": "RGM Lead Watcher - self-test\n" + "\n".join(lines) +
                 ("\n\nAll systems go. Live alerts are on." if all_ok
                  else "\n\nSome connection failed - check the log.")},
        TELEGRAM_ACCOUNT)
    print("[SELFTEST] " + ("PASSED" if all_ok else "FAILED — see above"))
    sys.exit(0 if all_ok else 1)


def main():
    if not API_KEY:
        print("[FATAL] COMPOSIO_API_KEY is not set.")
        sys.exit(1)

    if "--selftest" in sys.argv:
        selftest()

    print(f"[RUN] {NOW.isoformat()}  lookback={LOOKBACK_MIN}m  cutoff={CUTOFF.isoformat()}")

    all_leads = []
    for fn in (poll_facebook, poll_instagram, poll_gmail):
        try:
            all_leads.extend(fn())
        except Exception as e:  # noqa
            print(f"[ERROR] {fn.__name__}: {e}")

    # de-dupe within this run
    seen = set()
    new = 0
    for source, lead, did in all_leads:
        key = did or json.dumps(lead, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        send_telegram(source, lead)
        new += 1

    print(f"[DONE] {new} new lead(s) sent." if new else "[DONE] No new leads.")


if __name__ == "__main__":
    main()
