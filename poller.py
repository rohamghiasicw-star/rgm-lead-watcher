#!/usr/bin/env python3
"""
RGM Lead Watcher
----------------
Runs on GitHub Actions every 15 minutes (cloud-hosted, works with your laptop off).
Each run it checks Facebook Messenger, Instagram DMs, and Gmail for NEW leads that
arrived in the last ~20 minutes, and sends one Telegram message per new lead.

It reuses the connections already set up in Composio, reached through Composio's
MCP endpoint with your CONSUMER key (ck_...). The only secret needed:

  COMPOSIO_CONSUMER_KEY  - required, your Composio consumer key (ck_...)
  TELEGRAM_CHAT_ID       - optional, defaults to the value below
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
MCP_URL = "https://connect.composio.dev/mcp"
CONSUMER_KEY = os.environ.get("COMPOSIO_CONSUMER_KEY", "").strip()
TELEGRAM_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID") or "8295197275")

# Cron runs every 15 min; 20 gives a safety overlap so a lead is never missed if
# a run is slightly delayed. Worst case = a rare duplicate at the boundary.
LOOKBACK_MIN = int(os.environ.get("LOOKBACK_MIN", "20"))

FB_PAGE_ID = "114357208375877"          # RGM page
IG_ACCOUNT = "rgm-business"             # @rgm_marketing_
IG_SELF_USERNAME = "rgm_marketing_"
GMAIL_ACCOUNTS = [                      # the three ghiasi@ inboxes
    "gmail_glady-emmer",                # ghiasi@rghiasi.ca
    "gmail_seeder-soally",              # ghiasi@rohamresults.ca
    "gmail_michel-burrow",              # ghiasi@rohamresultsrg.ca
]

NOW = dt.datetime.now(dt.timezone.utc)
CUTOFF = NOW - dt.timedelta(minutes=LOOKBACK_MIN)


# ----------------------------------------------------------------------------
# Minimal Composio-MCP client (Streamable HTTP)
# ----------------------------------------------------------------------------
class MCP:
    def __init__(self, url, key):
        self.url = url
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Consumer-API-Key": key,
        }
        self.session = None
        self._id = 0
        self._handshake()

    def _post(self, payload):
        h = dict(self.headers)
        if self.session:
            h["mcp-session-id"] = self.session
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), headers=h, method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=90)
        except urllib.error.HTTPError as e:
            print(f"[MCP HTTP {e.code}] {e.read().decode()[:300]}")
            return None, {}
        raw = r.read().decode()
        body = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    body = json.loads(line[5:].strip())
                except Exception:
                    pass
        return body, dict(r.headers)

    def _next_id(self):
        self._id += 1
        return self._id

    def _handshake(self):
        _, hdrs = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "rgm-lead-watcher", "version": "1"}}})
        self.session = hdrs.get("mcp-session-id") or hdrs.get("Mcp-Session-Id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def execute(self, tool_slug, arguments, account=None):
        """Run one Composio tool via COMPOSIO_MULTI_EXECUTE_TOOL; return its `data` dict."""
        item = {"tool_slug": tool_slug, "arguments": arguments}
        if account:
            item["account"] = account
        res, _ = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
            "params": {"name": "COMPOSIO_MULTI_EXECUTE_TOOL", "arguments": {
                "thought": "lead poll", "current_step": "POLL",
                "sync_response_to_workbench": False, "tools": [item]}}})
        try:
            text = res["result"]["content"][0]["text"]
            payload = json.loads(text)
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


LEAD_FIELDS = {
    "name": re.compile(r"Full name:\s*(.+)", re.I),
    "company": re.compile(r"Company name:\s*(.+)", re.I),
    "phone": re.compile(r"Phone number:\s*(.+)", re.I),
    "city": re.compile(r"City:\s*(.+)", re.I),
}


def parse_lead_form(text):
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


def send_telegram(mcp, source, lead):
    name, company, city, phone = (lead.get("name", "?"), lead.get("company", ""),
                                  lead.get("city", ""), lead.get("phone", ""))
    who = " - ".join(x for x in [company, city] if x)
    text = (f"NEW LEAD ({source})\n{name}"
            + (f"\n{who}" if who else "")
            + f"\nPhone: {phone}\nGBP check: {maps_link(company, city)}")
    mcp.execute("TELEGRAM_SEND_MESSAGE", {"chat_id": TELEGRAM_CHAT_ID, "text": text})
    print(f"[SENT] {source}: {name} / {company} / {phone}")


# ----------------------------------------------------------------------------
# Source pollers
# ----------------------------------------------------------------------------
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
            if (parse_ts(m.get("created_time")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) < CUTOFF:
                continue
            lead = parse_lead_form(m.get("message", ""))
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
            if (parse_ts(m.get("created_time")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) < CUTOFF:
                continue
            lead = parse_lead_form(m.get("message", ""))
            if lead:
                leads.append(("Instagram", lead, m.get("id")))
    return leads


def poll_gmail(mcp):
    leads = []
    for acct in GMAIL_ACCOUNTS:
        data = mcp.execute("GMAIL_FETCH_EMAILS",
                           {"query": 'subject:"Top 10 in" newer_than:1h',
                            "label_ids": ["INBOX"], "max_results": 15, "verbose": True}, acct)
        for msg in data.get("messages", []) or []:
            ts = parse_ts(msg.get("messageTimestamp"))
            if ts is None or ts < CUTOFF:
                continue
            sender = msg.get("sender", "")
            if "roham" in sender.lower() or "ghiasi" in sender.lower():
                continue
            city = ""
            mc = re.search(r"Top 10 in\s+(.+)", msg.get("subject", ""), re.I)
            if mc:
                city = mc.group(1).strip()
            leads.append(("Gmail reply",
                          {"name": sender, "company": "", "city": city, "phone": "(see email)"},
                          msg.get("messageId")))
    return leads


# ----------------------------------------------------------------------------
# Self-test + main
# ----------------------------------------------------------------------------
def selftest(mcp):
    print("[SELFTEST] verifying every connection...")
    checks = []
    fb = mcp.execute("FACEBOOK_GET_PAGE_CONVERSATIONS",
                     {"page_id": FB_PAGE_ID, "fields": "id,updated_time", "limit": 1})
    checks.append(("Facebook", "data" in fb))
    ig = mcp.execute("INSTAGRAM_LIST_ALL_CONVERSATIONS", {"limit": 1}, IG_ACCOUNT)
    checks.append(("Instagram", "data" in ig))
    for acct in GMAIL_ACCOUNTS:
        gm = mcp.execute("GMAIL_FETCH_EMAILS",
                         {"query": 'subject:"Top 10 in"', "label_ids": ["INBOX"],
                          "max_results": 1, "verbose": False}, acct)
        checks.append((f"Gmail {acct}", ("messages" in gm or "nextPageToken" in gm)))
    lines = [f"{'OK  ' if ok else 'FAIL'} {n}" for n, ok in checks]
    all_ok = all(ok for _, ok in checks)
    for ln in lines:
        print("  " + ln)
    mcp.execute("TELEGRAM_SEND_MESSAGE", {"chat_id": TELEGRAM_CHAT_ID,
        "text": "RGM Lead Watcher - self-test\n" + "\n".join(lines)
                + ("\n\nAll systems go. Live alerts every 15 min." if all_ok
                   else "\n\nSomething failed - check the log.")})
    print("[SELFTEST] " + ("PASSED" if all_ok else "FAILED"))
    sys.exit(0 if all_ok else 1)


def main():
    if not CONSUMER_KEY:
        print("[FATAL] COMPOSIO_CONSUMER_KEY is not set.")
        sys.exit(1)
    mcp = MCP(MCP_URL, CONSUMER_KEY)

    if "--selftest" in sys.argv:
        selftest(mcp)

    print(f"[RUN] {NOW.isoformat()} lookback={LOOKBACK_MIN}m cutoff={CUTOFF.isoformat()}")
    leads = []
    for fn in (poll_facebook, poll_instagram, poll_gmail):
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
