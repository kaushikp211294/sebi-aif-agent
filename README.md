# SEBI AIF Regulatory Monitor

A free, 24/7 agent that watches **sebi.gov.in** for new AIF-related **Circulars, Master Circulars, Regulations and Consultation Papers**, summarises each with **Gemini (free tier)**, renders an **infographic PNG**, and emails it through **Gmail SMTP**. Runs on **GitHub Actions** every 30 minutes. No server, no paid service.

```
sebi-aif-agent/
├── monitor.py                      # the agent
├── requirements.txt
├── templates/
│   ├── infographic.html            # HTML/CSS card -> PNG
│   ├── email.html                  # email body (inline CSS for Gmail/Outlook)
│   └── sample_summary.json         # sample data for previewing the template
├── .github/workflows/sebi_agent.yml
└── seen_notifications.json         # created on first run, committed back by the workflow
```

## How it works

1. **Scrape** four SEBI listing pages (Circulars, Master Circulars, Regulations, Reports for Public Comments) plus the SEBI RSS feed. Uses browser-like headers and a cookie warm-up. If a page returns nothing (JS-rendered or a block page), it reloads it in headless Chrome.
2. **Remember** every item by SEBI's numeric content ID in `seen_notifications.json`. The **first run only records a baseline** and sends an "Activated" email; it does not email you SEBI's existing backlog.
3. **Filter**: the title is checked for *Alternative Investment Fund, AIF, Venture Capital, Angel Fund, Large Value Fund, Accredited Investor, Social Impact Fund, Special Situation Fund*, or *Category I/II/III … fund*. If the title doesn't match, the PDF is checked: relevant if the addressee block ("To, All AIFs…") names AIFs, or AIFs are mentioned ≥ 3 times (`BODY_MIN_HITS`).
4. **Summarise**: downloads the PDF, extracts the text (PyMuPDF), and asks Gemini for strict JSON: summary, key changes, deadlines, impact on Managers / Trustees / Investors / other intermediaries, a checklist, severity and an uncertainty list. Scanned PDFs are sent to Gemini directly. If one model name fails it tries the next one.
5. **Render** the JSON into `templates/infographic.html` and screenshot it at 2x (≈2160 px wide PNG).
6. **Email**: HTML body + inline infographic + text summary + the PDF attached (if ≤ 15 MB) + links to the SEBI page and PDF.

**Failure handling.** A failed summary or email is retried on the next run (up to 3 tries). After that you still get a plain alert with the link and PDF, never silence. If **all** sources return nothing for 6 consecutive runs (~3 h), you get a **"cannot read SEBI" alert**. A **weekly heartbeat** email confirms the agent is alive and lists anything it couldn't verify.

---

## Setup (about 20 minutes)

### 1. Get a free Gemini API key
1. Go to **https://aistudio.google.com/apikey** and sign in with a Google account.
2. Click **Create API key** → create it in a new project. **Do not enable billing**; that keeps it free.
3. Copy the key (starts with `AIza…`).

> Free-tier inputs may be used by Google to improve its products. SEBI circulars are public documents, so this is acceptable here. Never send client data through this key.

### 2. Create a Gmail App Password
App Passwords only work when 2-Step Verification is on.
1. **https://myaccount.google.com/security** → turn on **2-Step Verification**.
2. Open **https://myaccount.google.com/apppasswords**.
3. Name it `SEBI Monitor` → **Create** → copy the 16-character password (spaces don't matter).

> Use a personal Gmail account. Google Workspace (company) accounts often have App Passwords disabled by the admin.

### 3. Create the GitHub repository
1. Create a free account at github.com → **New repository** → name it `sebi-aif-agent`.
2. Choose **Public** (see *Public vs private* below for why) → **Create**.
3. Upload all files from this folder: **Add file → Upload files**, then drag in the folder contents.
   The `.github/workflows/sebi_agent.yml` path must be kept exactly. If uploading from a phone strips the folder, create it via **Add file → Create new file**, type `.github/workflows/sebi_agent.yml` as the name, and paste the contents.

### 4. Add the secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `GEMINI_API_KEY` | your `AIza…` key |
| `GMAIL_USER` | the Gmail address you send from |
| `GMAIL_APP_PASSWORD` | the 16-character app password |
| `EMAIL_TO` | recipient(s), comma-separated (optional; defaults to `GMAIL_USER`) |

Optional, under the **Variables** tab: `GEMINI_MODEL` (for example `gemini-2.5-flash`). Set it if Google retires the default model names.

### 5. Allow the workflow to commit its memory
**Settings → Actions → General → Workflow permissions → Read and write permissions → Save.**

### 6. First run and test
1. **Actions** tab → enable workflows if prompted → **SEBI AIF Monitor → Run workflow** with the default inputs.
   This records the baseline. You should receive an **"Activated"** email listing per-source item counts.
   **Check those counts.** A `0` for any source means that page isn't being read (see *Troubleshooting*).
2. Run it again with **test_latest = 1**. This sends a real alert for the newest AIF item, so you can check the email, the infographic and the PDF attachment end to end.
3. After that, scheduled runs take over.

### Local preview (optional)
```bash
pip install -r requirements.txt
python monitor.py --render-sample        # out/sample.png from sample data
export GEMINI_API_KEY=...                 # then:
python monitor.py --dry-run --test-latest 1   # full pipeline, no email; writes out/<id>.{png,eml,json}
```

---

## Things you should know (limits of the free stack)

- **Latency is not 30 minutes.** GitHub's cron is best-effort; scheduled runs are commonly delayed and occasionally skipped at busy times. Expect alerts within **~30–90 minutes** of SEBI publishing, not seconds.
- **Public vs private repo.** Public repos get unlimited free Actions minutes. Private repos get 2,000 min/month on the free plan; 48 runs/day at ~1–2 billed minutes each is roughly 1,500–2,900 min/month, which can exceed the quota. If you go private, change the cron to hourly (`7 * * * *`). Your secrets stay encrypted in both cases. Only the code and the list of seen SEBI items are public.
- **60-day auto-disable.** GitHub disables scheduled workflows in public repos with no activity for 60 days, and emails a warning first. The state commits and weekly heartbeat normally keep the repo active. If you get that warning email, click *Enable workflow*.
- **SEBI may block cloud IPs.** GitHub runners run on US data-centre IPs. If SEBI starts rejecting them, the health alert tells you. The free fix is a **self-hosted runner** on any always-on PC in India: Settings → Actions → Runners → New self-hosted runner, then change `runs-on: ubuntu-latest` to `runs-on: self-hosted`.
- **Gemini free-tier quota** is per project and per minute/day. The agent processes at most 8 items per run with pauses between calls. SEBI publishes far fewer AIF documents than that, so it should not hit the quota.
- **The summary is AI output.** It is told not to invent dates or numbers and to list what it couldn't verify, but it can still be wrong. Treat it as triage, and act on the PDF.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Activated` email shows `0` for a source | Open that section on sebi.gov.in, copy `sid/ssid/smid` from the address bar, and update `SOURCES` in `monitor.py`. |
| `SMTPAuthenticationError` | 2-Step Verification is off, the App Password is wrong, or it's a Workspace account. |
| `HTTP 404` from Gemini for every model | Google renamed its models. Set the `GEMINI_MODEL` variable to a current Flash model listed in AI Studio. |
| Push fails in "Commit state" | Step 5 (read/write permissions) wasn't saved. |
| Too many or too few alerts | Edit `STRONG_PATTERNS` or `BODY_MIN_HITS` in `monitor.py`. |

## Tunables (environment variables)
`GEMINI_MODEL`, `EMAIL_TO`, `EMAIL_FROM_NAME`, `MAX_ITEMS_PER_RUN` (8), `MAX_ATTEMPTS` (3), `BODY_MIN_HITS` (3), `ATTACH_PDF_MAX_MB` (15), `MAX_PDF_MB` (40), `MAX_INPUT_CHARS` (180000), `HEARTBEAT_DAYS` (7, 0 = off), `HEALTH_ALERT_AFTER` (6 runs).
