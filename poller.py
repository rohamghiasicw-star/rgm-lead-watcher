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
  - rgresults.ca free-analysis form -> formsubmit.co relay -> rohamghiasicw@gmail.com
Every lead is also appended to leads.jsonl, committed back to this repo.
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

# EVERY Composio-connected Gmail inbox, watched for direct human replies (poll_direct).
# Roham 2026-08-05: "get the lead watcher to watch all composio inboxes connected too."
# WHY THIS EXISTS: Loom/manual outreach is sent straight from these inboxes, NOT through
# the Instantly campaign, so those replies never touch the unibox and poll_instantly is
# blind to them. Bryan Bene (Beyond Auto) and Idrees Hakimi (Proud Cleaners) both replied
# with buying questions on 2026-08-04/05 and neither fired an alert.
# Composio caps Gmail at 5 accounts per toolkit; add new ids here as they're connected.
DIRECT_INBOXES = [
    ("gmail_nail-trest",  "roham@rghiasi.com"),
    ("gmail_bilby-stoma", "rg@rghiasi.com"),
    ("gmail_catch-uncord", "roham@rohamrg.com"),
    ("gmail_incog-wur",   "rohamghiasicw@gmail.com"),
]

# Roham's own sending infrastructure. A REAL reply threads onto something he sent, so its
# In-Reply-To/References points back at one of these. Warmup mail has NO In-Reply-To at
# all (verified 2026-08-05), which is what makes this filter work.
OWN_MSGID_MARKERS = ("rghiasi.com", "rohamrg.com", "rohamresults.com",
                     "rohamresultsrg.com", "rghiasiresults.com", "mail.gmail.com")

# Header-independent fallback. The first production dryrun returned 0 from every inbox
# even though the same filter found 2 locally, because payload.headers does not reliably
# come back over the MCP path - so an In-Reply-To-only gate silently rejects everything.
# A real reply also QUOTES the original ("On <date> Roham Ghiasi <roham@rghiasi.com>
# wrote:"), so his own address appears in the body. Warmup templates never contain it.
# Verified 2026-08-05: header and body signals agreed on both real replies, 0/58 warmup
# emails matched either.
OWN_ADDRESSES = ("roham@rghiasi.com", "rg@rghiasi.com",
                 "roham@rohamrg.com", "rghiasi@rohamrg.com",
                 "rohamghiasi@rohamresults.com", "rghiasi@rohamresults.com",
                 "rohamghiasi@rohamresultsrg.com", "rghiasi@rohamresultsrg.com",
                 "rohamghiasi@rghiasiresults.com", "roham@rghiasiresults.com")

# jq run in Composio's sandbox over an offloaded GMAIL_FETCH_EMAILS file (see poll_direct).
GMAIL_REPLIES_JQ = (json.dumps([a.lower() for a in OWN_ADDRESSES]) +
    r' as $own | [.results[0].response.data.messages[] | objects'
    r' | select(((.messageText // "") | ascii_downcase) as $t | any($own[]; . as $a | $t | contains($a)))'
    r' | {messageId, sender, subject, messageTimestamp, internalDate, display_url, messageText: ((.messageText // "") as $m'
    r' | ($m | sub("(?:^|\\n)[ \\t]*(On [^\\n]{0,250}(\\r?\\n[^\\n]{0,250})?wrote:|-{2,}[ \\t]*Original Message'
    r'|From:[^\\n]*\\r?\\n[ \\t]*(Sent|Date):|_{8,}[ \\t]*\\r?$).*"; ""; "m")) as $c'
    r' | if ($c | test("\\S")) then $c[0:60000] else $m[0:8000] end)}]')

# Instantly's warmup network tags every warmup subject with a shared token. Secondary
# belt-and-braces filter only - the In-Reply-To gate above is the real defence, because
# this token can rotate.
WARMUP_SUBJECT_TAGS = ("SXEG9Y4",)

# Cold outreach now sends via Instantly (ScaledMail inboxes -> Instantly). Prospect
# replies land in the Instantly unibox, NOT in the old per-inbox Gmail accounts (those
# were disconnected from Composio 2026-07). A focused "received" email = a real reply.
INSTANTLY_ACCOUNT = "instantly_sprite-olax"   # Composio Instantly connection

# Senders that are never a real reply (automation, your own domains, big platforms).
EXCLUDE_SENDERS = ("noreply", "no-reply", "donotreply", "notification", "mailer-daemon",
                   "postmaster", "rohamresults", "rghiasi", "ghiasi@", "roham@",
                   "google.com", "facebook", "wix.com", "paypal", "github",
                   "atlassian", "linkedin", "intuit", "glassdoor", "calendly",
                   "usebouncer.com", "instantly.ai", "scaledmail", "rohamghiasi")

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


LEADS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leads.jsonl")


def save_lead(source, lead, did):
    """Append every lead to leads.jsonl, one JSON object per line.

    A Telegram ping is a notification, not a record: scroll past it and the lead is
    gone. The email survives in Gmail but is not a list you can read down. This file
    is committed back to the repo by the workflow alongside state.json, so there is a
    permanent, timestamped, ordered record of every lead that ever came in, readable
    on GitHub and greppable locally. Append-only and best-effort: a failure here must
    never stop the Telegram alert, which is the time-critical half."""
    try:
        row = {"at": NOW.isoformat(), "source": source, "msg_id": did}
        row.update({k: v for k, v in lead.items() if v})
        with open(LEADS_FILE, "a") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception as e:
        print(f"[WARN] could not append to leads.jsonl: {e}")


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
        self.last_file = None
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
                # The preview cuts every long string to "..." - email bodies included.
                # The whole response is still in the sandbox file; from_file() reads it.
                self.last_file = ((payload.get("data") or {}).get("remote_file_info") or {}).get("file_path")
            return data or {}
        except Exception as e:
            print(f"[ERROR] {tool_slug}: {e} | {json.dumps(res)[:300] if res else 'no response'}")
            return {}


    def from_file(self, path, jq_filter):
        """Run jq over an offloaded response file in Composio's sandbox and return the
        parsed JSON. The preview Composio hands back cuts long strings to "..." AND drops
        whole fields (subject, timestamps), so anything that matters is re-read from the
        file. Returns None on any failure so the caller keeps the preview."""
        if not path or not re.fullmatch(r"[\w./-]+", path):
            return None
        res, _ = self._post({"jsonrpc": "2.0", "id": self._nid(), "method": "tools/call",
            "params": {"name": "COMPOSIO_REMOTE_BASH_TOOL", "arguments": {
                "command": f"jq -c '{jq_filter}' {path}"}}})
        try:
            out = json.loads(res["result"]["content"][0]["text"])
            return json.loads((out.get("data") or {}).get("stdout") or "null")
        except Exception as e:
            print(f"[WARN] from_file {path}: {e}")
            return None


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


# Where the quoted history starts in a reply. Everything from the first match down is
# Roham's own earlier email, so the alert shows the prospect's words in full and stops there.
_QUOTE_START = re.compile(
    r"(?im)^[ \t]*On\b[^\n]{0,250}(?:\n[^\n]{0,250})?\bwrote:[ \t]*$"     # Gmail/Apple, may wrap
    r"|^[ \t]*-{2,}[ \t]*Original Message[ \t]*-{2,}"                    # Outlook classic
    r"|^[ \t]*From:[^\n]*\n[ \t]*(?:Sent|Date):"                          # Outlook header block
    r"|^[ \t]*_{8,}[ \t]*$"                                              # Outlook divider
    r"|^[ \t]*>")                                                       # plain-text quoting


def reply_text(body):
    """The prospect's whole message, uncut, with the quoted thread below it removed.
    Roham 2026-10-06: "I need to be able to see the full email message, it cant be cut off"
    - the old alert sliced every reply to 180 characters."""
    body = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    m = _QUOTE_START.search(body)
    text = body[:m.start()] if m else body
    if m and not text.strip():
        # Nothing above the quote: either an inline reply (their lines sit between the
        # quoted ">" lines) or a photo/attachment-only email.
        inline = [ln for ln in body[m.end():].split("\n")
                  if ln.strip() and not ln.lstrip().startswith(">") and not _QUOTE_START.match(ln)]
        text = ("(reply written inside the quoted email)\n" + "\n".join(inline)) if inline \
            else "(no typed text - probably a photo or attachment, open the email)"
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text).strip()


TG_LIMIT = 3900          # Telegram caps a message at 4096 chars; leave room for the (1/2) tag


def tg_chunks(text):
    """Split a long alert on paragraph/line breaks so nothing is lost past Telegram's cap."""
    if len(text) <= TG_LIMIT:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > TG_LIMIT:                      # one monster line: hard-split it
            if cur:
                out.append(cur); cur = ""
            out.append(line[:TG_LIMIT]); line = line[TG_LIMIT:]
        if len(cur) + len(line) + 1 > TG_LIMIT:
            out.append(cur); cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    n = len(out)
    return [f"({i}/{n})\n{c}" if i > 1 else f"{c}\n({i}/{n})" for i, c in enumerate(out, 1)]


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
        parts.append("")                       # blank line, then the message itself
        parts.append(lead["note"])
        parts.append("")
    if lead.get("company") or lead.get("city"):
        parts.append("GBP check: " + maps_link(lead.get("company", ""), lead.get("city", "")))
    if lead.get("link"):
        parts.append("Open email: " + lead["link"])
    chunks = tg_chunks("\n".join(parts))
    if DRY_RUN:
        print(f"[WOULD ALERT] {source}: {who} | {lead.get('phone','')} | {lead.get('email','')} | {len(chunks)} msg(s)")
        for c in chunks:
            print("[WOULD SEND] " + c.replace("\n", " || "))
        return
    for c in chunks:
        mcp.execute("TELEGRAM_SEND_MESSAGE", {"chat_id": TELEGRAM_CHAT_ID, "text": c})
    print(f"[SENT] {source}: {who} ({len(chunks)} msg)")


# ----------------------------------------------------------------------------
# Source pollers
# ----------------------------------------------------------------------------
OLD = dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def gmail_messages(listing):
    """Every message list Gmail hands back, with the junk filtered out.

    WHY THIS EXISTS: for at least one connected inbox the API returns `messages`
    as a list of bare id STRINGS instead of objects. Calling .get() on those
    raised "'str' object has no attribute 'get'", and because that happened
    inside a poller, the exception aborted the WHOLE poller. poll_calendly had
    already collected a real booking from another inbox and threw it away on the
    way out, alerting nobody and logging one cryptic line. poll_meta has been
    dying the same way on every run, which is why Facebook lead alerts stopped.

    Silently skipping a malformed entry is right here: one odd row must never
    cost the leads that were parsed correctly beside it.
    """
    out = []
    for m in (listing or {}).get("messages", []) or []:
        if isinstance(m, dict):
            out.append(m)
    return out


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
    for msg in gmail_messages(listing):
        if (parse_ts(msg.get("messageTimestamp") or msg.get("internalDate")) or OLD) < CUTOFF:
            continue
        snippet = (msg.get("preview") or {}).get("body") or msg.get("messageText", "")
        lead = parse_wix(snippet)
        if lead:
            lead["link"] = msg.get("display_url", "")
            leads.append(("Website form", lead, msg.get("messageId")))
    return leads


# --- rgresults.ca intake form (FormSubmit relay) -----------------------------
# The Wix site was replaced on 2026-09-16. rgresults.ca runs on GitHub Pages now and
# its free-analysis form POSTs JSON to https://formsubmit.co/ajax/rohamghiasicw@gmail.com,
# which relays it here as an email. poll_wix only queries from:crm.wix.com, so without
# this every lead off the new site would land in the inbox and never reach Telegram,
# and nothing would error - the run would just keep printing "No new leads".

SITE_LABELS = {"fullname": "name", "full name": "name", "name": "name",
               "email": "email", "bemail": "email", "business email": "email",
               "phone": "phone", "code": "code",
               "biz": "company", "company": "company", "business": "company",
               "website": "website", "site": "website",
               "gmaps": "gmaps", "google maps": "gmaps",
               "timing": "timing", "wants it solved": "timing"}

_SITE_STOPS = "|".join(sorted((re.escape(k) for k in SITE_LABELS), key=len, reverse=True))


def parse_site_form(text):
    """Parse a FormSubmit relay of the rgresults.ca free-analysis form.

    FormSubmit renders the posted JSON as 'label: value', but Gmail's inline snippet
    flattens it onto one line, so each value is read up to the NEXT known label rather
    than to a newline."""
    if not text:
        return None

    def grab(*labels):
        """An empty field is normal - the form only requires some of them. So the value
        is allowed to be empty and is then cut at the next 'label:', because otherwise
        'fullname:  email: x@y.com' returns the email address as the NAME."""
        for lab in labels:
            m = re.search(re.escape(lab) + r"\s*:?\s*(.*?)(?=\s*(?:" + _SITE_STOPS + r")\s*:|$)",
                          text, re.I)
            if not m:
                continue
            val = re.split(r"\s*(?:" + _SITE_STOPS + r")\s*:", m.group(1), 1, re.I)[0].strip()
            if val:
                return val
        return ""

    email = grab("email", "bemail", "business email")
    em = re.search(r"[\w.+-]+@[\w.-]+\.\w+", email or text)
    email = em.group(0) if em else ""
    name = grab("fullname", "full name", "name")
    phone = " ".join(x for x in [grab("code"), grab("phone")] if x).strip()
    company = grab("biz", "company", "business")
    website = grab("website", "site")
    timing = grab("timing", "wants it solved")
    if not (name or email or phone):
        return None
    note = " - ".join(x for x in [website, ("wants it solved: " + timing) if timing else ""] if x)
    return {"name": name or "(no name)", "company": company, "city": "",
            "phone": phone, "email": email, "note": note}


def poll_site_form(mcp):
    """rgresults.ca free-analysis form, relayed by FormSubmit into the same inbox."""
    leads = []
    listing = mcp.execute("GMAIL_FETCH_EMAILS",
                          {"query": f"from:formsubmit.co newer_than:{GMAIL_FRESH_H}h",
                           "label_ids": ["INBOX"], "max_results": 15, "verbose": True}, WIX_INBOX)
    for msg in gmail_messages(listing):
        if (parse_ts(msg.get("messageTimestamp") or msg.get("internalDate")) or OLD) < CUTOFF:
            continue
        body = (msg.get("preview") or {}).get("body") or msg.get("messageText", "")
        if "activate" in (msg.get("subject", "") + body).lower():
            continue                      # FormSubmit's own activation handshake
        lead = parse_site_form(body)
        if lead:
            lead["link"] = msg.get("display_url", "")
            leads.append(("rgresults.ca form", lead, msg.get("messageId")))
    return leads


CAL_CONN = {addr: conn for conn, addr in DIRECT_INBOXES}

_CAL_STOPS = (r"event type|event name|invitee|invitee email|invitee time zone|event date"
              r"|event date/time|location|questions|phone|phone number|company website"
              r"|website|description|cancel|reschedule|powered by")


def html_to_text(v):
    """The full message comes back as raw HTML. Labels and their values sit in
    separate tags, so a regex looking for 'Invitee Email:' followed by a value
    matches nothing until the markup is gone."""
    if not v or "<" not in v:
        return v or ""
    v = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", v)
    v = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|td|table|h[1-6]|li)>", " \n ", v)
    v = re.sub(r"(?s)<!--.*?-->", " ", v)
    v = re.sub(r"<[^>]+>", " ", v)
    v = (v.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
          .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
          .replace("&zwnj;", "").replace("&#8203;", ""))
    return re.sub(r"[ \t\r\f\v]+", " ", v).strip()


def parse_calendly(subject, text):
    """Calendly's host notification for a NEW booking.

    Deliberately tolerant. Calendly's body layout differs between locales, event
    types and whether custom questions were answered, so this never depends on a
    single exact label. The invitee name falls back to the subject line, which is
    the one field Calendly always puts there: "New Event: <Name> - <when> - <event>".
    """
    text = html_to_text(text)

    def grab(*labels):
        for lab in labels:
            m = re.search(re.escape(lab) + r"\s*:?\s*(.*?)(?=\s*(?:" + _CAL_STOPS + r")\s*:|$)",
                          text, re.I)
            if not m:
                continue
            val = re.split(r"\s*(?:" + _CAL_STOPS + r")\s*:", m.group(1), 1, re.I)[0].strip()
            if val:
                return val
        return ""

    # Take EVERY address in the mail and drop Calendly's own, rather than trusting
    # one label. The body is boilerplate-heavy ("Your Calendly Notetaker will join
    # this meeting...") so a single failed label match otherwise falls back to
    # whatever address appears first, which is Calendly's, and then gets blanked.
    email = ""
    for cand in re.findall(r"[\w.+-]+@[\w.-]+\.\w+", (grab("invitee email", "email") or "") + " " + text):
        host = cand.lower().rsplit("@", 1)[-1]
        if host.endswith("calendly.com") or host.endswith("google.com"):
            continue
        email = cand
        break

    name = grab("invitee name", "invitee", "name")
    if not name:
        m = re.search(r"(?:new event|event scheduled)\s*:\s*([^\-\u2013]+)", subject or "", re.I)
        name = m.group(1).strip() if m else ""

    phone = grab("phone number", "phone", "mobile")
    if not phone:
        pm = re.search(r"(\+?\d[\d\s().-]{7,}\d)", text)
        phone = pm.group(1).strip() if pm else ""
    website = grab("company website", "website", "site", "url")
    when = grab("event date/time", "event date", "date / time", "when")
    etype = grab("event type", "event name")
    # Composio truncates a large message body to ~200 chars, so on the booking mail
    # the labelled fields are often simply absent. The SUBJECT always carries
    # "New Event: <name> - <when> - <event type>", so fall back to it rather than
    # send an alert that says only a name.
    if (not when or not etype) and subject:
        sp = [x.strip() for x in re.split(r"\s+[\-\u2013]\s+", re.sub(r"(?i)^new event\s*:\s*", "", subject))]
        if len(sp) >= 3:
            when = when or sp[-2]
            etype = etype or sp[-1]
    if not etype and subject:
        parts = [x.strip() for x in re.split(r"\s+[\-\u2013]\s+", subject)]
        etype = parts[-1] if len(parts) > 2 else ""

    if not (name or email or phone):
        return None

    def tidy(v):
        """Calendly's body has no blank line before its footer, so a grabbed value
        runs straight on into 'Need to make changes?' and the unsubscribe copy."""
        if not v:
            return ""
        v = re.split(r"(?i)\s*(?:need to make changes|powered by|reschedule|cancel|"
                     r"unsubscribe|manage notetaker|view (?:event|invitee))\b", v)[0]
        return v.strip(" .,-|")[:90]

    bits = [tidy(etype), tidy(when), tidy(website)]
    if not email and not phone:
        bits.append("contact details are in the email, tap below")
    note = " - ".join(x for x in bits if x)
    return {"name": name or "(no name)", "company": "", "city": "",
            "phone": phone, "email": email, "note": note}


def poll_calendly(mcp):
    """Booked calls. Calendly emails the host on every new booking.

    NEW BOOKINGS ONLY. Cancellations and reschedules come from the same sender and
    would otherwise fire an alert that reads exactly like a fresh lead. They are
    skipped explicitly rather than by accident, so if that is ever wanted it is a
    one-line change here and not a rediscovery.
    """
    # EVERY connected inbox, not just WIX_INBOX. Calendly notifies whichever address
    # the Calendly ACCOUNT uses, which is not necessarily the one the website forms
    # relay into. A 180 day dry run found zero Calendly mail in rohamghiasicw@gmail
    # alone, so pinning this to one inbox is exactly how a booked call would go
    # unnoticed forever while the code looked fine. Cost is 4 cheap calls a poll.
    leads = []
    seen_ids = set()
    msgs = []
    for conn, addr in DIRECT_INBOXES:
        try:
            listing = mcp.execute("GMAIL_FETCH_EMAILS",
                                  {"query": f"from:calendly.com newer_than:{GMAIL_FRESH_H}h",
                                   "label_ids": ["INBOX"], "max_results": 15, "verbose": True}, conn)
        except Exception as e:
            print(f"[WARN] poll_calendly {addr}: {e}")
            continue
        if DRY_RUN:
            # Per-inbox proof of life. An empty result and a dead connection look
            # identical from the outside, which is how a silently broken channel
            # survives. This says which it is.
            n = len(gmail_messages(listing))
            print(f"[CAL PROBE] {addr:<26} conn={conn:<20} from:calendly.com -> {n}")
            try:
                any_mail = mcp.execute("GMAIL_FETCH_EMAILS",
                                       {"query": "newer_than:2d", "label_ids": ["INBOX"],
                                        "max_results": 3, "verbose": True}, conn)
                am = gmail_messages(any_mail)
                print(f"[CAL PROBE] {addr:<26} ANY mail in 2d -> {len(am)}"
                      + ("" if not am else f" e.g. {str(am[0].get('subject'))[:70]!r}"))
            except Exception as e:
                print(f"[CAL PROBE] {addr:<26} ANY mail query FAILED: {e}")
        for m in gmail_messages(listing):
            mid = m.get("messageId")
            if mid and mid in seen_ids:
                continue
            if mid:
                seen_ids.add(mid)
            m["_inbox"] = addr
            msgs.append(m)
    for msg in msgs:
        raw_ts = msg.get("messageTimestamp") or msg.get("internalDate")
        ts = parse_ts(raw_ts)
        if DRY_RUN:
            print(f"[CAL TS] raw={raw_ts!r} parsed={ts} cutoff={CUTOFF} "
                  f"keep={bool(ts and ts >= CUTOFF)} subj={str(msg.get('subject'))[:60]!r}")
        if (ts or OLD) < CUTOFF:
            continue
        subject = msg.get("subject", "") or ""
        body = (msg.get("preview") or {}).get("body") or msg.get("messageText", "")
        low = subject.lower()

        # GMAIL_FETCH_EMAILS hands back roughly 200 characters of body, and Calendly
        # opens a booking mail with Notetaker boilerplate, so the invitee's email and
        # phone fall past the cutoff and are simply absent from the text. No parser
        # can recover what was never fetched. Pull the real message for the few
        # Calendly mails per poll that matter.
        if len(body) < 600 and msg.get("messageId"):
            for slug in ("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", "GMAIL_GET_MESSAGE"):
                try:
                    full = mcp.execute(slug, {"message_id": msg["messageId"],
                                              "user_id": "me", "format": "full"},
                                       CAL_CONN.get(msg.get("_inbox")) or WIX_INBOX)
                except Exception:
                    continue
                cand = ""
                if isinstance(full, dict):
                    cand = (full.get("messageText")
                            or (full.get("preview") or {}).get("body")
                            or full.get("body") or "")
                if len(cand or "") > len(body):
                    body = cand
                    if DRY_RUN:
                        print(f"[CAL FULL] {slug} gave {len(body)}ch")
                    break
        if DRY_RUN:
            pv = (msg.get("preview") or {}).get("body") or ""
            mt = msg.get("messageText", "") or ""
            print(f"[CALENDLY RAW] inbox={msg.get('_inbox')} subject={subject!r}")
            print(f"[CALENDLY RAW] fields: preview.body={len(pv)}ch messageText={len(mt)}ch "
                  f"keys={sorted(k for k in msg.keys())[:14]}")
            print(f"[CALENDLY RAW] used body[:1400]={(body or '')[:1400]!r}")
        if any(w in low for w in ("cancel", "reschedul", "reminder", "invitation to", "survey")):
            continue
        lead = parse_calendly(subject, body)
        if lead:
            lead["link"] = msg.get("display_url", "")
            leads.append(("Calendly booking", lead, msg.get("messageId")))
    return leads


def poll_meta(mcp):
    """Facebook Lead Ads: Meta emails 'N new lead(s) available for RGM' (no contact in the
    email - it lives in Meta Lead Center, so we notify + link to it)."""
    leads = []
    listing = mcp.execute("GMAIL_FETCH_EMAILS",
                          {"query": f"from:business.facebook.com subject:lead newer_than:{GMAIL_FRESH_H}h",
                           "label_ids": ["INBOX"], "max_results": 15, "verbose": True}, WIX_INBOX)
    for msg in gmail_messages(listing):
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
        if mcp.last_file:                     # offloaded: preview lost the body + timestamps
            full = mcp.from_file(mcp.last_file, '.results[0].response.data.items[0] | {subject, '
                                 'timestamp_email, timestamp_created, body: {text: ((.body.text // "")[0:60000])}}')
            if isinstance(full, dict):
                msg = {**msg, **{k: v for k, v in full.items() if v}}
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
                if not body_text and isinstance(body, dict):
                    body_text = html_to_text(body.get("html") or "")
                note = reply_text(body_text or msg.get("content_preview") or "")
                lead = {"name": name, "company": "", "city": "", "phone": "", "email": email,
                        "subject": msg.get("subject", ""), "note": note,
                        "received": iso(parse_ts(msg.get("timestamp_email")) or t),
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


def _headers(msg):
    ph = (msg.get("payload") or {}).get("headers") or []
    return {(h.get("name") or "").lower(): (h.get("value") or "") for h in ph if isinstance(h, dict)}


def poll_direct(mcp):
    """Human replies to Roham's DIRECT outreach, across every connected Gmail inbox.

    Loom/manual sends go straight from these inboxes and never enter the Instantly
    campaign, so poll_instantly can't see the replies. This closes that gap.

    THE HARD PART is that these inboxes are flooded with Instantly warmup mail (10-30/day
    each). Alerting on all of it would bury the real leads. The filter that works
    (verified against live data 2026-08-05):

      * a REAL reply threads onto a message Roham sent, so In-Reply-To/References
        contains one of OWN_MSGID_MARKERS. Bryan + Idrees both matched.
      * WARMUP mail carries no In-Reply-To at all and self-references its own
        Message-ID, so it never matches. All 10+ sampled warmup emails were excluded.

    The In-Reply-To gate is the primary defence; the warmup subject tag is a secondary
    check in case the warmup network ever starts threading properly.
    """
    leads = []
    for account, label in DIRECT_INBOXES:
        try:
            listing = mcp.execute("GMAIL_FETCH_EMAILS",
                                  {"query": f"in:inbox newer_than:{GMAIL_FRESH_H}h",
                                   "label_ids": ["INBOX"], "max_results": 15,
                                   "verbose": True}, account)
        except Exception as e:
            print(f"[WARN] direct[{label}] fetch failed: {e}")
            continue
        msgs = gmail_messages(listing)
        if mcp.last_file:
            # Offloaded: the preview keeps ~2 of the 15 messages and cuts every body to
            # "...", so real replies vanished (roham@rohamrg.com "scanned 2"). Re-read the
            # whole listing from the file, keep only mail that quotes one of Roham's
            # addresses (warmup never does), and strip the quoted thread in the sandbox so
            # the response stays small.
            full = mcp.from_file(mcp.last_file, GMAIL_REPLIES_JQ)
            if isinstance(full, list):
                msgs = [dict(m, _quotes_roham=True) for m in full if isinstance(m, dict)]
        kept = 0
        for msg in msgs:
            if (parse_ts(msg.get("messageTimestamp") or msg.get("internalDate")) or OLD) < CUTOFF:
                continue
            sender = (msg.get("sender") or "")
            email = sender.split("<")[-1].rstrip(">").strip().lower() if "<" in sender else sender.strip().lower()
            if any(x in email for x in EXCLUDE_SENDERS):
                continue
            subject = msg.get("subject") or ""
            if any(tag in subject for tag in WARMUP_SUBJECT_TAGS):
                continue
            body = msg.get("messageText") or (msg.get("preview") or {}).get("body") or ""
            H = _headers(msg)
            thread_ref = (H.get("in-reply-to", "") + " " + H.get("references", "")).lower()
            by_header = any(dom in thread_ref for dom in OWN_MSGID_MARKERS)
            by_quote = msg.get("_quotes_roham") or any(a in body.lower() for a in OWN_ADDRESSES)
            if not (by_header or by_quote):
                continue          # not a reply to anything Roham sent -> not a lead
            name = sender.split("<")[0].strip().strip('"') or (email.split("@")[0] if email else "(reply)")
            lead = {"name": name, "company": "", "city": "", "phone": "", "email": email,
                    "subject": subject, "note": reply_text(body),
                    "received": iso(parse_ts(msg.get("messageTimestamp") or msg.get("internalDate"))),
                    "link": msg.get("display_url", "")}
            leads.append((f"Direct reply ({label})", lead, msg.get("messageId")))
            kept += 1
        print(f"[direct] {label}: scanned {len(msgs)}, {kept} real repl(y/ies) in window")
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
    # Every connected Gmail inbox must be readable. Reading >0 is the pass condition
    # (these inboxes always have warmup traffic), so a dead/revoked connection fails loud.
    for account, label in DIRECT_INBOXES:
        g = mcp.execute("GMAIL_FETCH_EMAILS",
                        {"query": "in:inbox", "label_ids": ["INBOX"],
                         "max_results": 1, "verbose": False}, account)
        n = len(g.get("messages", []) or [])
        checks.append((f"Direct inbox {label} - read {n}", n > 0))
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


DUP_WINDOW = dt.timedelta(minutes=5)   # both copies carry the email's own send time


def iso(t):
    return t.isoformat() if t else ""


def fingerprint(lead):
    email = (lead.get("email") or "").strip().lower()
    if not email:
        return None
    subj = re.sub(r"^\s*(?:(?:re|fw|fwd|aw)\s*:\s*)+", "", (lead.get("subject") or "").lower())
    subj = re.sub(r"\W+", " ", subj).strip()
    # First 60 letters/digits of the message: a back-and-forth (Jason sent 12 replies in
    # 90 min on Oct 5) shares sender + subject + time but never the same words.
    words = re.sub(r"[\W_]+", "", (lead.get("note") or "").lower())[:60]
    return [email, subj, lead.get("received") or NOW.isoformat(), words]


def is_same_email(a, b):
    if a[0] != b[0] or a[1] != b[1]:
        return False
    ta, tb = parse_ts(a[2]), parse_ts(b[2])
    if not (ta and tb and abs(ta - tb) <= DUP_WINDOW):
        return False
    wa, wb = (a[3] if len(a) > 3 else ""), (b[3] if len(b) > 3 else "")
    return not wa or not wb or wa.startswith(wb) or wb.startswith(wa)


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
    fns = [poll_facebook, poll_instagram, poll_wix, poll_site_form, poll_calendly, poll_meta, poll_instantly, poll_direct]

    print(f"[RUN] {NOW.isoformat()} since={CUTOFF.isoformat()} last_run={state.get('last_run')} seen={len(seen)} fast={'--fast' in sys.argv}")
    leads = []
    for fn in fns:
        try:
            leads.extend(fn(mcp))
        except Exception as e:
            print(f"[ERROR] {fn.__name__}: {e}")

    if DRY_RUN:
        seen = {}          # a diagnostic shows everything in the window, alerted or not
    # ONE ALERT PER EMAIL, whichever channel sees it first. Roham's sending inboxes are
    # connected to Instantly AND to Composio Gmail, so the same reply arrives twice: once
    # in the unibox (poll_instantly) and once in the inbox (poll_direct), with different
    # ids. 18 leads double-pinged Sep 18 - Oct 2 (Costa, Dripp, Onley, Schultz...).
    # Same sender + same subject + sent within DUP_WINDOW = the same email.
    recent = [] if DRY_RUN else list(state.get("recent", []))   # [email, subject, sent_iso]
    leads.sort(key=lambda x: not x[0].startswith("Direct"))     # Gmail copy wins: real thread link
    new = dupes = 0
    for source, lead, did in leads:
        key = did or json.dumps(lead, sort_keys=True)
        if key in seen:
            continue
        seen[key] = NOW.isoformat()
        fp = fingerprint(lead)
        if fp and any(is_same_email(fp, r) for r in recent):
            dupes += 1
            print(f"[DUP] {source}: {lead.get('email')} {lead.get('subject','')!r} already alerted")
            continue
        if fp:
            recent.append(fp)
        send_telegram(mcp, source, lead)
        if not DRY_RUN:
            save_lead(source, lead, did)
        new += 1
    print((f"[DONE] {new} new lead(s) sent." if new else "[DONE] No new leads.")
          + (f" {dupes} duplicate(s) suppressed." if dupes else ""))

    if not DRY_RUN:
        cut14 = NOW - dt.timedelta(days=14)
        seen = {k: v for k, v in seen.items() if (parse_ts(v) or NOW) >= cut14}
        cut3 = NOW - dt.timedelta(days=3)
        recent = [r for r in recent if (parse_ts(r[2]) or NOW) >= cut3]
        save_state({"last_run": NOW.isoformat(), "seen": seen, "recent": recent})
        if new:
            # Tells the workflow to commit state NOW, not at the next 15-min checkpoint, so a
            # restart (cancel-in-progress) can't replay alerts sent in the last few polls.
            open(".alerted", "w").close()


if __name__ == "__main__":
    main()
