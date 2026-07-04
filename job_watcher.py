#!/usr/bin/env python3
"""
job_watcher.py — Watch quant/HFT career pages for NEW new-grad software roles
and email you when something new appears.

Design
------
Each company is a small "adapter" that returns a normalized list of Job records.
Three backends were reverse-engineered from the live pages:

  * Greenhouse public JSON API  -> Radix (radixuniversity), HRT (wehrtyou)
  * Plain HTTP + HTML parse     -> D.E. Shaw (jobs are embedded in the page)
  * Headless render / WP-AJAX   -> Citadel, Citadel Securities, DRW (JS-rendered)

New jobs are detected by diffing against a local SQLite store, so you only get
emailed once per posting. First run seeds the store silently (no 100-email flood).

Run
---
    python job_watcher.py --once            # single pass (use with cron/launchd)
    python job_watcher.py --interval 120    # loop forever, check every 120 min
    python job_watcher.py --list            # show what will be monitored
    python job_watcher.py --debug           # dump rendered HTML for JS sites
    python job_watcher.py --selftest        # run parser tests, no network

Email is configured via environment variables (see EMAIL section below).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import smtplib
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Paths / logging
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "seen_jobs.sqlite3"
LOG_PATH = BASE_DIR / "job_watcher.log"

log = logging.getLogger("job_watcher")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
log.addHandler(_sh)
_fh = logging.FileHandler(LOG_PATH)
_fh.setFormatter(_fmt)
log.addHandler(_fh)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# --------------------------------------------------------------------------- #
# What counts as a "new-grad software" role
# --------------------------------------------------------------------------- #
# A title/department must contain a SWE term...
SWE_TERMS = [
    "software", "developer", "engineer", "programmer", "programming",
    "c++", "python", "rust", "backend", "front-end", "frontend",
    "full stack", "full-stack", "infrastructure", "platform", "sre",
    "devops", "systems", "technologist",  # Radix titles its SWE roles "Quantitative Technologist"
]
# ...and (in "filter" mode) also carry a new-grad signal...
NEWGRAD_TERMS = [
    "new grad", "new-grad", "newgrad", "graduate", "campus", "university",
    "entry level", "entry-level", "junior", "bachelor", "master", "phd",
    "early career", "early-career", "2026", "2027", "2028",
]
# ...and never match an internship (toggle EXCLUDE_INTERN below).
INTERN_TERMS = [
    "intern", "internship", "co-op", "co op", "coop",
    "placement year", "industrial placement", "summer 20",
]
EXCLUDE_INTERN = True  # Kavin wants full-time new-grad; flip to False to include interns


@dataclass
class Job:
    company: str
    job_id: str
    title: str
    location: str = ""
    department: str = ""
    url: str = ""

    @property
    def key(self) -> str:
        return f"{self.company}::{self.job_id}"


# --------------------------------------------------------------------------- #
# Company configuration
#   newgrad_mode:
#     "scoped"  -> the source is already restricted to new-grad; keep all SWE roles
#     "filter"  -> require a new-grad signal in the title/department
#     "all_swe" -> keep all software roles regardless of level (firm doesn't label
#                  new-grad; the dedup store means you still only get alerted once)
# --------------------------------------------------------------------------- #
COMPANIES = [
    # ---- Clean JSON / HTTP adapters (work out of the box) ------------------ #
    {
        "name": "Radix Trading",
        "adapter": "greenhouse",
        "board_token": "radixuniversity",   # entire board is university/campus hiring
        "newgrad_mode": "scoped",
    },
    {
        "name": "Hudson River Trading",
        "adapter": "greenhouse",
        "board_token": "wehrtyou",           # HRT's main board (all levels) -> filter to campus
        "newgrad_mode": "filter",
    },
    {
        "name": "D.E. Shaw",
        "adapter": "deshaw",
        "newgrad_mode": "all_swe",           # DESCO doesn't label level; interns still excluded
    },

    # ---- JS-rendered sites (may need one selector tweak; see --debug) ------ #
    {
        "name": "Citadel",
        "adapter": "citadel_ajax",
        "base_url": "https://www.citadel.com",
        # params mirror your careers URL; experience-filter added to match "new grad" intent
        "params": {
            "action": "careers_listing_filter",
            "selected-job-sections": "388,389,387,390",
            "experience-filter": "new-graduates",
            "sort_order": "DESC",
            "per_page": "10",
        },
        "newgrad_mode": "scoped",
    },
    {
        "name": "Citadel Securities",
        "adapter": "citadel_ajax",
        "base_url": "https://www.citadelsecurities.com",
        "params": {
            "action": "careers_listing_filter",
            "experience-filter": "new-graduates",
            "location-filter": "americas,chicago,greenwich,houston,miami,new-york",
            "selected-job-sections": "323,325,324,326",
            "sort_order": "DESC",
            "per_page": "10",
        },
        "newgrad_mode": "scoped",
    },
    {
        "name": "DRW",
        "adapter": "drw",
        "url": "https://www.drw.com/work-at-drw/listings",
        # Jobs ship inside the page's Next.js __NEXT_DATA__ payload — no browser needed.
        "newgrad_mode": "filter",
    },
]


# --------------------------------------------------------------------------- #
# HTTP helper with light retry
# --------------------------------------------------------------------------- #
def http_get(url: str, params: dict | None = None, tries: int = 3, timeout: int = 30) -> requests.Response:
    last = None
    for attempt in range(1, tries + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < tries:
                time.sleep(1.5 * attempt)
    raise last  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Adapter: Greenhouse public API  (Radix, HRT)
# --------------------------------------------------------------------------- #
def _parse_greenhouse_departments(data: dict, company: str) -> list[Job]:
    jobs: dict[str, Job] = {}
    for dept in data.get("departments", []):
        dept_name = (dept.get("name") or "").strip()
        for j in dept.get("jobs", []):
            jid = str(j.get("id"))
            if jid in jobs:
                continue
            loc = ""
            if isinstance(j.get("location"), dict):
                loc = (j["location"].get("name") or "").strip()
            jobs[jid] = Job(
                company=company,
                job_id=jid,
                title=(j.get("title") or "").strip(),
                location=loc,
                department=dept_name,
                url=(j.get("absolute_url") or "").strip(),
            )
    return list(jobs.values())


def fetch_greenhouse(cfg: dict) -> list[Job]:
    token = cfg["board_token"]
    # /departments returns departments -> jobs (id, title, location, absolute_url)
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/departments"
    r = http_get(url)
    return _parse_greenhouse_departments(r.json(), cfg["name"])


# --------------------------------------------------------------------------- #
# Adapter: D.E. Shaw  (jobs are embedded in the server-rendered page)
# --------------------------------------------------------------------------- #
_DESHAW_JOB_HREF = re.compile(r"/careers/([a-z0-9][a-z0-9-]*?)-(\d+)/?$", re.I)


def _parse_deshaw_html(html: str, company: str) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: dict[str, Job] = {}

    # Preferred: structured Next.js payload (clean titles/locations if present)
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag and tag.string:
        try:
            for j in _walk_for_jobs(json.loads(tag.string)):
                jid = str(j["id"])
                jobs.setdefault(jid, Job(company, jid, j["title"], j.get("location", ""),
                                         j.get("department", ""), j["url"]))
        except Exception:  # noqa: BLE001
            pass

    # Fallback (and belt-and-suspenders): scrape anchors to /careers/<slug>-<id>
    for a in soup.find_all("a", href=True):
        m = _DESHAW_JOB_HREF.search(a["href"])
        if not m:
            continue
        jid = m.group(2)
        if jid in jobs:
            continue
        text = a.get_text(" ", strip=True)
        text = re.sub(r"^icon\s+", "", text, flags=re.I)
        title = text.split(":")[0].strip() or m.group(1).replace("-", " ").title()
        href = a["href"]
        url = href if href.startswith("http") else urljoin("https://www.deshaw.com", href)
        jobs[jid] = Job(company, jid, title, url=url)
    return list(jobs.values())


def _walk_for_jobs(obj):
    """Recursively yield dict-shaped job records out of a Next.js JSON blob."""
    if isinstance(obj, dict):
        title = obj.get("title") or obj.get("jobTitle") or obj.get("name")
        jid = obj.get("id") or obj.get("jobId") or obj.get("slug")
        loc_keys = ("office", "location", "city", "offices", "locations")
        dept_keys = ("category", "department", "team", "businessUnit", "function")
        has_loc = any(k in obj for k in loc_keys)
        has_dept = any(k in obj for k in dept_keys)
        if isinstance(title, str) and jid is not None and (has_loc or has_dept):
            slug = obj.get("slug")
            url = obj.get("url") or obj.get("absolute_url")
            if not url and slug:
                url = f"https://www.deshaw.com/careers/{slug}"

            def _flat(v):
                if isinstance(v, str):
                    return v
                if isinstance(v, dict):
                    return v.get("name") or v.get("title") or ""
                if isinstance(v, list):
                    return ", ".join(filter(None, (_flat(x) for x in v)))
                return ""
            loc = next((_flat(obj[k]) for k in loc_keys if k in obj), "")
            dept = next((_flat(obj[k]) for k in dept_keys if k in obj), "")
            if url:
                yield {"id": jid, "title": title.strip(), "location": loc,
                       "department": dept, "url": url}
        for v in obj.values():
            yield from _walk_for_jobs(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_for_jobs(v)


def fetch_deshaw(cfg: dict) -> list[Job]:
    r = http_get("https://www.deshaw.com/careers")
    return _parse_deshaw_html(r.text, cfg["name"])


# --------------------------------------------------------------------------- #
# Adapter: Citadel / Citadel Securities  (WordPress admin-ajax filter endpoint)
#   Lightweight attempt at the XHR their careers page fires. If it returns 0,
#   run with --debug (writes debug_<company>.html) and either fix the parser
#   below or switch this company's "adapter" to "playwright".
# --------------------------------------------------------------------------- #
def _parse_job_links(html: str, base_url: str, company: str,
                     link_hint=("job", "career", "greenhouse", "opportun")) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: dict[str, Job] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if not any(h in low for h in link_hint):
            continue
        title = a.get_text(" ", strip=True)
        if len(title) < 3:
            continue
        url = href if href.startswith("http") else urljoin(base_url, href)
        jobs.setdefault(url, Job(company, _hash(url), title, url=url))
    return list(jobs.values())


def fetch_citadel_ajax(cfg: dict, debug: bool = False) -> list[Job]:
    base = cfg["base_url"].rstrip("/")
    endpoint = f"{base}/wp-admin/admin-ajax.php"
    params = dict(cfg["params"])
    all_jobs: dict[str, Job] = {}
    for page in range(1, 16):  # paginate defensively
        params["current_page"] = str(page)
        try:
            r = http_get(endpoint, params=params)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: ajax page %d failed (%s)", cfg["name"], page, e)
            break
        body = r.text
        if debug and page == 1:
            _dump_debug(cfg["name"], body)

        # Response may be JSON ({html: ...} / {data: [...]}) or a raw HTML fragment
        html_fragment = body
        ctype = r.headers.get("content-type", "")
        if "json" in ctype or body.lstrip()[:1] in "{[":
            try:
                data = r.json()
                html_fragment = _extract_html_from_json(data) or ""
                for j in _extract_jobs_from_json(data, cfg["name"], base):
                    all_jobs.setdefault(j.key, j)
            except Exception:  # noqa: BLE001
                html_fragment = body

        page_jobs = _parse_job_links(html_fragment, base, cfg["name"])
        before = len(all_jobs)
        for j in page_jobs:
            all_jobs.setdefault(j.key, j)
        # Stop when a page adds nothing new or returns fewer than a full page
        if len(all_jobs) == before or len(page_jobs) < int(params.get("per_page", "10")):
            break
    return list(all_jobs.values())


def _extract_html_from_json(data):
    for k in ("html", "content", "markup", "results_html", "data"):
        v = data.get(k) if isinstance(data, dict) else None
        if isinstance(v, str) and "<" in v:
            return v
    return None


def _extract_jobs_from_json(data, company, base):
    """Best-effort: pull job-like dicts from an AJAX JSON payload."""
    out: list[Job] = []
    for j in _walk_for_jobs(data):
        out.append(Job(company, str(j["id"]), j["title"], j.get("location", ""),
                       j.get("department", ""), j["url"]))
    return out


# --------------------------------------------------------------------------- #
# Adapter: generic headless render via Playwright  (DRW; fallback for others)
# --------------------------------------------------------------------------- #
def fetch_playwright(cfg: dict, debug: bool = False) -> list[Job]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("%s: Playwright not installed — skipping. Enable with:\n"
                    "    pip install playwright && playwright install chromium",
                    cfg["name"])
        return []

    url = cfg["url"]
    link_regex = re.compile(cfg.get("link_regex", r"/(job|career)"))
    wait_selector = cfg.get("wait_selector")

    html = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=UA)
            page.goto(url, wait_until="networkidle", timeout=45000)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=10000)
                except Exception:  # noqa: BLE001
                    pass
            else:
                page.wait_for_timeout(4000)
            html = page.content()
        finally:
            browser.close()

    if debug:
        _dump_debug(cfg["name"], html)

    soup = BeautifulSoup(html, "html.parser")
    jobs: dict[str, Job] = {}
    for a in soup.find_all("a", href=True):
        if not link_regex.search(a["href"]):
            continue
        title = a.get_text(" ", strip=True)
        if len(title) < 3:
            continue
        full = a["href"] if a["href"].startswith("http") else urljoin(url, a["href"])
        jobs.setdefault(full, Job(cfg["name"], _hash(full), title, url=full))
    return list(jobs.values())


# --------------------------------------------------------------------------- #
# Adapter: DRW  (jobs are embedded in the page's Next.js __NEXT_DATA__ payload)
#   The full listing ships inside the HTML, so no headless browser is required.
# --------------------------------------------------------------------------- #
def _parse_drw_next_data(html: str, company: str) -> list[Job]:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        log.warning("%s: __NEXT_DATA__ payload not found (site layout changed?)", company)
        return []
    try:
        records = json.loads(m.group(1))["props"]["pageProps"]["jobData"]["en"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:  # noqa: BLE001
        log.warning("%s: unexpected page shape (%s)", company, e)
        return []

    jobs: dict[str, Job] = {}
    for j in records:
        if not isinstance(j, dict):
            continue
        jid = str(j.get("id") or j.get("internal_job_id") or j.get("slug") or "").strip()
        if not jid:
            continue
        slug = (j.get("slug") or "").strip()
        loc = ", ".join(j.get("locations") or []) or ", ".join(j.get("career_countries") or [])
        jobs.setdefault(jid, Job(
            company=company,
            job_id=jid,
            title=(j.get("title") or j.get("job_title") or "").strip(),
            location=loc,
            department=", ".join(j.get("career_categories") or []),
            url=(f"https://www.drw.com/work-at-drw/listings/{slug}" if slug
                 else "https://www.drw.com/work-at-drw/listings"),
        ))
    return list(jobs.values())


def fetch_drw(cfg: dict, debug: bool = False) -> list[Job]:
    r = http_get(cfg.get("url", "https://www.drw.com/work-at-drw/listings"))
    if debug:
        _dump_debug(cfg["name"], r.text)
    return _parse_drw_next_data(r.text, cfg["name"])


# --------------------------------------------------------------------------- #
# Filtering / helpers
# --------------------------------------------------------------------------- #
def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _dump_debug(company: str, html: str) -> None:
    p = BASE_DIR / f"debug_{re.sub(r'[^a-z0-9]+', '_', company.lower())}.html"
    p.write_text(html, encoding="utf-8")
    log.info("%s: wrote rendered HTML to %s (%d bytes)", company, p.name, len(html))


def is_target_role(job: Job, mode: str) -> bool:
    hay = f"{job.title} {job.department} {job.location}".lower()
    if not any(term in hay for term in SWE_TERMS):
        return False
    if EXCLUDE_INTERN and any(term in hay for term in INTERN_TERMS):
        return False
    if mode == "filter":
        return any(term in hay for term in NEWGRAD_TERMS)
    return True  # "scoped" / "all_swe"


ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "deshaw": fetch_deshaw,
    "citadel_ajax": fetch_citadel_ajax,
    "playwright": fetch_playwright,
    "drw": fetch_drw,
}
# Adapters that accept a debug= kwarg (they can dump their raw/rendered response).
_DEBUG_ADAPTERS = ("citadel_ajax", "playwright", "drw")


def scrape_company(cfg: dict, debug: bool = False, mode: str | None = None) -> tuple[list[Job], list[Job]]:
    """Scrape one company and return (raw, kept). Raises on adapter failure.
    `mode` overrides the company's configured newgrad_mode when provided."""
    fn = ADAPTERS[cfg["adapter"]]
    raw = fn(cfg, debug=debug) if cfg["adapter"] in _DEBUG_ADAPTERS else fn(cfg)
    kept = [j for j in raw if is_target_role(j, mode or cfg.get("newgrad_mode", "filter"))]
    return raw, kept


def collect_all(debug: bool = False) -> list[Job]:
    found: list[Job] = []
    for cfg in COMPANIES:
        try:
            raw, kept = scrape_company(cfg, debug=debug)
        except Exception as e:  # noqa: BLE001
            log.error("%s: adapter error: %s", cfg["name"], e)
            continue
        log.info("%-22s scraped %3d roles, %2d match new-grad SWE", cfg["name"], len(raw), len(kept))
        found.extend(kept)
    return found


# --------------------------------------------------------------------------- #
# Storage (SQLite)
# --------------------------------------------------------------------------- #
def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """CREATE TABLE IF NOT EXISTS seen (
               key        TEXT PRIMARY KEY,
               company    TEXT,
               job_id     TEXT,
               title      TEXT,
               location   TEXT,
               department TEXT,
               url        TEXT,
               first_seen TEXT
           )"""
    )
    con.commit()
    return con


def load_seen_keys(con: sqlite3.Connection) -> set[str]:
    return {row[0] for row in con.execute("SELECT key FROM seen")}


def save_jobs(con: sqlite3.Connection, jobs: list[Job]) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con.executemany(
        "INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?,?,?,?)",
        [(j.key, j.company, j.job_id, j.title, j.location, j.department, j.url, now) for j in jobs],
    )
    con.commit()


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def send_email(new_jobs: list[Job]) -> bool:
    host = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("EMAIL_SMTP_PORT", "465"))
    user = os.getenv("EMAIL_USER")
    pw = os.getenv("EMAIL_APP_PASSWORD")
    to = os.getenv("EMAIL_TO", user or "")

    if not (user and pw and to):
        log.error("Email not sent — set EMAIL_USER, EMAIL_APP_PASSWORD, EMAIL_TO "
                  "(Gmail needs an App Password, not your login password).")
        return False

    by_company: dict[str, list[Job]] = {}
    for j in new_jobs:
        by_company.setdefault(j.company, []).append(j)

    companies = ", ".join(sorted(by_company))
    subject = f"[Jobs] {len(new_jobs)} new new-grad SWE role(s): {companies}"

    text_lines, html_parts = [], ["<div style='font-family:-apple-system,Segoe UI,Arial,sans-serif'>"]
    for company in sorted(by_company):
        text_lines.append(f"\n{company}")
        html_parts.append(f"<h3 style='margin:16px 0 4px'>{company}</h3><ul style='margin:0'>")
        for j in by_company[company]:
            loc = f" — {j.location}" if j.location else ""
            text_lines.append(f"  • {j.title}{loc}\n    {j.url}")
            html_parts.append(
                f"<li style='margin:4px 0'><a href='{j.url}'>{_esc(j.title)}</a>"
                f"<span style='color:#666'>{_esc(loc)}</span></li>"
            )
        html_parts.append("</ul>")
    html_parts.append("</div>")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content("New roles found:\n" + "\n".join(text_lines))
    msg.add_alternative("".join(html_parts), subtype="html")

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        with server:
            server.login(user, pw)
            server.send_message(msg)
        log.info("Emailed %d new role(s) to %s", len(new_jobs), to)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Email send failed: %s", e)
        return False


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# --------------------------------------------------------------------------- #
# Core cycle
# --------------------------------------------------------------------------- #
def run_once(debug: bool = False, notify_seed: bool = False) -> None:
    con = db_connect()
    seen = load_seen_keys(con)
    is_first_run = len(seen) == 0

    current = collect_all(debug=debug)
    # de-dup within this cycle (same job id can surface twice)
    uniq: dict[str, Job] = {}
    for j in current:
        uniq.setdefault(j.key, j)
    current = list(uniq.values())

    new_jobs = [j for j in current if j.key not in seen]
    save_jobs(con, current)
    con.close()

    if not new_jobs:
        log.info("No new roles this pass (%d tracked).", len(current))
        return

    if is_first_run and not notify_seed:
        log.info("First run: seeded %d roles silently. Future new postings will be emailed.",
                 len(new_jobs))
        return

    log.info("%d NEW role(s):", len(new_jobs))
    for j in new_jobs:
        log.info("   + [%s] %s — %s", j.company, j.title, j.url)
    send_email(new_jobs)


def find_company(query: str) -> dict | None:
    """Resolve a --company argument to a config. Matches on name, ignoring case,
    spaces and punctuation, so 'drw', 'D.E. Shaw', 'citadelsecurities' all work."""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    q = norm(query)
    for c in COMPANIES:                     # exact (normalized) name wins outright
        if norm(c["name"]) == q:
            return c
    partial = [c for c in COMPANIES if q and q in norm(c["name"])]
    return partial[0] if len(partial) == 1 else None


def run_company(query: str, debug: bool = False) -> int:
    """Scrape ONE company once, email any NEW matching roles, and store them.

    Unlike the full run there is no silent first-run seeding: if new postings
    exist you are always emailed. Nothing new -> no email is sent. Postings are
    only written to the store once the email actually goes out, so a failed send
    is retried on the next run instead of being silently swallowed.
    """
    cfg = find_company(query)
    if not cfg:
        names = ", ".join(c["name"] for c in COMPANIES)
        log.error("Unknown company %r. Pick one of: %s", query, names)
        return 2

    try:
        # --company is a manual "show me the SWE roles here" query, so it keeps ALL
        # software roles (not just new-grad). Change "all_swe" -> None below to
        # instead honor this firm's configured new-grad filter.
        raw, kept = scrape_company(cfg, debug=debug, mode="all_swe")
    except Exception as e:  # noqa: BLE001
        log.error("%s: adapter error: %s", cfg["name"], e)
        return 1
    log.info("%-22s scraped %3d roles, %2d SWE role(s)", cfg["name"], len(raw), len(kept))

    uniq: dict[str, Job] = {}               # de-dup within this single scrape
    for j in kept:
        uniq.setdefault(j.key, j)
    kept = list(uniq.values())

    con = db_connect()
    try:
        seen = load_seen_keys(con)
        new_jobs = [j for j in kept if j.key not in seen]
        if not new_jobs:
            log.info("%s: nothing new (%d role(s) already tracked) — no email sent.",
                     cfg["name"], len(kept))
            return 0

        log.info("%s: %d NEW posting(s):", cfg["name"], len(new_jobs))
        for j in new_jobs:
            log.info("   + %s — %s", j.title, j.url)

        if send_email(new_jobs):
            save_jobs(con, new_jobs)
            log.info("%s: emailed and saved %d new posting(s).", cfg["name"], len(new_jobs))
            return 0
        log.warning("%s: email failed — NOT saving, so they'll retry next run.", cfg["name"])
        return 1
    finally:
        con.close()


def run_loop(interval_min: int, debug: bool, notify_seed: bool) -> None:
    log.info("Watching %d companies every %d min. Ctrl+C to stop.", len(COMPANIES), interval_min)
    while True:
        try:
            run_once(debug=debug, notify_seed=notify_seed)
        except Exception as e:  # noqa: BLE001
            log.error("Cycle failed: %s", e)
        try:
            time.sleep(interval_min * 60)
        except KeyboardInterrupt:
            log.info("Stopped.")
            return


# --------------------------------------------------------------------------- #
# Self-test (no network) — proves the parsers work on real-shaped fixtures
# --------------------------------------------------------------------------- #
def selftest() -> int:
    ok = True

    gh = {"departments": [
        {"name": "Research", "jobs": [
            {"id": 1, "title": "Quantitative Researcher (Full-Time - Master's/Bachelor's)",
             "location": {"name": "Chicago"},
             "absolute_url": "https://job-boards.greenhouse.io/radixuniversity/jobs/1"}]},
        {"name": "Research Technology & Trading Systems", "jobs": [
            {"id": 2, "title": "Quantitative Technologist (Full-Time - C++ Developer)",
             "location": {"name": "New York"},
             "absolute_url": "https://job-boards.greenhouse.io/radixuniversity/jobs/2"},
            {"id": 3, "title": "Quantitative Technologist (C++ Intern)",
             "location": {"name": "Chicago"},
             "absolute_url": "https://job-boards.greenhouse.io/radixuniversity/jobs/3"}]},
    ]}
    gh_jobs = _parse_greenhouse_departments(gh, "Radix Trading")
    kept = [j for j in gh_jobs if is_target_role(j, "scoped")]
    ok &= _check("greenhouse parse count", len(gh_jobs) == 3)
    # Keeps the FT software role ("Technologist"); drops the C++ *Intern* and the
    # (non-software) Quantitative *Researcher*.
    ok &= _check("greenhouse keeps FT SWE only, drops intern + researcher",
                 [j.title for j in kept] == ["Quantitative Technologist (Full-Time - C++ Developer)"])

    deshaw_html = """
    <a href="https://www.deshaw.com/careers/software-developer-2646">icon Software Developer:
       Technology is integral to virtually everything the D. E. Shaw group does...</a>
    <a href="https://www.deshaw.com/careers/quant-systems-systems-developer-new-york-5739">
       icon Quant Systems: Systems Developer (New York): The D. E. Shaw group seeks...</a>
    <a href="https://www.deshaw.com/careers/software-developer-intern-new-york-summer-2027-5894">
       icon Software Developer Intern (New York) - Summer 2027: The D. E. Shaw group seeks...</a>
    <a href="https://www.deshaw.com/careers/senior-executive-assistant-5721">
       icon Senior Executive Assistant: The D. E. Shaw group seeks...</a>
    <a href="https://www.deshaw.com/careers/faq">Go to FAQ</a>
    """
    ds = _parse_deshaw_html(deshaw_html, "D.E. Shaw")
    ds_kept = [j for j in ds if is_target_role(j, "all_swe")]
    ids = {j.job_id for j in ds}
    ok &= _check("deshaw finds 4 postings (nav excluded)", ids == {"2646", "5739", "5894", "5721"})
    kept_ids = {j.job_id for j in ds_kept}
    ok &= _check("deshaw keeps 2 SWE (dev + systems dev)", kept_ids == {"2646", "5739"})
    ok &= _check("deshaw drops intern", "5894" not in kept_ids)
    ok &= _check("deshaw drops exec assistant", "5721" not in kept_ids)

    ok &= _check("filter mode needs new-grad hint",
                 is_target_role(Job("X", "1", "Software Engineer, Campus"), "filter") is True
                 and is_target_role(Job("X", "2", "Senior Software Engineer"), "filter") is False)

    drw_html = ('<html><body><script id="__NEXT_DATA__" type="application/json">'
                + json.dumps({"props": {"pageProps": {"jobData": {"en": [
                    {"id": 111, "title": "Software Engineer - New Grad 2026",
                     "slug": "software-engineer-new-grad-2026-111",
                     "locations": ["Chicago"], "career_categories": ["Technology"]},
                    {"id": 222, "title": "Senior Software Engineer, C++",
                     "slug": "senior-software-engineer-c-222",
                     "locations": ["London"], "career_categories": ["Technology"]},
                    {"id": 333, "title": "Accounts Administrator",
                     "slug": "accounts-administrator-333",
                     "locations": ["London"], "career_categories": ["Operations"]},
                ]}}}}) + "</script></body></html>")
    drw = _parse_drw_next_data(drw_html, "DRW")
    ok &= _check("drw parses 3 jobs from __NEXT_DATA__", len(drw) == 3)
    ok &= _check("drw builds the listing URL from the slug",
                 any(j.url == "https://www.drw.com/work-at-drw/listings/"
                     "software-engineer-new-grad-2026-111" for j in drw))
    drw_kept = [j for j in drw if is_target_role(j, "filter")]
    ok &= _check("drw filter keeps new-grad SWE, drops senior + non-SWE",
                 [j.title for j in drw_kept] == ["Software Engineer - New Grad 2026"])

    print("\nSELF-TEST:", "ALL PASSED ✅" if ok else "FAILURES ❌")
    return 0 if ok else 1


def _check(label: str, cond: bool) -> bool:
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
    return cond


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Watch quant/HFT career pages for new-grad SWE roles.")
    ap.add_argument("--once", action="store_true", help="Run one pass then exit (for cron/launchd).")
    ap.add_argument("--interval", type=int, default=int(os.getenv("CHECK_EVERY_MINUTES", "180")),
                    help="Minutes between checks when looping (default 180).")
    ap.add_argument("--list", action="store_true", help="Print monitored companies and exit.")
    ap.add_argument("--company", metavar="NAME",
                    help="Scrape ONE company once (e.g. --company DRW), email any NEW roles, "
                         "then exit. No email is sent if nothing is new.")
    ap.add_argument("--debug", action="store_true", help="Dump rendered HTML for JS/AJAX sites.")
    ap.add_argument("--notify-seed", action="store_true", help="Email even on the first (seeding) run.")
    ap.add_argument("--selftest", action="store_true", help="Run offline parser tests and exit.")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.list:
        print("Monitoring:")
        for c in COMPANIES:
            print(f"  • {c['name']:<22} via {c['adapter']:<13} (mode: {c.get('newgrad_mode')})")
        return 0
    if args.company:
        return run_company(args.company, debug=args.debug)

    if args.once:
        run_once(debug=args.debug, notify_seed=args.notify_seed)
    else:
        run_loop(args.interval, debug=args.debug, notify_seed=args.notify_seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
