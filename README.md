# RGM Lead Watcher (free, cloud-hosted, laptop-off)

Texts your Telegram whenever a **new lead** lands in Facebook Messenger,
Instagram DMs, the Wix website form, a Facebook lead ad, or as a **cold-outreach
reply in your Instantly unibox**. Runs every 15 minutes on **GitHub Actions**
(GitHub's servers) — your laptop can be off, dead, or in a lake. It doesn't matter.

- **Cost:** $0. Public repo = unlimited free Actions minutes. Composio API = free tier.
- **No n8n, no Zapier, no always-on computer.**

It reuses the Facebook / Instagram / Gmail / Telegram connections already set up in
Composio, so the only secret it needs is your Composio API key.

---

## One-time setup (about 5 minutes)

### 1. Create a repo
- Go to github.com → **New repository** → name it `rgm-lead-watcher` →
  set it **Public** (public = unlimited free Actions) → Create.
- Upload the two files from this folder, keeping the folder structure:
  - `poller.py`
  - `.github/workflows/lead-poller.yml`

  (Easiest: "Add file → Upload files", drag `poller.py`, commit. Then "Add file →
  Create new file", type `.github/workflows/lead-poller.yml`, paste the contents, commit.)

### 2. Add your Composio API key as a secret
- Get the key: **platform.composio.dev → Settings → API Keys** → copy.
- In the repo: **Settings → Secrets and variables → Actions → New repository secret**
  - Name: `COMPOSIO_CONSUMER_KEY`  → Value: *(paste the key)* → Add secret.
- (Optional) add a second secret `TELEGRAM_CHAT_ID` = `8295197275` if you want it
  configurable; otherwise the script already defaults to your chat.

### 3. Turn it on
- Open the **Actions** tab → enable workflows if prompted.
- Click **RGM Lead Watcher → Run workflow** to test it once right now.
- After that it runs itself every 15 minutes, forever.

---

## Notes
- **Keep it alive:** GitHub disables scheduled workflows in a repo with *no activity
  for 60 days*. Just push any small commit occasionally, or it'll email you first.
- **What counts as a lead:** a Facebook/Instagram lead-form message (contains a
  phone number), a Wix website-form / Facebook lead-ad email, or a focused "received"
  reply in the Instantly unibox (a real prospect answering a cold campaign), seen in
  the last ~20 minutes. No "all clear" spam — it only messages you on a real lead.
- **GBP check:** each alert includes a one-tap Google Maps search link for that
  business so you can eyeball their Google Business Profile.
- **Tuning:** change the schedule in `lead-poller.yml` (`*/15` → `*/30` etc.).
  Change the lookback window with a `LOOKBACK_MIN` secret/variable.
- If a run errors, open the failed run in the **Actions** tab — the log prints the
  exact Composio response. Paste it back and it's a quick fix (usually an account ID).

## Change log

### 2026-09-16 - new-site leads would have reached nobody, and leads were never kept
**What broke:** the Wix site was replaced by rgresults.ca on GitHub Pages. Its
free-analysis form POSTs to `https://formsubmit.co/ajax/rohamghiasicw@gmail.com`, the
same inbox but a different sender. `poll_wix` only queries `from:crm.wix.com`, so every
lead off the new site would have sat unread while the run kept printing "No new leads".
Nothing would have errored.

**What I changed:**
- `parse_site_form()` + `poll_site_form()` querying `from:formsubmit.co`, registered in
  `fns`. Kept separate from `poll_wix`: the two email bodies share no format, and Wix
  notifications already in the inbox still have to parse.
- `save_lead()` appends every lead to `leads.jsonl`, and the workflow now commits it
  alongside `state.json`. A Telegram ping is a notification, not a record - scroll past
  it and the lead is gone. This is the permanent list.

**Two gotchas worth keeping:**
1. FormSubmit answers **HTTP 200** with `{"success":"false"}` until the address is
   activated. The site's form handler checks the response BODY, not just `r.ok`, or it
   would show visitors "we have it" while nothing was delivered.
2. FormSubmit flattens the posted JSON onto one line in the Gmail snippet, so values are
   read up to the NEXT known label rather than to a newline. The first version let an
   empty field swallow the following label and reported the email address as the name.
