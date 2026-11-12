#!/usr/bin/env python3
"""
SEBI AIF Regulatory Monitor
===========================

Watches sebi.gov.in for new Circulars, Master Circulars, Regulations and
Reports for Public Comments (consultation papers) that concern Alternative
Investment Funds, summarises each one with Google Gemini (free tier), renders
an infographic PNG, and emails it via Gmail SMTP.

Designed to run statelessly on GitHub Actions every 30 minutes; memory lives in
seen_notifications.json, which the workflow commits back to the repo.

Usage
-----
  python monitor.py                    # normal run (what the workflow calls)
  python monitor.py --dry-run          # do everything except send email / save state
  python monitor.py --test-latest 1    # re-send the newest relevant item (ignores state)
  python monitor.py --render-sample    # render the template with sample data -> out/
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader, select_autoescape
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"
OUT_DIR = ROOT / "out"
IST = timezone(timedelta(hours=5, minutes=30))

BASE = "https://www.sebi.gov.in"
LISTING = BASE + "/sebiweb/home/HomeAction.do?doListing=yes&sid={sid}&ssid={ssid}&smid={smid}"

# SEBI listing pages. If SEBI ever renumbers a section, open the section in a
# browser and copy the sid/ssid/smid values from the address bar.
SOURCES = [
    {"name": "Circulars", "url": LISTING.format(sid=1, ssid=7, smid=0)},
    {"name": "Master Circulars", "url": LISTING.format(sid=1, ssid=6, smid=0)},
    {"name": "Regulations", "url": LISTING.format(sid=1, ssid=3, smid=0)},
    {"name": "Reports for Public Comments", "url": LISTING.format(sid=4, ssid=38, smid=35)},
]
RSS_URL = BASE + "/sebirss.xml"  # supplementary: catches circulars even if a listing page breaks

# Detail pages look like /legal/circulars/jul-2026/<slug>_103241.html
DETAIL_RE = re.compile(
    r"/(?:legal|reports|reports-and-statistics)/[^\s\"'<>]+?_(\d{3,})\.html", re.I
)
DATE_RE = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+\d{4}\b")
CIRC_NO_RE = re.compile(r"(?:Circular|Notification)\s*No\.?\s*:?\s*([A-Z][A-Z0-9/().\-]{10,100})", re.I)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": BASE + "/",
    "Connection": "keep-alive",
}

# Relevance rules ------------------------------------------------------------
# Strong: any one of these in the TITLE makes an item relevant.
STRONG_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Alternative Investment Fund", re.compile(r"\balternative\s+investment\s+funds?\b", re.I)),
    ("AIF", re.compile(r"\bAIFs?\b")),  # case-sensitive on purpose
    ("Venture Capital", re.compile(r"\bventure\s+capital\b", re.I)),
    ("Angel Fund", re.compile(r"\bangel\s+funds?\b", re.I)),
    ("Large Value Fund", re.compile(r"\blarge\s+value\s+funds?\b", re.I)),
    ("Accredited Investor", re.compile(r"\baccredited\s+investors?\b", re.I)),
    ("Social Impact Fund", re.compile(r"\bsocial\s+impact\s+funds?\b", re.I)),
    ("Special Situation Fund", re.compile(r"\bspecial\s+situation\s+funds?\b", re.I)),
]
CATEGORY_RE = re.compile(r"\bcategory\s*[-–]?\s*(?:I{1,3}|[123])\b", re.I)
AIF_COUNT_RE = re.compile(r"\balternative\s+investment\s+funds?\b|\bAIFs?\b")
BODY_MIN_HITS = int(os.getenv("BODY_MIN_HITS", "3"))

# Gemini ------------------------------------------------------------------------
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-flash-lite"]

log = logging.getLogger("sebi-aif")


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass
class Config:
    gemini_key: str = field(default_factory=lambda: env("GEMINI_API_KEY"))
    gemini_models: list[str] = field(default_factory=list)
    gmail_user: str = field(default_factory=lambda: env("GMAIL_USER"))
    gmail_password: str = field(default_factory=lambda: env("GMAIL_APP_PASSWORD").replace(" ", ""))
    email_to: list[str] = field(default_factory=list)
    from_name: str = field(default_factory=lambda: env("EMAIL_FROM_NAME", "SEBI AIF Monitor"))
    state_file: Path = field(default_factory=lambda: ROOT / env("STATE_FILE", "seen_notifications.json"))
    max_pdf_mb: float = field(default_factory=lambda: float(env("MAX_PDF_MB", "40")))
    attach_pdf_max_mb: float = field(default_factory=lambda: float(env("ATTACH_PDF_MAX_MB", "15")))
    max_chars: int = field(default_factory=lambda: int(env("MAX_INPUT_CHARS", "180000")))
    max_attempts: int = field(default_factory=lambda: int(env("MAX_ATTEMPTS", "3")))
    max_items_per_run: int = field(default_factory=lambda: int(env("MAX_ITEMS_PER_RUN", "8")))
    heartbeat_days: int = field(default_factory=lambda: int(env("HEARTBEAT_DAYS", "7")))
    health_alert_after: int = field(default_factory=lambda: int(env("HEALTH_ALERT_AFTER", "6")))
    dry_run: bool = False

    def __post_init__(self) -> None:
        models = env("GEMINI_MODEL")
        self.gemini_models = ([models] if models else []) + [m for m in DEFAULT_MODELS if m != models]
        to = env("EMAIL_TO") or self.gmail_user
        self.email_to = [a.strip() for a in to.split(",") if a.strip()]

    def validate(self) -> None:
        missing = [n for n, v in [("GEMINI_API_KEY", self.gemini_key),
                                  ("GMAIL_USER", self.gmail_user),
                                  ("GMAIL_APP_PASSWORD", self.gmail_password)] if not v]
        if missing and not self.dry_run:
            raise SystemExit(f"Missing required secrets: {', '.join(missing)}")
        if not self.gemini_key:
            log.warning("GEMINI_API_KEY not set - summaries will be skipped.")


# --------------------------------------------------------------------------- #
# Data model & state
# --------------------------------------------------------------------------- #
@dataclass
class Notice:
    id: str
    title: str
    url: str
    source: str
    date: str = ""
    pdf_url: str = ""
    ref_no: str = ""


def now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


class State:
    MAX_ITEMS = 5000

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}
        self.dirty = False

    def load(self) -> "State":
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                backup = self.path.with_suffix(".corrupt.json")
                self.path.rename(backup)
                log.error("State file was corrupt; moved to %s and starting fresh.", backup.name)
                self.data = {}
        self.data.setdefault("schema", 1)
        self.data.setdefault("items", {})
        self.data.setdefault("health", {"consecutive_failures": 0, "alert_sent": False,
                                        "last_error": "", "source_zero_runs": {}})
        self.data.setdefault("stats", {"alerts_sent": 0})
        return self

    @property
    def items(self) -> dict[str, dict]:
        return self.data["items"]

    @property
    def is_new(self) -> bool:
        return not self.data.get("initialized_at")

    def get(self, nid: str) -> dict | None:
        return self.items.get(nid)

    def upsert(self, n: Notice, **fields: Any) -> dict:
        rec = self.items.get(n.id) or {"title": n.title, "url": n.url, "source": n.source,
                                       "date": n.date, "first_seen": now_iso(), "attempts": 0}
        rec.update(fields)
        rec["updated"] = now_iso()
        self.items[n.id] = rec
        self.mark()
        return rec

    def mark(self) -> None:
        self.dirty = True
        self.data["last_change_at"] = now_iso()

    def save(self) -> None:
        if not self.dirty:
            return
        if len(self.items) > self.MAX_ITEMS:  # keep the newest by SEBI's numeric id
            keep = sorted(self.items, key=lambda k: int(k) if k.isdigit() else 0)[-self.MAX_ITEMS:]
            self.data["items"] = {k: self.items[k] for k in keep}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        self.dirty = False


# --------------------------------------------------------------------------- #
# HTTP / scraping
# --------------------------------------------------------------------------- #
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(total=3, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "POST"), respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    try:  # warm-up request to pick up session cookies (JSESSIONID etc.)
        s.get(BASE + "/index.html", timeout=30)
    except requests.RequestException as e:
        log.warning("Warm-up request failed: %s", e)
    return s


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def canonical(url: str) -> str:
    p = urlparse(url)
    return f"https://www.sebi.gov.in{p.path}"


def looks_blocked(html_text: str) -> bool:
    low = html_text[:5000].lower()
    return any(k in low for k in ("access denied", "request rejected", "captcha", "are you a robot"))


def _launch_browser(p):
    """Prefer the Google Chrome preinstalled on GitHub runners (no download)."""
    try:
        return p.chromium.launch(channel="chrome", headless=True)
    except Exception as e:  # noqa: BLE001
        log.info("System Chrome unavailable (%s); trying bundled Chromium.", str(e).splitlines()[0])
    try:
        return p.chromium.launch(headless=True)
    except Exception:  # noqa: BLE001
        log.info("Installing Playwright Chromium (one-off, ~1 min)...")
        subprocess.run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
                       check=False, timeout=900)
        return p.chromium.launch(headless=True)


def fetch_rendered(url: str) -> str:
    """Fallback for JS-rendered or bot-protected pages: load in a real browser."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = _launch_browser(p)
        try:
            page = browser.new_page(user_agent=HEADERS["User-Agent"], locale="en-IN")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector("table a[href*='.html']", timeout=20000)
            except Exception:  # noqa: BLE001
                pass
            return page.content()
        finally:
            browser.close()


def parse_listing(html_text: str, source: str, base_url: str) -> list[Notice]:
    soup = BeautifulSoup(html_text, "lxml")
    anchors = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        m = DETAIL_RE.search(href)
        if m:
            anchors.append((a, href, m.group(1)))
    in_rows = [t for t in anchors if t[0].find_parent("tr")]
    if in_rows:
        anchors = in_rows  # ignore sidebar / ticker links when the table exists

    out: dict[str, Notice] = {}
    for a, href, nid in anchors:
        title = clean(a.get("title") or a.get_text(" "))
        if len(title) < 8 or nid in out:
            continue
        row = a.find_parent("tr")
        dm = DATE_RE.search(row.get_text(" ")) if row else None
        out[nid] = Notice(id=nid, title=title, url=canonical(href), source=source,
                          date=dm.group(0) if dm else "")
    return list(out.values())


def parse_rss(xml_text: str) -> list[Notice]:
    out = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError as e:
        log.warning("RSS parse error: %s", e)
        return out
    for item in root.iter("item"):
        link = clean(item.findtext("link"))
        m = DETAIL_RE.search(link)
        if not m:  # skips orders, press releases etc.
            continue
        pub = clean(item.findtext("pubDate"))
        src = "RSS: " + ("Circulars" if "/circulars/" in link else
                         "Regulations" if "/regulations/" in link else
                         "Master Circulars" if "/master-circulars/" in link else "Reports")
        out.append(Notice(id=m.group(1), title=clean(item.findtext("title")),
                          url=canonical(link), source=src, date=pub[:16]))
    return out


def collect_notices(session: requests.Session) -> tuple[list[Notice], dict[str, int], list[str]]:
    """Return (notices, per-source counts, errors)."""
    found: dict[str, Notice] = {}
    counts: dict[str, int] = {}
    errors: list[str] = []

    for src in SOURCES:
        items: list[Notice] = []
        try:
            r = session.get(src["url"], timeout=45)
            r.raise_for_status()
            if not looks_blocked(r.text):
                items = parse_listing(r.text, src["name"], src["url"])
        except requests.RequestException as e:
            errors.append(f"{src['name']}: {e}")
        if not items:  # dynamic page, block page or network hiccup -> real browser
            try:
                items = parse_listing(fetch_rendered(src["url"]), src["name"], src["url"])
            except Exception as e:  # noqa: BLE001
                errors.append(f"{src['name']} (browser): {e}")
        counts[src["name"]] = len(items)
        log.info("%-28s %3d items", src["name"], len(items))
        for n in items:
            found.setdefault(n.id, n)
        time.sleep(1.5)  # be polite

    try:
        r = session.get(RSS_URL, timeout=45)
        r.raise_for_status()
        rss = parse_rss(r.text)
        counts["RSS"] = len(rss)
        for n in rss:
            found.setdefault(n.id, n)
        log.info("%-28s %3d items", "RSS (legal/reports only)", len(rss))
    except requests.RequestException as e:
        counts["RSS"] = 0
        errors.append(f"RSS: {e}")

    return sorted(found.values(), key=lambda n: int(n.id)), counts, errors


def fetch_detail(session: requests.Session, n: Notice) -> None:
    """Fill pdf_url, ref_no, date from the detail page."""
    r = session.get(n.url, timeout=45)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    text = clean(soup.get_text(" "))
    if not n.ref_no:
        m = CIRC_NO_RE.search(text)
        if m:
            n.ref_no = clean(m.group(1))
    if not n.date:
        dm = DATE_RE.search(text)
        n.date = dm.group(0) if dm else ""

    candidates: list[str] = []
    for tag in soup.find_all(["iframe", "embed", "object", "a"]):
        src = tag.get("src") or tag.get("data") or tag.get("href") or ""
        if ".pdf" not in src.lower():
            continue
        q = parse_qs(urlparse(src).query)
        if "file" in q:  # ../../../web/?file=https://www.sebi.gov.in/sebi_data/attachdocs/...pdf
            src = q["file"][0]
        full = urljoin(n.url, src)
        if full not in candidates:
            candidates.append(full)
    candidates.sort(key=lambda u: 0 if "sebi_data" in u else 1)
    n.pdf_url = candidates[0] if candidates else ""


def download_pdf(session: requests.Session, url: str, max_mb: float) -> bytes:
    with session.get(url, timeout=90, stream=True) as r:
        r.raise_for_status()
        buf = bytearray()
        for chunk in r.iter_content(65536):
            buf.extend(chunk)
            if len(buf) > max_mb * 1024 * 1024:
                raise ValueError(f"PDF larger than {max_mb} MB")
    if not bytes(buf[:1024]).lstrip().startswith(b"%PDF"):
        raise ValueError("Downloaded file is not a PDF")
    return bytes(buf)


def extract_pdf_text(pdf: bytes) -> tuple[str, int]:
    import pymupdf
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        pages = doc.page_count
        text = "\n".join(page.get_text("text") for page in doc)
    return re.sub(r"[ \t]+", " ", text), pages


# --------------------------------------------------------------------------- #
# Relevance
# --------------------------------------------------------------------------- #
def title_relevance(title: str) -> list[str]:
    hits = [label for label, rx in STRONG_PATTERNS if rx.search(title)]
    if not hits and CATEGORY_RE.search(title) and re.search(r"\bfunds?\b", title, re.I):
        hits.append("Category I/II/III fund")
    return hits


def body_relevance(text: str) -> list[str]:
    head = text[:2500]  # addressee block: "To, All Alternative Investment Funds..."
    reasons = [f"Addressed to/about: {label}" for label, rx in STRONG_PATTERNS if rx.search(head)]
    hits = len(AIF_COUNT_RE.findall(text))
    if hits >= BODY_MIN_HITS:
        reasons.append(f"AIF mentioned {hits}x in document")
    return reasons


# --------------------------------------------------------------------------- #
# Gemini summarisation
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """You are a senior regulatory compliance analyst specialising in the SEBI
(Alternative Investment Funds) Regulations, 2012. Your readers are AIF managers, trustees,
compliance officers, custodians and fund administrators in India.

Analyse ONLY the document supplied. Hard rules:
1. Never invent facts, dates, thresholds, circular numbers or paragraph references.
   If something is not stated, write "Not specified".
2. Reproduce dates exactly as written in the document.
3. For consultation papers / draft regulations, label changes as PROPOSED, and capture the
   last date for public comments. Do not describe proposals as binding.
4. Be concrete and operational (what must change in PPMs, reporting, valuation, investor
   onboarding, KYC, custody, NAV, trustee oversight, filings). Cite paragraph numbers when visible.
5. If the document is only tangentially about AIFs, say so in relevance_note and keep the
   AIF impact sections short and honest.
6. Severity = operational urgency for an AIF manager:
   High   = new or changed mandatory obligation, or a deadline within ~90 days;
   Medium = procedural change, relaxation, extension, or deadline beyond 90 days;
   Low    = clarification, informational, or consultation with no immediate obligation."""

USER_PROMPT = """Document metadata:
- Title: {title}
- SEBI section: {source}
- Listed date: {date}
- Reference no. (if detected): {ref_no}
- URL: {url}

Produce the JSON analysis. Keep each bullet under 35 words.
{body}"""


def _arr(item_schema: dict) -> dict:
    return {"type": "ARRAY", "items": item_schema}


STR = {"type": "STRING"}
SUMMARY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "headline": STR,
        "doc_type": {"type": "STRING", "enum": ["Circular", "Master Circular", "Regulation / Amendment",
                                                "Consultation Paper", "Notification", "Other"]},
        "reference_no": STR,
        "issue_date": STR,
        "effective_date": STR,
        "comment_deadline": STR,
        "severity": {"type": "STRING", "enum": ["High", "Medium", "Low"]},
        "severity_reason": STR,
        "applicable_to": _arr(STR),
        "executive_summary": STR,
        "key_changes": _arr({"type": "OBJECT", "properties": {"change": STR, "detail": STR},
                             "required": ["change", "detail"]}),
        "deadlines": _arr({"type": "OBJECT", "properties": {"date": STR, "action": STR, "applies_to": STR},
                           "required": ["date", "action"]}),
        "impact_managers": _arr(STR),
        "impact_trustees": _arr(STR),
        "impact_investors": _arr(STR),
        "impact_others": _arr(STR),
        "action_items": _arr(STR),
        "relevance_note": STR,
        "uncertainties": _arr(STR),
    },
    "required": ["headline", "doc_type", "severity", "executive_summary", "key_changes", "deadlines",
                 "impact_managers", "impact_trustees", "impact_investors", "action_items"],
}


class SummaryError(RuntimeError):
    pass


def _retry_delay(resp: requests.Response) -> float:
    try:
        for d in resp.json().get("error", {}).get("details", []):
            if "retryDelay" in d:
                return min(float(str(d["retryDelay"]).rstrip("s")), 60.0)
    except Exception:  # noqa: BLE001
        pass
    return 20.0


def _shrink(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head, tail = int(limit * 0.8), int(limit * 0.2)
    return text[:head] + "\n\n[... middle of document truncated for length ...]\n\n" + text[-tail:], True


def summarise(cfg: Config, n: Notice, text: str, pdf: bytes | None) -> tuple[dict, str]:
    if not cfg.gemini_key:
        raise SummaryError("GEMINI_API_KEY not configured")

    parts: list[dict] = []
    if len(text.strip()) >= 800:
        body, truncated = _shrink(text, cfg.max_chars)
        note = " (NOTE: document truncated; flag anything you cannot verify)" if truncated else ""
        parts.append({"text": USER_PROMPT.format(body=f"\nDocument text{note}:\n<<<\n{body}\n>>>", **asdict(n))})
    elif pdf and len(pdf) < 18 * 1024 * 1024:  # scanned PDF -> let Gemini read it directly
        parts.append({"inlineData": {"mimeType": "application/pdf", "data": base64.b64encode(pdf).decode()}})
        parts.append({"text": USER_PROMPT.format(body="The document is the attached PDF.", **asdict(n))})
    else:
        raise SummaryError("No extractable text and PDF unavailable/too large for inline upload")

    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 16384,
                             "responseMimeType": "application/json", "responseSchema": SUMMARY_SCHEMA},
    }
    last_err = ""
    for model in cfg.gemini_models:
        for attempt in range(3):
            try:
                resp = requests.post(GEMINI_URL.format(model=model), json=payload, timeout=180,
                                     headers={"x-goog-api-key": cfg.gemini_key})
            except requests.RequestException as e:
                last_err = f"{model}: {e}"
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code == 200:
                try:
                    return _parse_gemini(resp.json()), model
                except SummaryError as e:
                    last_err = f"{model}: {e}"
                    break  # malformed output -> try next model
            last_err = f"{model}: HTTP {resp.status_code} {resp.text[:300]}"
            if resp.status_code == 429:
                time.sleep(_retry_delay(resp))
            elif resp.status_code >= 500:
                time.sleep(8 * (attempt + 1))
            else:  # 400/403/404: wrong model name, bad key, unsupported field -> next model
                break
        log.warning("Gemini model failed -> %s", last_err[:300])
    raise SummaryError(last_err or "All Gemini models failed")


def _parse_gemini(data: dict) -> dict:
    try:
        cand = data["candidates"][0]
    except (KeyError, IndexError):
        raise SummaryError(f"No candidates (promptFeedback={data.get('promptFeedback')})")
    raw = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        s = json.loads(raw)
    except json.JSONDecodeError:
        raise SummaryError(f"Non-JSON output (finishReason={cand.get('finishReason')})")
    for k in ("applicable_to", "key_changes", "deadlines", "impact_managers", "impact_trustees",
              "impact_investors", "impact_others", "action_items", "uncertainties"):
        s[k] = [x for x in (s.get(k) or []) if x]
    for k in ("headline", "doc_type", "reference_no", "issue_date", "effective_date", "comment_deadline",
              "severity_reason", "executive_summary", "relevance_note"):
        s[k] = clean(str(s.get(k) or ""))
    s["severity"] = s.get("severity") if s.get("severity") in ("High", "Medium", "Low") else "Medium"
    return s


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def jinja() -> Environment:
    e = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]),
                    trim_blocks=True, lstrip_blocks=True)
    e.filters["na"] = lambda v: v if v and str(v).strip().lower() not in ("not specified", "n/a", "none") else ""
    return e


def build_context(n: Notice, s: dict | None, model: str, reasons: list[str]) -> dict:
    return {"n": n, "s": s or {}, "model": model, "reasons": reasons,
            "generated": datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")}


def render_png(ctx: dict, out_path: Path) -> bool:
    html_doc = jinja().get_template("infographic.html").render(**ctx)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.with_suffix(".html").write_text(html_doc, encoding="utf-8")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = _launch_browser(p)
            try:
                page = browser.new_page(viewport={"width": 1080, "height": 1350}, device_scale_factor=2)
                page.set_content(html_doc, wait_until="networkidle", timeout=45000)
                page.evaluate("document.fonts.ready")
                page.locator(".card").screenshot(path=str(out_path), type="png")
            finally:
                browser.close()
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Infographic render failed: %s", e)
        return False


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def plain_text(n: Notice, s: dict | None) -> str:
    lines = [n.title, f"Section: {n.source} | Date: {n.date} | Ref: {n.ref_no or 'n/a'}",
             f"SEBI page: {n.url}", f"PDF: {n.pdf_url or 'n/a'}", ""]
    if not s:
        lines.append("AI summary could not be generated. Please read the original document.")
        return "\n".join(lines)
    lines += [f"[{s['severity'].upper()}] {s['headline']}", "", "EXECUTIVE SUMMARY", s["executive_summary"], ""]
    lines += ["KEY CHANGES"] + [f"- {c.get('change')}: {c.get('detail')}" for c in s["key_changes"]] + [""]
    lines += ["DEADLINES"] + [f"- {d.get('date')}: {d.get('action')}" for d in s["deadlines"]] + [""]
    for label, key in (("MANAGERS", "impact_managers"), ("TRUSTEES", "impact_trustees"),
                       ("INVESTORS", "impact_investors"), ("OTHERS", "impact_others")):
        if s[key]:
            lines += [f"IMPACT - {label}"] + [f"- {x}" for x in s[key]] + [""]
    lines += ["ACTION ITEMS"] + [f"[ ] {x}" for x in s["action_items"]]
    lines += ["", "AI-generated summary. Verify against the original document before acting."]
    return "\n".join(lines)


def build_email(cfg: Config, n: Notice, s: dict | None, ctx: dict,
                png: Path | None, pdf: bytes | None) -> EmailMessage:
    msg = EmailMessage()
    sev = (s or {}).get("severity", "Review")
    doc_type = (s or {}).get("doc_type") or n.source
    msg["Subject"] = f"[SEBI AIF][{sev}] {doc_type}: {n.title}"[:240]
    msg["From"] = formataddr((cfg.from_name, cfg.gmail_user or "noreply@example.com"))
    msg["To"] = ", ".join(cfg.email_to) or "noreply@example.com"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="sebi-aif-monitor")

    cid = make_msgid(domain="sebi-aif-monitor")
    ctx = {**ctx, "image_cid": cid[1:-1] if png else ""}
    msg.set_content(plain_text(n, s))
    msg.add_alternative(jinja().get_template("email.html").render(**ctx), subtype="html")
    if png and png.exists():
        msg.get_payload()[1].add_related(png.read_bytes(), maintype="image", subtype="png",
                                         cid=cid, filename="sebi-aif-infographic.png")
    if pdf and len(pdf) <= cfg.attach_pdf_max_mb * 1024 * 1024:
        msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=f"SEBI_{n.id}.pdf")
    return msg


def send_email(cfg: Config, msg: EmailMessage) -> None:
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=60) as smtp:
        smtp.login(cfg.gmail_user, cfg.gmail_password)
        smtp.send_message(msg)


def send_simple(cfg: Config, subject: str, body: str) -> None:
    if cfg.dry_run:
        log.info("[dry-run] would send: %s\n%s", subject, body)
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, formataddr((cfg.from_name, cfg.gmail_user)), ", ".join(cfg.email_to)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)
    try:
        send_email(cfg, msg)
    except Exception as e:  # noqa: BLE001
        log.error("Could not send '%s': %s", subject, e)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def process(cfg: Config, session: requests.Session, state: State | None, n: Notice,
            force: bool = False) -> str:
    """Handle one notice end-to-end. Returns final status. `state=None` => no persistence."""
    rec = state.get(n.id) if state else None
    attempts = (rec or {}).get("attempts", 0) + 1
    reasons = title_relevance(n.title)
    log.info("-> [%s] %s | title hits=%s", n.id, n.title[:90], reasons or "none")

    def persist(**f):
        if state:
            state.upsert(n, attempts=attempts, **f)
            state.save()

    # 1. detail page + PDF
    pdf, text, pages, fetch_err = None, "", 0, ""
    try:
        fetch_detail(session, n)
        if n.pdf_url:
            pdf = download_pdf(session, n.pdf_url, cfg.max_pdf_mb)
            text, pages = extract_pdf_text(pdf)
        else:
            fetch_err = "No PDF found on detail page"
    except Exception as e:  # noqa: BLE001
        fetch_err = str(e)
    if fetch_err:
        log.warning("   fetch problem: %s", fetch_err)

    # 2. relevance (title, else document body)
    if not reasons and text:
        reasons = body_relevance(text)
    if not reasons and not force:
        if fetch_err and attempts < cfg.max_attempts:
            persist(status="pending", last_error=fetch_err)
            return "pending"
        persist(status="unverified" if fetch_err else "irrelevant", last_error=fetch_err)
        return "irrelevant"

    # 3. summary (reuse cached summary from a previous failed send)
    summary, model = (rec or {}).get("summary"), (rec or {}).get("model", "")
    if not summary:
        try:
            summary, model = summarise(cfg, n, text, pdf)
            time.sleep(4)  # stay well inside free-tier RPM
        except Exception as e:  # noqa: BLE001
            log.error("   summary failed (attempt %d/%d): %s", attempts, cfg.max_attempts, e)
            if attempts < cfg.max_attempts and not force:
                persist(status="pending", last_error=f"summary: {e}", reasons=reasons)
                return "pending"
            summary, model = None, ""  # give up on AI, send a plain alert

    # 4. infographic + email
    ctx = build_context(n, summary, model, reasons)
    png_path = OUT_DIR / f"{n.id}.png"
    png = png_path if (summary and render_png(ctx, png_path)) else None
    msg = build_email(cfg, n, summary, ctx, png, pdf)

    if cfg.dry_run:
        OUT_DIR.mkdir(exist_ok=True)
        (OUT_DIR / f"{n.id}.eml").write_bytes(bytes(msg))
        (OUT_DIR / f"{n.id}.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("   [dry-run] wrote out/%s.{png,eml,json}", n.id)
        return "dry-run"
    try:
        send_email(cfg, msg)
    except Exception as e:  # noqa: BLE001
        log.error("   email failed: %s", e)
        persist(status="pending", last_error=f"email: {e}", summary=summary, model=model, reasons=reasons)
        return "pending"

    log.info("   alert sent (%s, %s)", (summary or {}).get("severity", "no-summary"), model or "no model")
    if state:
        state.data["stats"]["alerts_sent"] = state.data["stats"].get("alerts_sent", 0) + 1
        persist(status="alerted", alerted_at=now_iso(), severity=(summary or {}).get("severity"),
                reasons=reasons, summary=None, last_error="")
    return "alerted"


def update_health(cfg: Config, state: State, counts: dict[str, int], errors: list[str]) -> bool:
    h = state.data["health"]
    zero = h.setdefault("source_zero_runs", {})
    for name, c in counts.items():
        new = 0 if c else zero.get(name, 0) + 1
        if zero.get(name) != new:
            zero[name] = new
            state.mark()
    healthy = any(counts.values())
    if healthy:
        if h.get("alert_sent"):
            send_simple(cfg, "[SEBI AIF Monitor] Recovered", f"Scraping works again at {now_iso()}.")
        if h.get("consecutive_failures") or h.get("alert_sent"):
            h.update(consecutive_failures=0, alert_sent=False, last_error="")
            state.mark()
        return True
    h["consecutive_failures"] = h.get("consecutive_failures", 0) + 1
    h["last_error"] = "; ".join(errors)[:1500]
    state.mark()
    if h["consecutive_failures"] >= cfg.health_alert_after and not h.get("alert_sent"):
        send_simple(cfg, "[SEBI AIF Monitor] ALERT: cannot read SEBI website",
                    f"{h['consecutive_failures']} consecutive runs returned no data.\n\n"
                    f"Last errors:\n{h['last_error']}\n\nLikely causes: SEBI blocking GitHub IPs, "
                    "site redesign, or outage. You are NOT receiving alerts until this recovers.")
        h["alert_sent"] = True
    return False


def maybe_heartbeat(cfg: Config, state: State) -> None:
    if cfg.heartbeat_days <= 0:
        return
    last = state.data.get("last_heartbeat_at")
    if last and datetime.fromisoformat(last) > datetime.now(IST) - timedelta(days=cfg.heartbeat_days):
        return
    items = state.items.values()
    cutoff = (datetime.now(IST) - timedelta(days=cfg.heartbeat_days)).isoformat()
    recent = [i for i in items if i.get("first_seen", "") >= cutoff]
    alerted = [i for i in recent if i.get("status") == "alerted"]
    flagged = [i for i in items if i.get("status") in ("pending", "unverified")]
    zero = {k: v for k, v in state.data["health"].get("source_zero_runs", {}).items() if v}
    body = [f"Agent is alive. Window: last {cfg.heartbeat_days} days.",
            f"New SEBI items seen: {len(recent)} | AIF alerts sent: {len(alerted)}", ""]
    body += ["Alerts sent:"] + [f"- {i['title']}" for i in alerted] + [""] if alerted else []
    if flagged:
        body += ["Needs manual check (could not fetch/verify):"]
        body += [f"- [{i['status']}] {i['title']} -> {i['url']}" for i in flagged] + [""]
    if zero:
        body += [f"Sources returning 0 items (consecutive runs): {zero}"]
    send_simple(cfg, "[SEBI AIF Monitor] Weekly heartbeat", "\n".join(body))
    state.data["last_heartbeat_at"] = now_iso()
    state.mark()


def run(args: argparse.Namespace) -> int:
    cfg = Config(dry_run=args.dry_run)
    cfg.validate()

    if args.render_sample:
        sample = json.loads((TEMPLATES / "sample_summary.json").read_text(encoding="utf-8"))
        n = Notice(**sample.pop("_notice"))
        ok = render_png(build_context(n, sample, "sample", ["Alternative Investment Fund"]), OUT_DIR / "sample.png")
        print("Rendered out/sample.png" if ok else "Render failed (see log)")
        return 0 if ok else 1

    state = State(cfg.state_file).load()
    session = make_session()
    notices, counts, errors = collect_notices(session)
    for e in errors:
        log.warning("source error: %s", e)

    if not update_health(cfg, state, counts, errors):
        log.error("No source returned data this run.")
        if not cfg.dry_run:
            state.save()
        return 0  # exit 0: failures are reported by email, not by GitHub's failure spam

    if args.test_latest:
        picks = [n for n in reversed(notices) if title_relevance(n.title)][: args.test_latest]
        log.info("TEST MODE: processing %d newest title-relevant items (state untouched).", len(picks))
        for n in picks:
            process(cfg, session, None, n, force=True)
        return 0

    if state.is_new:
        for n in notices:
            state.upsert(n, status="baseline")
        state.data["initialized_at"] = state.data["last_heartbeat_at"] = now_iso()
        state.mark()
        log.info("First run: baselined %d existing items (no alerts sent).", len(notices))
        send_simple(cfg, "[SEBI AIF Monitor] Activated",
                    f"Monitoring started at {now_iso()}.\nBaselined {len(notices)} existing SEBI items.\n"
                    f"Per-source counts: {counts}\nYou will be emailed only for items published from now on.\n"
                    "Tip: run the workflow manually with test_latest=1 to see a sample alert.")
        if not cfg.dry_run:
            state.save()
        return 0

    todo = [n for n in notices if (state.get(n.id) or {}).get("status", "new") in ("new", "pending")]
    log.info("%d candidate(s) to process (%d total listed).", len(todo), len(notices))
    for n in todo[: cfg.max_items_per_run]:
        try:
            process(cfg, session, None if cfg.dry_run else state, n)
        except Exception as e:  # noqa: BLE001  never let one item kill the run
            log.exception("Unexpected failure on %s: %s", n.id, e)

    maybe_heartbeat(cfg, state)
    if not cfg.dry_run:
        state.save()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="no email, no state writes; outputs in out/")
    ap.add_argument("--test-latest", type=int, default=int(env("TEST_LATEST", "0") or 0),
                    help="send alerts for the N newest relevant items, ignoring state")
    ap.add_argument("--render-sample", action="store_true", help="render templates/sample_summary.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    sys.exit(run(args))


if __name__ == "__main__":
    main()
