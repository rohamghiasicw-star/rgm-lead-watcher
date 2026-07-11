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
