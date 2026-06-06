#!/usr/bin/env python3
"""
RGM Lead Watcher
----------------
Runs on GitHub Actions every 15 minutes (cloud-hosted, works with your laptop off).
Each run it checks for NEW leads from the last ~20 minutes and sends ONE Telegram
message per new lead. Silent when there's nothing new.

Lead sources:
  - Facebook Messenger (RGM page)  - lead-form DMs
  - Instagram DMs (@rgm_marketing_) - lead-form DMs
  - Wix website contact form -> no-reply@crm.wix.com -> rohamghiasicw@gmail.com

Reuses the Composio connections via the MCP endpoint + your CONSUMER key (ck_...).
Secrets:
  COMPOSIO_CONSUMER_KEY  - required (ck_...)
  TELEGRAM_CHAT_ID       - optional (defaults below)
"""

import os
import re
import sys
import json
import base64
import datetime as dt
import urllib.request
import urllib.error

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
MCP_URL = "https://connect.composio.dev/mcp"
CONSUMER_KEY = os.environ.get("COMPOSIO_CONSUMER_KEY", "").strip()
TELEGRAM_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID") or "8295197275")
LOOKBACK_MIN = int(os.environ.get("LOOKBACK_MIN", "20"))

FB_PAGE_ID = "114357208375877"          # RGM page
IG_ACCOUNT = "rgm-business"             # @rgm_marketing_
IG_SELF_USERNAME = "rgm_marketing_"
WIX_INBOX = "gmail_incog-wur"   # rohamghiasicw@gmail.com - Wix website-form leads land here

# The agency inboxes that SEND cold outreach. A human reply landing here = a lead.
COLD_INBOXES = [
    "gmail_seeder-soally",  # ghiasi@rohamresults.ca
    "gmail_tonant-reflow",  # roham@rghiasi.ca
    "gmail_michel-burrow",  # ghiasi@rohamresultsrg.ca
    "gmail_glady-emmer",    # ghiasi@rghiasi.ca
    "gmail_affect-unique",  # rg@rohamresults.ca
    "gmail_yahgan-ganoid",  # rg@rohamresultsrg.ca
]
# Your cold-outreach campaign subject line(s). A reply to one of these = a real
# prospect - anchoring on this keeps out the cold spam these inboxes also receive.
# Add more subjects here as you run new campaigns.
CAMPAIGN_SUBJECTS = ["Top 10 in"]

# Senders that are never a real reply (automation, your own domains, big platforms).
EXCLUDE_SENDERS = ("noreply", "no-reply", "donotreply", "notification", "mailer-daemon",
                   "postmaster", "rohamresults", "rghiasi", "ghiasi@", "roham@",
                   "google.com", "facebook", "wix.com", "paypal", "github",
                   "atlassian", "linkedin", "intuit", "glassdoor", "calendly")

NOW = dt.datetime.now(dt.timezone.utc)
CUTOFF = NOW - dt.timedelta(minutes=LOOKBACK_MIN)
GMAIL_FRESH_H = max(1, (LOOKBACK_MIN + 59) // 60 + 1)  # Gmail search window, tracks lookback
DRY_RUN = False


# ----------------------------------------------------------------------------
# Minimal Composio-MCP client (Streamable HTTP)
# ----------------------------------------------------------------------------
class MCP:
    def __init__(self, url, key):
        self.url = url
        self.headers = {"Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        "X-Consumer-API-Key": key}
        self.session = None
        self._id = 0
        self._handshake()

    def _post(self, payload):
        h = dict(self.headers)
        if self.session:
            h["mcp-session-id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     headers=h, method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=90)
        except urllib.error.HTTPError as e:
            print(f"[MCP HTTP {e.code}] {e.read().decode()[:300]}")
            return None, {}
        body = None
        for line in r.read().decode().splitlines():
            if line.startswith("data:"):
                try:
                    body = json.loads(line[5:].strip())
                except Exception:
                    pass
        return body, dict(r.headers)

    def _nid(self):
        self._id += 1
        return self._id

    def _handshake(self):
        _, hdrs = self._post({"jsonrpc": "2.0", "id": self._nid(), "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "rgm-lead-watcher", "version": "2"}}})
        self.session = hdrs.get("mcp-session-id") or hdrs.get("Mcp-Session-Id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def execute(self, tool_slug, arguments, account=None):
        item = {"tool_slug": tool_slug, "arguments": arguments}
        if account:
            item["account"] = account
        res, _ = self._post({"jsonrpc": "2.0", "id": self._nid(), "method": "tools/call",
            "params": {"name": "COMPOSIO_MULTI_EXECUTE_TOOL", "arguments": {
                "thought": "lead poll", "current_step": "POLL",
                "sync_response_to_workbench": False, "tools": [item]}}})
        try:
            payload = json.loads(res["result"]["content"][0]["text"])
            r0 = payload["data"]["results"][0]["response"]
            if not r0.get("successful", True):
                print(f"[WARN] {tool_slug}: {str(r0.get('error'))[:200]}")
            return r0.get("data", {}) or {}
        except Exception as e:
            print(f"[ERROR] {tool_slug}: {e} | {json.dumps(res)[:300] if res else 'no response'}")
            return {}


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def parse_ts(value):
    if not value:
        return None
    v = str(value).strip()
    if v.isdigit():
        return dt.datetime.fromtimestamp(int(v) / 1000, dt.timezone.utc)
    v = v.replace("Z", "+00:00")
    m = re.search(r"([+-]\d{2})(\d{2})$", v)
    if m:
        v = v[: m.start()] + m.group(1) + ":" + m.group(2)
    try:
        d = dt.datetime.fromisoformat(v)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


# --- FB/IG lead form ("Phone number:" style) -------------------------------
FORM_FIELDS = {"name": r"Full name:\s*(.+)", "company": r"Company name:\s*(.+)",
               "phone": r"Phone number:\s*(.+)", "city": r"City:\s*(.+)"}


def parse_dm_form(text):
    if not text or "phone number:" not in text.lower():
        return None
    out = {}
    for k, rx in FORM_FIELDS.items():
        m = re.search(rx, text, re.I)
        if m:
            out[k] = m.group(1).strip()
    return out or None


# --- Wix contact-form email ------------------------------------------------
WIX_LABELS = {"first name": "first", "last name": "last", "name": "name",
              "business email": "email", "email": "email",
              "company name": "company", "company": "company",
              "phone": "phone", "phone number": "phone",
              "short answer": "note", "message": "note", "subject": "note"}


_WIX_STOPS = ("First name|Last name|Business Email|Email|Company name|Company|"
              "Phone number|Phone|Short answer|Message|Subject")


def parse_wix(snippet):
    """Parse the inline Gmail snippet of a Wix contact-form notification.
    Reliable for name/email/company; phone shows up when it fits in the snippet."""
    if not snippet:
        return None

    def grab(label):
        m = re.search(label + r"\s*:\s*(.+?)(?=\s+(?:" + _WIX_STOPS + r")\s*:|$)", snippet, re.I)
        return m.group(1).strip() if m else ""

    first = grab("First name") or grab("Name")
    last = grab("Last name")
    email = grab("Business Email") or grab("Email")
    em = re.search(r"[\w.+-]+@[\w.-]+\.\w+", email)   # keep just the address
    email = em.group(0) if em else email
    company = grab("Company name") or grab("Company")
    phone = grab("Phone number") or grab("Phone")
    if not (first or email or phone):
        return None
    name = " ".join(x for x in [first, last] if x) or "(no name)"
    return {"name": name, "company": company, "city": "",
            "phone": phone, "email": email, "note": ""}


def maps_link(company, city):
    q = " ".join(x for x in [company, city] if x).replace(" ", "+")
    return f"https://www.google.com/maps/search/?api=1&query={q}"


def send_telegram(mcp, source, lead):
    who = " - ".join(x for x in [lead.get("name"), lead.get("company")] if x) or lead.get("name", "(lead)")
    parts = [f"NEW LEAD ({source})", who]
    if lead.get("subject"):
        parts.append('"' + lead["subject"] + '"')
    if lead.get("phone"):
        parts.append("Phone: " + lead["phone"])
    if lead.get("email"):
        parts.append("Email: " + lead["email"])
    if lead.get("note"):
        parts.append(lead["note"])
    if lead.get("company") or lead.get("city"):
        parts.append("GBP check: " + maps_link(lead.get("company", ""), lead.get("city", "")))
    if lead.get("link"):
        parts.append("Open email (full details/phone): " + lead["link"])
    if DRY_RUN:
        print(f"[WOULD ALERT] {source}: {who} | {lead.get('phone','')} | {lead.get('email','')}")
        return
    mcp.execute("TELEGRAM_SEND_MESSAGE", {"chat_id": TELEGRAM_CHAT_ID, "text": "\n".join(parts)})
    print(f"[SENT] {source}: {who}")


# ----------------------------------------------------------------------------
# Source pollers
# ----------------------------------------------------------------------------
OLD = dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def poll_facebook(mcp):
    leads = []
    data = mcp.execute("FACEBOOK_GET_PAGE_CONVERSATIONS",
                       {"page_id": FB_PAGE_ID, "fields": "id,updated_time", "limit": 25})
    for conv in data.get("data", []):
        if (parse_ts(conv.get("updated_time")) or NOW) < CUTOFF:
            continue
        msgs = mcp.execute("FACEBOOK_GET_CONVERSATION_MESSAGES",
                           {"page_id": FB_PAGE_ID, "conversation_id": conv["id"],
                            "fields": "id,created_time,from,message", "limit": 15})
        for m in msgs.get("data", []):
            if (m.get("from") or {}).get("id") == FB_PAGE_ID:
                continue
            if (parse_ts(m.get("created_time")) or OLD) < CUTOFF:
                continue
            lead = parse_dm_form(m.get("message", ""))
            if lead:
                leads.append(("Facebook", lead, m.get("id")))
    return leads


def poll_instagram(mcp):
    leads = []
    data = mcp.execute("INSTAGRAM_LIST_ALL_CONVERSATIONS", {"limit": 50}, IG_ACCOUNT)
    for conv in data.get("data", []):
        if (parse_ts(conv.get("updated_time")) or NOW) < CUTOFF:
            continue
        msgs = mcp.execute("INSTAGRAM_LIST_ALL_MESSAGES",
                           {"conversation_id": conv["id"], "limit": 15}, IG_ACCOUNT)
        for m in msgs.get("data", []):
            if (m.get("from") or {}).get("username") == IG_SELF_USERNAME:
                continue
            if (parse_ts(m.get("created_time")) or OLD) < CUTOFF:
                continue
            lead = parse_dm_form(m.get("message", ""))
            if lead:
                leads.append(("Instagram", lead, m.get("id")))
    return leads


def poll_wix(mcp):
    """Website contact-form leads: no-reply@crm.wix.com -> rohamghiasicw@gmail.com."""
    leads = []
    listing = mcp.execute("GMAIL_FETCH_EMAILS",
                          {"query": f"from:crm.wix.com newer_than:{GMAIL_FRESH_H}h",
                           "label_ids": ["INBOX"], "max_results": 15, "verbose": True}, WIX_INBOX)
    for msg in listing.get("messages", []) or []:
        if (parse_ts(msg.get("messageTimestamp") or msg.get("internalDate")) or OLD) < CUTOFF:
            continue
        snippet = (msg.get("preview") or {}).get("body") or msg.get("messageText", "")
        lead = parse_wix(snippet)
        if lead:
            lead["link"] = msg.get("display_url", "")
            leads.append(("Website form", lead, msg.get("messageId")))
    return leads


def parse_cold_reply(msg):
    sender = msg.get("sender", "")
    m = re.match(r'\s*"?([^"<]*?)"?\s*<([^>]+)>', sender)
    name = (m.group(1).strip() if m else sender).strip() or sender
    email = m.group(2).strip() if m else ""
    return {"name": name or email or "(reply)", "company": "", "city": "", "phone": "",
            "email": email, "subject": msg.get("subject", ""),
            "note": ((msg.get("preview") or {}).get("body") or "")[:180],
            "link": msg.get("display_url", "")}


def poll_cold(mcp):
    """A reply to one of YOUR outreach campaigns, landing in a cold-outreach inbox = a lead."""
    leads = []
    subj = " OR ".join(f'subject:"{s}"' for s in CAMPAIGN_SUBJECTS)
    query = f"in:inbox newer_than:{GMAIL_FRESH_H}h ({subj})"
    for acct in COLD_INBOXES:
        listing = mcp.execute("GMAIL_FETCH_EMAILS",
                              {"query": query, "label_ids": ["INBOX"], "max_results": 15, "verbose": True}, acct)
        for msg in listing.get("messages", []) or []:
            if (parse_ts(msg.get("messageTimestamp") or msg.get("internalDate")) or OLD) < CUTOFF:
                continue
            if any(x in msg.get("sender", "").lower() for x in EXCLUDE_SENDERS):
                continue
            leads.append(("Cold-outreach reply", parse_cold_reply(msg), msg.get("messageId")))
    return leads


# ----------------------------------------------------------------------------
# Self-test + main
# ----------------------------------------------------------------------------
def selftest(mcp):
    print("[SELFTEST] verifying connections...")
    checks = []
    fb = mcp.execute("FACEBOOK_GET_PAGE_CONVERSATIONS",
                     {"page_id": FB_PAGE_ID, "fields": "id,updated_time", "limit": 1})
    checks.append(("Facebook", "data" in fb))
    ig = mcp.execute("INSTAGRAM_LIST_ALL_CONVERSATIONS", {"limit": 1}, IG_ACCOUNT)
    checks.append(("Instagram", "data" in ig))
    wx = mcp.execute("GMAIL_FETCH_EMAILS",
                     {"query": "from:crm.wix.com", "label_ids": ["INBOX"], "max_results": 1, "verbose": False},
                     WIX_INBOX)
    checks.append(("Wix form inbox", ("messages" in wx or "nextPageToken" in wx)))
    cold_ok = 0
    for acct in COLD_INBOXES:
        c = mcp.execute("GMAIL_FETCH_EMAILS",
                        {"query": "in:inbox", "label_ids": ["INBOX"], "max_results": 1, "verbose": False}, acct)
        cold_ok += 1 if ("messages" in c or "nextPageToken" in c) else 0
    checks.append((f"Cold-outreach inboxes ({cold_ok}/{len(COLD_INBOXES)})", cold_ok == len(COLD_INBOXES)))
    lines = [f"{'OK  ' if ok else 'FAIL'} {n}" for n, ok in checks]
    all_ok = all(ok for _, ok in checks)
    for ln in lines:
        print("  " + ln)
    mcp.execute("TELEGRAM_SEND_MESSAGE", {"chat_id": TELEGRAM_CHAT_ID,
        "text": "RGM Lead Watcher - self-test\n" + "\n".join(lines)
                + ("\n\nWatching: FB DMs, IG DMs, Wix website form, + cold-outreach replies in 6 inboxes."
                   " Only texts on a real new lead." if all_ok else "\n\nSomething failed - check the log.")})
    print("[SELFTEST] " + ("PASSED" if all_ok else "FAILED"))
    sys.exit(0 if all_ok else 1)


def main():
    if not CONSUMER_KEY:
        print("[FATAL] COMPOSIO_CONSUMER_KEY is not set.")
        sys.exit(1)
    mcp = MCP(MCP_URL, CONSUMER_KEY)

    if "--selftest" in sys.argv:
        selftest(mcp)

    global DRY_RUN
    if "--dryrun" in sys.argv:
        DRY_RUN = True

    print(f"[RUN] {NOW.isoformat()} lookback={LOOKBACK_MIN}m cutoff={CUTOFF.isoformat()}")
    leads = []
    for fn in (poll_facebook, poll_instagram, poll_wix, poll_cold):
        try:
            leads.extend(fn(mcp))
        except Exception as e:
            print(f"[ERROR] {fn.__name__}: {e}")

    seen, new = set(), 0
    for source, lead, did in leads:
        key = did or json.dumps(lead, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        send_telegram(mcp, source, lead)
        new += 1
    print(f"[DONE] {new} new lead(s) sent." if new else "[DONE] No new leads.")


if __name__ == "__main__":
    main()
