#!/usr/bin/env python3
"""Mamba Drainage lead relay.

WHY THIS EXISTS
---------------
mambadrainageservices.com has no SPF/DKIM record and its GoDaddy sending IP is
blocklisted, so the lead-notification email the site sends is spam-filtered by
the client's Google Workspace. A real lead (2026-08-10) was lost that way.

The site still SAVES every landing-page submission to its database. This job
polls for submissions that have not been relayed yet and re-sends them from an
authenticated Gmail account via Composio, which does reach the client's inbox.

PRIVACY: this runs in a PUBLIC repo, so Actions logs are world-readable.
Lead content (customer name, phone, address) is NEVER printed. Only counts and
post IDs are logged.

ENV
  COMPOSIO_CONSUMER_KEY  - required (ck_...), already a repo secret
  MAMBA_WP_APP_PASS      - required, WordPress application password
  MAMBA_DRY_RUN          - optional, "1" = do not send or mark
  MAMBA_TEST_TO          - optional, redirect the email here instead of the
                           client (for end-to-end testing without spamming them)
"""
import base64, json, os, re, sys, urllib.request, urllib.error
import html as htmlmod

MCP_URL = "https://connect.composio.dev/mcp"
CKEY    = os.environ.get("COMPOSIO_CONSUMER_KEY", "").strip()
WP_PASS = os.environ.get("MAMBA_WP_APP_PASS", "").strip()
DRY     = os.environ.get("MAMBA_DRY_RUN", "").strip() == "1"

WP_BASE = "https://mambadrainageservices.com/wp-json"
WP_USER = "seo.ancell@gmail.com"
UA      = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

GMAIL_ACCOUNT = "gmail_nail-trest"          # roham@rghiasi.com
CLIENT_EMAIL  = os.environ.get("MAMBA_TEST_TO", "").strip() or "info@mambadrainageservices.com"


class MCP:
    """Minimal Composio-MCP client (same pattern as poller.py)."""

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
            print(f"[MCP HTTP {e.code}]")
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
                       "clientInfo": {"name": "mamba-lead-relay", "version": "1"}}})
        self.session = hdrs.get("mcp-session-id") or hdrs.get("Mcp-Session-Id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def execute(self, tool_slug, arguments, account=None):
        item = {"tool_slug": tool_slug, "arguments": arguments}
        if account:
            item["account"] = account
        res, _ = self._post({"jsonrpc": "2.0", "id": self._nid(), "method": "tools/call",
            "params": {"name": "COMPOSIO_MULTI_EXECUTE_TOOL", "arguments": {
                "thought": "relay lead", "current_step": "RELAY",
                "sync_response_to_workbench": False, "tools": [item]}}})
        try:
            payload = json.loads(res["result"]["content"][0]["text"])
            r0 = payload["data"]["results"][0]["response"]
            if not r0.get("successful", True):
                print(f"[WARN] {tool_slug} not successful")
                return {}
            return r0.get("data") or r0.get("data_preview") or {}
        except Exception as e:
            print(f"[ERROR] {tool_slug}: {type(e).__name__}")
            return {}


def wp(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(WP_BASE + path, data=data,
                                 method="POST" if data else "GET")
    tok = base64.b64encode((WP_USER + ":" + WP_PASS).encode()).decode()
    req.add_header("Authorization", "Basic " + tok)
    req.add_header("User-Agent", UA)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=90).read())
    except urllib.error.HTTPError as e:
        print(f"[ERROR] WP HTTP {e.code} on {path}")
        return None


def clean(raw):
    txt = re.sub(r"<[^>]+>", "\n", raw or "")
    txt = htmlmod.unescape(txt)
    return "\n".join(l.strip() for l in txt.split("\n") if l.strip())


def pending():
    rows = wp("/wp/v2/mamba_lead?per_page=50&orderby=date&order=asc&status=publish")
    if not rows:
        return []
    out = []
    for p in rows:
        if str((p.get("meta") or {}).get("mamba_notified", "")) == "1":
            continue
        out.append({"id": p["id"],
                    "title": clean(p.get("title", {}).get("rendered", "")),
                    "body":  clean(p.get("content", {}).get("rendered", ""))})
    return out


def mark(pid):
    r = wp(f"/wp/v2/mamba_lead/{pid}", {"meta": {"mamba_notified": "1"}})
    return bool(r) and str((r.get("meta") or {}).get("mamba_notified")) == "1"


def main():
    if not CKEY:
        print("[FATAL] COMPOSIO_CONSUMER_KEY is not set."); sys.exit(1)
    if not WP_PASS:
        print("[FATAL] MAMBA_WP_APP_PASS is not set."); sys.exit(1)

    leads = pending()
    print(f"[relay] pending leads: {len(leads)}")
    if not leads:
        print("[DONE] nothing to relay.")
        return

    mcp = MCP(MCP_URL, CKEY)
    sent = 0
    for ld in leads:
        name = (ld["title"].split(",")[0] or "").strip()

        # never relay our own test records
        if re.search(r"test|delete me", name, re.I):
            if not DRY and mark(ld["id"]):
                print(f"[skip] id={ld['id']} test record, marked")
            continue

        body = ("New lead from the Google Ads landing page.\n\n"
                f"{ld['body']}\n\n"
                "Call them as soon as you can.\n\n"
                "Thanks,\nRoham\nRG Marketing")

        if DRY:
            print(f"[dry] would relay id={ld['id']}")
            continue

        res = mcp.execute("GMAIL_SEND_EMAIL", {
            "recipient_email": CLIENT_EMAIL,
            "subject": f"New Google Ads lead: {name}",
            "body": body,
            "is_html": False,
        }, account=GMAIL_ACCOUNT)

        if res.get("id") or res.get("threadId"):
            if os.environ.get("MAMBA_TEST_TO", "").strip():
                print(f"[test] id={ld['id']} relayed to test address, NOT marked")
                sent += 1
                continue
            if mark(ld["id"]):
                sent += 1
                print(f"[sent] id={ld['id']} relayed and marked")
            else:
                print(f"[WARN] id={ld['id']} sent but FAILED to mark - may resend")
        else:
            print(f"[WARN] id={ld['id']} send failed, left for next run")

    print(f"[DONE] relayed {sent}/{len(leads)}")


if __name__ == "__main__":
    main()
