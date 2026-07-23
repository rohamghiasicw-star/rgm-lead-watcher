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
  - Cold-outreach replies -> Instantly unibox (ScaledMail inboxes -> Instantly)

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

# Cold outreach now sends via Instantly (ScaledMail inboxes -> Instantly). Prospect
# replies land in the Instantly unibox, NOT in the old per-inbox Gmail accounts (those
# were disconnected from Composio 2026-07). A focused "received" email = a real reply.
INSTANTLY_ACCOUNT = "instantly_sprite-olax"   # Composio Instantly connection

# Senders that are never a real reply (automation, your own domains, big platforms).
EXCLUDE_SENDERS = ("noreply", "no-reply", "donotreply", "notification", "mailer-daemon",
                   "postmaster", "rohamresults", "rghiasi", "ghiasi@", "roham@",
                   "google.com", "facebook", "wix.com", "paypal", "github",
                   "atlassian", "linkedin", "intuit", "glassdoor", "calendly",
                   "usebouncer.com", "instantly.ai", "scaledmail")

NOW = dt.datetime.now(dt.timezone.utc)
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
MAX_LOOKBACK_H = 72                  # safety cap if runs were paused a long time
# CUTOFF / GMAIL_FRESH_H are refined in main() from saved state so we never miss a
# lead in the gap between irregular GitHub-cron runs. These are just fallbacks.
CUTOFF = NOW - dt.timedelta(minutes=LOOKBACK_MIN)
GMAIL_FRESH_H = max(1, (LOOKBACK_MIN + 59) // 60 + 1)
DRY_RUN = False


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[WARN] could not write {STATE_FILE}: {e}")


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
            # Composio auto-offloads LARGE tool responses to its workbench sandbox and
            # returns only a truncated `data_preview` (newest items first) INSTEAD of a
            # populated `data`. INSTANTLY_LIST_EMAILS carries full email bodies, so it
            # trips this once the unibox has a handful of replies - and reading `data`
            # alone then silently yields zero items. That's the bug that made cold
            # replies stop alerting ~2026-07-19. Fall back to `data_preview` so we still
            # catch the most-recent leads even when the response is offloaded.
            data = r0.get("data")
            if not data:
                data = r0.get("data_preview") or {}
            return data or {}
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


def poll_meta(mcp):
    """Facebook Lead Ads: Meta emails 'N new lead(s) available for RGM' (no contact in the
    email - it lives in Meta Lead Center, so we notify + link to it)."""
    leads = []
    listing = mcp.execute("GMAIL_FETCH_EMAILS",
                          {"query": f"from:business.facebook.com subject:lead newer_than:{GMAIL_FRESH_H}h",
                           "label_ids": ["INBOX"], "max_results": 15, "verbose": True}, WIX_INBOX)
    for msg in listing.get("messages", []) or []:
        if (parse_ts(msg.get("messageTimestamp") or msg.get("internalDate")) or OLD) < CUTOFF:
            continue
        subj = msg.get("subject", "")
        m = re.search(r"(\d+)\s+new lead", subj, re.I)
        n = m.group(1) if m else "New"
        lead = {"name": f"{n} Facebook lead-ad lead(s) for RGM", "company": "", "city": "",
                "phone": "", "email": "", "note": "Contact info is in Meta Lead Center - tap to open",
                "link": msg.get("display_url", "")}
        leads.append(("Facebook Lead Ad", lead, msg.get("messageId")))
    return leads


def poll_instantly(mcp):
    """ANY reply that lands in the Instantly unibox -> a Telegram ping. Roham wants
    every human reply, interested or not ("no thanks" / "remove me" / out-of-office
    all count), so we read `emode_all` (Focused AND Others) - not just Focused, which
    was hiding half the replies. EXCLUDE_SENDERS still drops the non-replies: literal
    noreply/mailer-daemon automation, his own sending domains, and his own SaaS
    account notifications (Bouncer/Instantly/ScaledMail)."""
    leads = []
    # PAGINATE ONE EMAIL AT A TIME. emode_all pulls in fat-HTML auto-replies whose bodies
    # push a multi-item response past Composio's large-response threshold; it then offloads
    # to a sandbox file and returns a TRUNCATED, unreliably-ordered `data_preview` that
    # silently drops replies (it dropped a real reply in testing). A limit:1 page is always
    # small enough to come back inline/whole, so we walk the unibox newest->older with the
    # cursor until we cross the CUTOFF window. Cheap in steady state (0-3 pages/poll).
    read = 0
    cursor = None
    seen_ids = set()
    hit_cap = True
    # Instantly's API allows 20 requests/min. Steady-state polling only walks the few
    # emails newer than CUTOFF (0-3 pages), so this cap only bites on a big backlog after
    # downtime - in which case a 429 makes execute() return {} and we stop cleanly; the
    # remaining unseen ones get picked up on the next 3-min poll (fresh rate budget).
    for _ in range(15):                       # page cap, safely under the 20 req/min limit
        args = {"email_type": "received", "mode": "emode_all", "limit": 1, "sort_order": "desc"}
        if cursor:
            args["starting_after"] = cursor
        data = mcp.execute("INSTANTLY_LIST_EMAILS", args, INSTANTLY_ACCOUNT)
        items = data.get("items") or []
        if not items or not isinstance(items[0], dict):
            hit_cap = False
            break
        msg = items[0]
        mid = msg.get("id") or msg.get("message_id")
        if mid in seen_ids:                   # cursor didn't advance - stop, don't spin
            hit_cap = False
            break
        seen_ids.add(mid)
        read += 1
        t = parse_ts(msg.get("timestamp_created") or msg.get("timestamp_email"))
        if t and t < CUTOFF:                  # reached older-than-window - done paging
            hit_cap = False
            break
        try:
            email = (msg.get("from_address_email") or "").strip()
            if not any(x in email.lower() for x in EXCLUDE_SENDERS):
                frm = msg.get("from_address_json") or []
                first = frm[0] if isinstance(frm, list) and frm and isinstance(frm[0], dict) else {}
                name = (first.get("name") or "").strip()
                if not name or "@" in name:
                    name = email.split("@")[0] if email else "(reply)"
                # `body` is usually {"text","html"} but some items return it as a bare
                # string - handle both so one odd item can't break the parse.
                body = msg.get("body")
                body_text = body.get("text") if isinstance(body, dict) else (body if isinstance(body, str) else "")
                note = (msg.get("content_preview") or body_text or "").strip()[:180]
                lead = {"name": name, "company": "", "city": "", "phone": "", "email": email,
                        "subject": msg.get("subject", ""), "note": note,
                        "link": "https://app.instantly.ai/app/unibox"}
                leads.append(("Cold-email reply", lead, mid))
        except Exception as e:
            print(f"[WARN] instantly item skipped: {e}")
        cursor = data.get("next_starting_after")
        if not cursor:
            hit_cap = False
            break
    # Visibility: read==0 is the connection/offload-bug signature; >0 means we're reading.
    print(f"[instantly] paged {read} received item(s), {len(leads)} reply lead(s) in window")
    if hit_cap:
        # Stopped at the page cap without reaching the window edge - a backlog remains.
        # The next 3-min poll continues from the newest (already-alerted ones are in seen).
        print(f"[instantly] page cap hit with backlog remaining; continues next poll")
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
    # Mirror the real poll's per-page call exactly (emode_all, limit 1). Requiring items>0
    # means the self-test fails loud if the Instantly read ever returns nothing again.
    inst = mcp.execute("INSTANTLY_LIST_EMAILS",
                       {"email_type": "received", "mode": "emode_all",
                        "limit": 1, "sort_order": "desc"}, INSTANTLY_ACCOUNT)
    n_inst = len(inst.get("items", []) or [])
    checks.append((f"Instantly unibox (all replies) - read {n_inst}", n_inst > 0))
    lines = [f"{'OK  ' if ok else 'FAIL'} {n}" for n, ok in checks]
    all_ok = all(ok for _, ok in checks)
    for ln in lines:
        print("  " + ln)
    mcp.execute("TELEGRAM_SEND_MESSAGE", {"chat_id": TELEGRAM_CHAT_ID,
        "text": "RGM Lead Watcher - self-test\n" + "\n".join(lines)
                + ("\n\nWatching: FB DMs, IG DMs, Facebook lead-ads, Wix website form, + cold-outreach"
                   " replies in the Instantly unibox. Only texts on a real new lead."
                   if all_ok else "\n\nSomething failed - check the log.")})
    print("[SELFTEST] " + ("PASSED" if all_ok else "FAILED"))
    sys.exit(0 if all_ok else 1)


def main():
    if not CONSUMER_KEY:
        print("[FATAL] COMPOSIO_CONSUMER_KEY is not set.")
        sys.exit(1)
    mcp = MCP(MCP_URL, CONSUMER_KEY)

    if "--selftest" in sys.argv:
        selftest(mcp)

    global DRY_RUN, CUTOFF, GMAIL_FRESH_H
    if "--dryrun" in sys.argv:
        DRY_RUN = True

    # Resume from the last run so irregular cron spacing never leaves a blind gap.
    state = load_state()
    seen = dict(state.get("seen", {}))           # message_id -> iso timestamp seen
    last_run = parse_ts(state.get("last_run"))
    if last_run and "--dryrun" not in sys.argv:
        CUTOFF = max(last_run - dt.timedelta(minutes=30), NOW - dt.timedelta(hours=MAX_LOOKBACK_H))
    else:
        CUTOFF = NOW - dt.timedelta(minutes=LOOKBACK_MIN)
    GMAIL_FRESH_H = max(1, int((NOW - CUTOFF).total_seconds() // 3600) + 2)

    # All channels are cheap single calls now (Instantly replaced the 6 Gmail inboxes),
    # so every poll runs the full set - replies alert as fast as DMs. --fast is a no-op.
    fns = [poll_facebook, poll_instagram, poll_wix, poll_meta, poll_instantly]

    print(f"[RUN] {NOW.isoformat()} since={CUTOFF.isoformat()} last_run={state.get('last_run')} seen={len(seen)} fast={'--fast' in sys.argv}")
    leads = []
    for fn in fns:
        try:
            leads.extend(fn(mcp))
        except Exception as e:
            print(f"[ERROR] {fn.__name__}: {e}")

    new = 0
    for source, lead, did in leads:
        key = did or json.dumps(lead, sort_keys=True)
        if key in seen:
            continue
        send_telegram(mcp, source, lead)
        seen[key] = NOW.isoformat()
        new += 1
    print(f"[DONE] {new} new lead(s) sent." if new else "[DONE] No new leads.")

    if not DRY_RUN:
        cut14 = NOW - dt.timedelta(days=14)
        seen = {k: v for k, v in seen.items() if (parse_ts(v) or NOW) >= cut14}
        save_state({"last_run": NOW.isoformat(), "seen": seen})


if __name__ == "__main__":
    main()
