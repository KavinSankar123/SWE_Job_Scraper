#!/usr/bin/env python3
"""
tech_watcher.py — Watch tech-company job boards for NEW mid-level software roles
and email you when something new appears.

Sibling of job_watcher.py (quant/HFT, new-grad). Same design, different target.
Three public ATS feeds cover every firm below — no scraping, no browser:

  * Greenhouse public JSON API  -> Stripe, Databricks, Airbnb, Anthropic, ... (34)
  * Lever public JSON API       -> Palantir
  * Ashby public posting API    -> Perplexity, Harvey, ClickHouse, Replit, ... (11)

What counts as "mid-level" (deliberately BROAD — widest net, some noise)
-----------------------------------------------------------------------
A title is kept when it looks like software engineering AND is neither
  * senior-tier  (senior / staff / principal / lead / manager / director / ...), nor
  * entry-level  (intern / new-grad / junior / apprentice / ...).
An unlevelled "Software Engineer" counts. "Software Engineer II/III" counts.

Two title traps this handles that naive substring matching gets wrong:
  * "Member of Technical Staff (Software Engineer)" contains "staff" but is a
    mid-level IC title -> kept (MTS is carved out before the seniority check).
  * "Internal Tools Engineer" contains "intern" -> kept (word-boundary matching).

Its own dedup store (seen_tech_jobs.sqlite3, table `seen_tech`), so it never
interferes with job_watcher.py's store.

Run
---
    python tech_watcher.py --once            # single pass (use with cron/launchd)
    python tech_watcher.py --interval 180    # loop forever, check every 180 min
    python tech_watcher.py --list            # show what will be monitored
    python tech_watcher.py --company Stripe  # scrape ONE firm now
    python tech_watcher.py --selftest        # run parser/filter tests, no network

Email is configured via the same environment variables as job_watcher.py
(EMAIL_USER, EMAIL_APP_PASSWORD, EMAIL_TO).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import smtplib
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import requests

import sheets_sync

# --------------------------------------------------------------------------- #
# Paths / logging
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "seen_tech_jobs.sqlite3"     # separate store from job_watcher
LOG_PATH = BASE_DIR / "tech_watcher.log"

log = logging.getLogger("tech_watcher")
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
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# --------------------------------------------------------------------------- #
# What counts as a "mid-level software" role
#   Word-boundary regexes throughout, so "Internal Tools Engineer" is NOT read as
#   an intern and "Leadership" is NOT read as a "lead".
# --------------------------------------------------------------------------- #
# 1. Must look like software engineering.
SWE_RE = re.compile(
    r"\b(software|swe|engineer|engineering|developer|programmer|sre|devops"
    r"|member\s+of\s+technical\s+staff)\b", re.I)

# 2. ...but NOT one of these non-software "engineer" roles.
#    The trailing "<function> ... partner" clause kills HR/finance business-partner
#    roles that merely name Engineering as the org they support — e.g. "Compensation
#    Partner (Engineering)", "People Partner, Engineering", "Finance & Strategy
#    Partner, Central Engineering". It deliberately does NOT fire on real software
#    roles that happen to contain "partner", e.g. "Software Engineer II, Backend
#    (Merchant & Partner Lifecycle)" or "Salesforce Developer, Partnerships".
NON_SWE_RE = re.compile(
    r"\b(sales|solutions?|customer|support|field|implementation|deployment|hardware"
    r"|mechanical|electrical|manufacturing|process|chemical|civil|industrial|optical"
    r"|rf|qa|quality|network|validation)\s+engineer(ing)?\b"
    r"|\bsolutions?\s+architect\b|\baccount\s+executive\b|\brecruit"
    r"|\b(compensation|people|talent|hr|human\s+resources|finance|strategy|business)\b"
    r".*\bpartner\b", re.I)

# 3. ...and NOT senior-tier (above mid-level).
SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|leader|director|manager|head|vp"
    r"|vice\s+president|distinguished|fellow|architect|chief|president|executive)\b", re.I)

# 4. ...and NOT entry-level (below mid-level).
ENTRY_RE = re.compile(
    r"\b(intern|interns|internship|co-?op|new[\s-]?grad(uate)?|newgrad|campus"
    r"|apprentice|entry[\s-]?level|junior|jr\.?|trainee|early[\s-]?career"
    r"|university\s+graduate|graduate\s+program|student)\b", re.I)

# "Member of Technical Staff" is a mid-level IC title at Perplexity/Anthropic/etc,
# but it contains the word "staff". Strip it before the seniority check.
_MTS_RE = re.compile(r"member\s+of\s+technical\s+staff", re.I)

# ATS-provided employment types that are never mid-level full-time roles.
_BAD_EMPLOYMENT = ("intern", "temporary", "contract")


@dataclass
class Job:
    company: str
    job_id: str
    title: str
    location: str = ""
    department: str = ""
    url: str = ""
    employment_type: str = ""      # Ashby/Lever expose this; Greenhouse does not

    @property
    def key(self) -> str:
        return f"{self.company}::{self.job_id}"


def is_mid_level_swe(job: Job) -> bool:
    """Broad mid-level filter: a software role that is neither senior-tier nor entry.

    Matches on the TITLE only — department is unreliable here, since at a tech company
    almost everyone sits under "Engineering" (which would let recruiters through).
    """
    title = job.title or ""
    if not SWE_RE.search(title):
        return False                          # not a software role at all
    if NON_SWE_RE.search(title):
        return False                          # sales/solutions/hardware/... "engineer"
    if SENIOR_RE.search(_MTS_RE.sub(" ", title)):
        return False                          # senior / staff / principal / manager / ...
    if ENTRY_RE.search(title):
        return False                          # intern / new-grad / junior / ...
    if any(b in (job.employment_type or "").lower() for b in _BAD_EMPLOYMENT):
        return False                          # ATS says intern/contract outright
    return True


# --------------------------------------------------------------------------- #
# Company configuration — every board token below was verified live against its
# ATS API. Adding a firm is a one-liner; find its token in its careers-page URL:
#   boards.greenhouse.io/<token>   jobs.lever.co/<token>   jobs.ashbyhq.com/<token>
# --------------------------------------------------------------------------- #
COMPANIES = [
    # ---- Greenhouse -------------------------------------------------------- #
    {"name": "Stripe",       "adapter": "greenhouse", "board_token": "stripe"},
    {"name": "Databricks",   "adapter": "greenhouse", "board_token": "databricks"},
    {"name": "Anthropic",    "adapter": "greenhouse", "board_token": "anthropic"},
    {"name": "Airbnb",       "adapter": "greenhouse", "board_token": "airbnb"},
    {"name": "Coinbase",     "adapter": "greenhouse", "board_token": "coinbase"},
    {"name": "Cloudflare",   "adapter": "greenhouse", "board_token": "cloudflare"},
    {"name": "MongoDB",      "adapter": "greenhouse", "board_token": "mongodb"},
    {"name": "Reddit",       "adapter": "greenhouse", "board_token": "reddit"},
    {"name": "Pinterest",    "adapter": "greenhouse", "board_token": "pinterest"},
    {"name": "Figma",        "adapter": "greenhouse", "board_token": "figma"},
    {"name": "Discord",      "adapter": "greenhouse", "board_token": "discord"},
    {"name": "Robinhood",    "adapter": "greenhouse", "board_token": "robinhood"},
    {"name": "Affirm",       "adapter": "greenhouse", "board_token": "affirm"},
    {"name": "Brex",         "adapter": "greenhouse", "board_token": "brex"},
    {"name": "Chime",        "adapter": "greenhouse", "board_token": "chime"},
    {"name": "Samsara",      "adapter": "greenhouse", "board_token": "samsara"},
    {"name": "Scale AI",     "adapter": "greenhouse", "board_token": "scaleai"},
    {"name": "Twilio",       "adapter": "greenhouse", "board_token": "twilio"},
    {"name": "Asana",        "adapter": "greenhouse", "board_token": "asana"},
    {"name": "GitLab",       "adapter": "greenhouse", "board_token": "gitlab"},
    {"name": "Lyft",         "adapter": "greenhouse", "board_token": "lyft"},
    {"name": "Instacart",    "adapter": "greenhouse", "board_token": "instacart"},
    {"name": "Elastic",      "adapter": "greenhouse", "board_token": "elastic"},
    {"name": "Vercel",       "adapter": "greenhouse", "board_token": "vercel"},
    {"name": "Dropbox",      "adapter": "greenhouse", "board_token": "dropbox"},
    {"name": "Gusto",        "adapter": "greenhouse", "board_token": "gusto"},
    {"name": "Duolingo",     "adapter": "greenhouse", "board_token": "duolingo"},
    {"name": "Flexport",     "adapter": "greenhouse", "board_token": "flexport"},
    {"name": "Amplitude",    "adapter": "greenhouse", "board_token": "amplitude"},
    {"name": "Webflow",      "adapter": "greenhouse", "board_token": "webflow"},
    {"name": "Carta",        "adapter": "greenhouse", "board_token": "carta"},
    {"name": "Airtable",     "adapter": "greenhouse", "board_token": "airtable"},
    {"name": "SoFi",         "adapter": "greenhouse", "board_token": "sofi"},
    {"name": "Squarespace",  "adapter": "greenhouse", "board_token": "squarespace"},
    {"name": "Datadog",      "adapter": "greenhouse", "board_token": "datadog"},
    {"name": "Okta",         "adapter": "greenhouse", "board_token": "okta"},
    {"name": "Roblox",       "adapter": "greenhouse", "board_token": "roblox"},
    {"name": "Waymo",        "adapter": "greenhouse", "board_token": "waymo"},
    {"name": "CoreWeave",    "adapter": "greenhouse", "board_token": "coreweave"},
    {"name": "xAI",          "adapter": "greenhouse", "board_token": "xai"},
    {"name": "Block",        "adapter": "greenhouse", "board_token": "block"},
    {"name": "Verkada",      "adapter": "greenhouse", "board_token": "verkada"},
    {"name": "Grafana Labs", "adapter": "greenhouse", "board_token": "grafanalabs"},
    {"name": "Figure AI",    "adapter": "greenhouse", "board_token": "figureai"},
    {"name": "Nuro",         "adapter": "greenhouse", "board_token": "nuro"},
    {"name": "Fivetran",     "adapter": "greenhouse", "board_token": "fivetran"},
    {"name": "Postman",      "adapter": "greenhouse", "board_token": "postman"},
    {"name": "Klaviyo",      "adapter": "greenhouse", "board_token": "klaviyo"},
    {"name": "Together AI",  "adapter": "greenhouse", "board_token": "togetherai"},
    {"name": "Tailscale",    "adapter": "greenhouse", "board_token": "tailscale"},
    {"name": "Braze",        "adapter": "greenhouse", "board_token": "braze"},
    {"name": "Chainguard",   "adapter": "greenhouse", "board_token": "chainguard"},
    {"name": "Abnormal Security", "adapter": "greenhouse", "board_token": "abnormalsecurity"},
    {"name": "Justworks",    "adapter": "greenhouse", "board_token": "justworks"},
    {"name": "Peloton",      "adapter": "greenhouse", "board_token": "peloton"},
    {"name": "Faire",        "adapter": "greenhouse", "board_token": "faire"},
    {"name": "Attentive",    "adapter": "greenhouse", "board_token": "attentive"},
    {"name": "project44",    "adapter": "greenhouse", "board_token": "project44"},
    {"name": "Betterment",   "adapter": "greenhouse", "board_token": "betterment"},
    {"name": "Komodo Health", "adapter": "greenhouse", "board_token": "komodohealth"},
    {"name": "Cockroach Labs", "adapter": "greenhouse", "board_token": "cockroachlabs"},
    {"name": "Mercury",      "adapter": "greenhouse", "board_token": "mercury"},
    {"name": "Gemini",       "adapter": "greenhouse", "board_token": "gemini"},
    {"name": "Twitch",       "adapter": "greenhouse", "board_token": "twitch"},
    {"name": "PlanetScale",  "adapter": "greenhouse", "board_token": "planetscale"},
    {"name": "Marqeta",      "adapter": "greenhouse", "board_token": "marqeta"},
    {"name": "Starburst",    "adapter": "greenhouse", "board_token": "starburst"},
    {"name": "Iterable",     "adapter": "greenhouse", "board_token": "iterable"},
    {"name": "Calendly",     "adapter": "greenhouse", "board_token": "calendly"},
    {"name": "StockX",       "adapter": "greenhouse", "board_token": "stockx"},

    # ---- Lever ------------------------------------------------------------- #
    {"name": "Palantir",     "adapter": "lever", "board_token": "palantir"},
    {"name": "Shield AI",    "adapter": "lever", "board_token": "shieldai"},
    {"name": "Zoox",         "adapter": "lever", "board_token": "zoox"},
    {"name": "Outreach",     "adapter": "lever", "board_token": "outreach"},
    {"name": "Wealthfront",  "adapter": "lever", "board_token": "wealthfront"},
    {"name": "Ro",           "adapter": "lever", "board_token": "ro"},

    # ---- Ashby ------------------------------------------------------------- #
    {"name": "Perplexity",   "adapter": "ashby", "board_token": "perplexity"},
    {"name": "Harvey",       "adapter": "ashby", "board_token": "harvey"},
    {"name": "ClickHouse",   "adapter": "ashby", "board_token": "clickhouse"},
    {"name": "Cohere",       "adapter": "ashby", "board_token": "cohere"},
    {"name": "Replit",       "adapter": "ashby", "board_token": "replit"},
    {"name": "Vanta",        "adapter": "ashby", "board_token": "vanta"},
    {"name": "Supabase",     "adapter": "ashby", "board_token": "supabase"},
    {"name": "Linear",       "adapter": "ashby", "board_token": "linear"},
    {"name": "Zip",          "adapter": "ashby", "board_token": "zip"},
    {"name": "Watershed",    "adapter": "ashby", "board_token": "watershed"},
    {"name": "Runway",       "adapter": "ashby", "board_token": "runway"},
    # These all 404 on Greenhouse — they're on Ashby. (Verify a token before adding:
    # a wrong one fails silently, and the firm simply never returns any jobs.)
    {"name": "OpenAI",       "adapter": "ashby", "board_token": "openai"},
    {"name": "Notion",       "adapter": "ashby", "board_token": "notion"},
    {"name": "Ramp",         "adapter": "ashby", "board_token": "ramp"},
    {"name": "Plaid",        "adapter": "ashby", "board_token": "plaid"},
    {"name": "Cursor",       "adapter": "ashby", "board_token": "cursor"},
    {"name": "ElevenLabs",   "adapter": "ashby", "board_token": "elevenlabs"},
    {"name": "Sierra",       "adapter": "ashby", "board_token": "sierra"},
    {"name": "Sentry",       "adapter": "ashby", "board_token": "sentry"},
    {"name": "Modal",        "adapter": "ashby", "board_token": "modal"},
    {"name": "Confluent",    "adapter": "ashby", "board_token": "confluent"},
    {"name": "Snowflake",    "adapter": "ashby", "board_token": "snowflake"},
    {"name": "Cerebras",     "adapter": "ashby", "board_token": "cerebras"},
    {"name": "Crusoe",       "adapter": "ashby", "board_token": "crusoe"},
    {"name": "Saronic",      "adapter": "ashby", "board_token": "saronic"},
    {"name": "Physical Intelligence", "adapter": "ashby", "board_token": "physicalintelligence"},
    {"name": "Decagon",      "adapter": "ashby", "board_token": "decagon"},
    {"name": "Writer",       "adapter": "ashby", "board_token": "writer"},
    {"name": "Docker",       "adapter": "ashby", "board_token": "docker"},
    {"name": "1Password",    "adapter": "ashby", "board_token": "1password"},
    {"name": "Temporal",     "adapter": "ashby", "board_token": "temporal"},
    {"name": "Benchling",    "adapter": "ashby", "board_token": "benchling"},
    {"name": "Render",       "adapter": "ashby", "board_token": "render"},
    {"name": "Railway",      "adapter": "ashby", "board_token": "railway"},
    {"name": "Airbyte",      "adapter": "ashby", "board_token": "airbyte"},
    {"name": "Neon",         "adapter": "ashby", "board_token": "neon"},
    {"name": "Zapier",       "adapter": "ashby", "board_token": "zapier"},
    {"name": "Miro",         "adapter": "ashby", "board_token": "miro"},
    {"name": "Strava",       "adapter": "ashby", "board_token": "strava"},
    {"name": "Poshmark",     "adapter": "ashby", "board_token": "poshmark"},
    {"name": "Modern Treasury", "adapter": "ashby", "board_token": "moderntreasury"},
    {"name": "Dave",         "adapter": "ashby", "board_token": "dave"},
    {"name": "Quora",        "adapter": "ashby", "board_token": "quora"},
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
# Adapter: Greenhouse  (flat /jobs endpoint)
#   NOTE: we use /jobs, not /departments. /departments silently omits postings that
#   have no department assigned; /jobs returns the complete board.
# --------------------------------------------------------------------------- #
def _parse_greenhouse(data: dict, company: str) -> list[Job]:
    jobs: dict[str, Job] = {}
    for j in data.get("jobs", []):
        jid = str(j.get("id") or "").strip()
        if not jid or jid in jobs:
            continue
        loc = j["location"].get("name", "") if isinstance(j.get("location"), dict) else ""
        jobs[jid] = Job(company=company, job_id=jid,
                        title=(j.get("title") or "").strip(),
                        location=(loc or "").strip(),
                        url=(j.get("absolute_url") or "").strip())
    return list(jobs.values())


def fetch_greenhouse(cfg: dict) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{cfg['board_token']}/jobs"
    return _parse_greenhouse(http_get(url).json(), cfg["name"])


# --------------------------------------------------------------------------- #
# Adapter: Lever  (api.lever.co/v0/postings/<token>?mode=json -> list of postings)
# --------------------------------------------------------------------------- #
def _parse_lever(data: list, company: str) -> list[Job]:
    jobs: dict[str, Job] = {}
    for j in data or []:
        if not isinstance(j, dict):
            continue
        jid = str(j.get("id") or "").strip()
        if not jid or jid in jobs:
            continue
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        jobs[jid] = Job(company=company, job_id=jid,
                        title=(j.get("text") or "").strip(),
                        location=", ".join(x for x in locs if x),
                        department=(cats.get("team") or "").strip(),
                        url=(j.get("hostedUrl") or j.get("applyUrl") or "").strip(),
                        employment_type=(cats.get("commitment") or "").strip())
    return list(jobs.values())


def fetch_lever(cfg: dict) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{cfg['board_token']}"
    return _parse_lever(http_get(url, params={"mode": "json"}).json(), cfg["name"])


# --------------------------------------------------------------------------- #
# Adapter: Ashby  (api.ashbyhq.com/posting-api/job-board/<token> -> {jobs: [...]})
# --------------------------------------------------------------------------- #
def _parse_ashby(data: dict, company: str) -> list[Job]:
    jobs: dict[str, Job] = {}
    for j in (data or {}).get("jobs", []):
        if not isinstance(j, dict):
            continue
        jid = str(j.get("id") or "").strip()
        if not jid or jid in jobs:
            continue
        if j.get("isListed") is False:          # unpublished / internal posting
            continue
        locs = [j.get("location") or ""] + [
            s.get("location", "") if isinstance(s, dict) else str(s)
            for s in (j.get("secondaryLocations") or [])
        ]
        jobs[jid] = Job(company=company, job_id=jid,
                        title=(j.get("title") or "").strip(),
                        location=", ".join(x for x in locs if x),
                        department=(j.get("department") or j.get("team") or "").strip(),
                        url=(j.get("jobUrl") or j.get("applyUrl") or "").strip(),
                        employment_type=(j.get("employmentType") or "").strip())
    return list(jobs.values())


def fetch_ashby(cfg: dict) -> list[Job]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{cfg['board_token']}"
    return _parse_ashby(http_get(url).json(), cfg["name"])


ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


# --------------------------------------------------------------------------- #
# Location filter — only notify about roles in the United States
#   Same rule as job_watcher: no location -> send; any US location -> send;
#   only-foreign -> ignore. Anything we can't place is treated as "maybe US".
# --------------------------------------------------------------------------- #
US_ONLY = True  # set False to notify regardless of location

_US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "PR",
}
_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho", "illinois",
    "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
    "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana",
    "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah",
    "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "puerto rico",
}
_US_CITIES = {
    "new york", "nyc", "san francisco", "sf bay area", "bay area", "seattle",
    "los angeles", "san jose", "palo alto", "mountain view", "sunnyvale", "oakland",
    "chicago", "boston", "cambridge", "austin", "dallas", "houston", "denver",
    "boulder", "atlanta", "miami", "washington", "philadelphia", "pittsburgh",
    "portland", "san diego", "phoenix", "salt lake city", "nashville", "charlotte",
    "minneapolis", "detroit", "raleigh", "durham", "bellevue", "redmond", "irvine",
    "santa monica", "culver city", "brooklyn", "jersey city", "stamford", "remote - us",
    "united states", "usa",
}
_FOREIGN = {
    "united kingdom", "england", "scotland", "wales", "ireland", "singapore", "india",
    "australia", "new zealand", "netherlands", "switzerland", "germany", "france",
    "italy", "spain", "portugal", "sweden", "norway", "denmark", "finland", "poland",
    "czech", "czechia", "austria", "belgium", "luxembourg", "hungary", "greece",
    "romania", "bulgaria", "canada", "japan", "china", "taiwan", "hong kong",
    "south korea", "korea", "israel", "united arab emirates", "emirates", "qatar",
    "saudi arabia", "brazil", "mexico", "argentina", "chile", "colombia", "uruguay",
    "turkey", "russia", "ukraine", "armenia", "vietnam", "thailand", "indonesia",
    "malaysia", "philippines", "south africa", "egypt", "nigeria", "costa rica",
    "london", "amsterdam", "montreal", "toronto", "vancouver", "sydney", "melbourne",
    "warsaw", "krakow", "zurich", "geneva", "mumbai", "bengaluru", "bangalore",
    "hyderabad", "chennai", "pune", "new delhi", "delhi", "noida", "gurugram",
    "budapest", "paris", "dublin", "dubai", "madrid", "barcelona", "shanghai",
    "beijing", "shenzhen", "seoul", "tel aviv", "tokyo", "osaka", "frankfurt",
    "munich", "berlin", "hamburg", "milan", "rome", "stockholm", "oslo", "helsinki",
    "lisbon", "porto", "prague", "vienna", "brussels", "edinburgh", "manchester",
    "kuala lumpur", "jakarta", "manila", "auckland", "cape town", "sao paulo",
    "mexico city", "buenos aires", "bogota", "istanbul", "emea", "apac",
    # common tech-office cities the first live preview surfaced as unplaceable
    "belgrade", "serbia", "zagreb", "croatia", "ljubljana", "slovenia", "sofia",
    "bucharest", "tallinn", "riga", "vilnius", "estonia", "latvia", "lithuania",
    "reykjavik", "iceland", "tbilisi", "minsk", "belarus", "cairo", "nairobi", "lagos",
}
_FOREIGN_EXACT = {"uk", "uae", "emea", "apac"}


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _split_locations(location: str) -> list[str]:
    parts = re.split(r"[;,/|\n]|\s+or\s+", location or "", flags=re.I)
    return [p.strip() for p in parts if p and p.strip()]


def _classify_location(part: str) -> str:
    low = _strip_accents(part.strip().lower())
    token = re.sub(r"[^a-z0-9]", "", low)
    up = re.sub(r"[^A-Za-z]", "", part).upper()
    if "united states" in low or token in {"us", "usa"}:
        return "US"
    if len(up) == 2 and up in _US_STATE_ABBR:
        return "US"
    if any(s in low for s in _US_STATES):
        return "US"
    if any(c in low for c in _US_CITIES):
        return "US"
    if token in _FOREIGN_EXACT or any(f in low for f in _FOREIGN):
        return "FOREIGN"
    return "UNKNOWN"


def location_in_scope(location: str) -> bool:
    """True if the posting should be emailed under the US-only rule.

    Deliberately STRICTER than job_watcher's rule. That one sends whenever *any* part
    is unrecognised, which lets clearly-foreign postings through as soon as they list
    an unplaceable sibling — e.g. "Toronto, CAN-Remote" (a Canadian role) or
    "Belgrade, London, Berlin". Tech boards list far more international offices than
    the quant boards do, so:

        any US location            -> send
        no US, but something foreign -> drop  (an unplaceable sibling can't rescue it)
        nothing placeable at all     -> send  (e.g. "N/A", "Remote" — don't miss it)
    """
    if not US_ONLY:
        return True
    parts = _split_locations(location)
    if not parts:
        return True                              # no location -> send
    classes = [_classify_location(p) for p in parts]
    if "US" in classes:
        return True                              # at least one US office
    if "FOREIGN" in classes:
        return False                             # no US office, and something is foreign
    return True                                  # entirely unplaceable -> send


# --------------------------------------------------------------------------- #
# Scrape
# --------------------------------------------------------------------------- #
def scrape_company(cfg: dict, keep_all_levels: bool = False) -> tuple[list[Job], list[Job]]:
    """Scrape one company and return (raw, kept). Raises on adapter failure."""
    raw = ADAPTERS[cfg["adapter"]](cfg)
    kept = [j for j in raw
            if (keep_all_levels or is_mid_level_swe(j)) and location_in_scope(j.location)]
    return raw, kept


def collect_all() -> list[Job]:
    found: list[Job] = []
    for cfg in COMPANIES:
        try:
            raw, kept = scrape_company(cfg)
        except Exception as e:  # noqa: BLE001
            log.error("%s: adapter error: %s", cfg["name"], e)
            continue
        log.info("%-14s scraped %4d roles, %3d match mid-level SWE", cfg["name"], len(raw), len(kept))
        found.extend(kept)
    return found


# --------------------------------------------------------------------------- #
# Storage (SQLite) — its own file AND its own table, separate from job_watcher
# --------------------------------------------------------------------------- #
def db_connect() -> sqlite3.Connection:
    # A background `--interval` loop and a manual `--once` can run at the same time, so
    # two processes may touch this file at once. WAL + a busy timeout let them share it
    # instead of failing with "database is locked".
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute(
        """CREATE TABLE IF NOT EXISTS seen_tech (
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
    return {row[0] for row in con.execute("SELECT key FROM seen_tech")}


def save_jobs(con: sqlite3.Connection, jobs: list[Job]) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con.executemany(
        "INSERT OR IGNORE INTO seen_tech VALUES (?,?,?,?,?,?,?,?)",
        [(j.key, j.company, j.job_id, j.title, j.location, j.department, j.url, now)
         for j in jobs],
    )
    con.commit()


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_email(new_jobs: list[Job], intro: str = "New mid-level roles:") -> bool:
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
    subject = f"[Tech] {len(new_jobs)} mid-level SWE role(s): {companies}"
    if len(subject) > 160:
        subject = f"[Tech] {len(new_jobs)} mid-level SWE role(s) at {len(by_company)} companies"

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
    msg.set_content(intro + "\n" + "\n".join(text_lines))
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
        log.info("Emailed %d mid-level role(s) to %s", len(new_jobs), to)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Email send failed: %s", e)
        return False


# --------------------------------------------------------------------------- #
# Core cycle
# --------------------------------------------------------------------------- #
def run_once(notify_seed: bool = False) -> None:
    con = db_connect()
    try:
        seen = load_seen_keys(con)
        is_first_run = len(seen) == 0

        current = collect_all()
        uniq: dict[str, Job] = {}
        for j in current:
            uniq.setdefault(j.key, j)
        current = list(uniq.values())

        new_jobs = [j for j in current if j.key not in seen]
        save_jobs(con, current)

        if not new_jobs:
            log.info("No new roles this pass (%d tracked).", len(current))
            return
        if is_first_run and not notify_seed:
            log.info("First run: seeded %d roles silently. Future postings will be emailed.",
                     len(new_jobs))
            return

        log.info("%d NEW role(s):", len(new_jobs))
        for j in new_jobs:
            log.info("   + [%s] %s — %s", j.company, j.title, j.url)
        send_email(new_jobs)
        sheets_sync.sync_jobs(new_jobs)
    finally:
        con.close()


def _source_fingerprint() -> tuple[int, int]:
    """(mtime, size) of this script — changes the moment a `git pull` rewrites it."""
    st = Path(__file__).resolve().stat()
    return (st.st_mtime_ns, st.st_size)


def _restart_if_source_changed(fingerprint: tuple[int, int]) -> tuple[int, int]:
    """Re-exec if this file changed on disk since the loop started.

    Python imports this module ONCE, so `COMPANIES` is bound at process start. A
    long-running `--interval` loop would therefore keep scraping the company list it
    loaded at boot — a `git pull` that adds companies would be silently ignored, forever,
    until someone restarted it. Detect the change and re-exec ourselves (same args, same
    env, same redirected stdout) so the next cycle picks up the new list.
    """
    current = _source_fingerprint()
    if current == fingerprint:
        return fingerprint

    path = Path(__file__).resolve()
    try:                                   # a broken pull must not kill a working watcher
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except (SyntaxError, OSError) as e:
        log.error("%s changed but won't compile (%s) — staying on the running version.",
                  path.name, e)
        return current                     # don't retry until it changes again

    log.info("%s changed on disk — restarting to pick up the new company list.", path.name)
    for h in log.handlers:
        h.flush()
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, str(path), *sys.argv[1:]])  # never returns


def run_loop(interval_min: int, notify_seed: bool) -> None:
    fingerprint = _source_fingerprint()
    log.info("Watching %d tech companies every %d min. Ctrl+C to stop.",
             len(COMPANIES), interval_min)
    while True:
        try:
            run_once(notify_seed=notify_seed)
        except Exception as e:  # noqa: BLE001
            log.error("Cycle failed: %s", e)
        try:
            time.sleep(interval_min * 60)
        except KeyboardInterrupt:
            log.info("Stopped.")
            return
        # A `git pull` may have added companies while we slept — reload if so.
        fingerprint = _restart_if_source_changed(fingerprint)


def find_company(query: str) -> dict | None:
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    q = norm(query)
    for c in COMPANIES:
        if norm(c["name"]) == q:
            return c
    partial = [c for c in COMPANIES if q and q in norm(c["name"])]
    return partial[0] if len(partial) == 1 else None


def run_company(query: str) -> int:
    """Scrape ONE company now, email any NEW mid-level roles, then store them.
    Postings are saved only after the email sends, so a failed send retries."""
    cfg = find_company(query)
    if not cfg:
        log.error("Unknown company %r. Try --list.", query)
        return 2
    try:
        raw, kept = scrape_company(cfg)
    except Exception as e:  # noqa: BLE001
        log.error("%s: adapter error: %s", cfg["name"], e)
        return 1
    log.info("%-14s scraped %4d roles, %3d mid-level SWE", cfg["name"], len(raw), len(kept))

    uniq: dict[str, Job] = {}
    for j in kept:
        uniq.setdefault(j.key, j)
    kept = list(uniq.values())

    con = db_connect()
    try:
        seen = load_seen_keys(con)
        new_jobs = [j for j in kept if j.key not in seen]
        if not new_jobs:
            log.info("%s: nothing new (%d already tracked) — no email sent.",
                     cfg["name"], len(kept))
            return 0
        log.info("%s: %d NEW posting(s):", cfg["name"], len(new_jobs))
        for j in new_jobs:
            log.info("   + %s — %s", j.title, j.url)
        if send_email(new_jobs):
            save_jobs(con, new_jobs)
            sheets_sync.sync_jobs(new_jobs)
            return 0
        log.warning("%s: email failed — NOT saving, so they'll retry next run.", cfg["name"])
        return 1
    finally:
        con.close()


def email_db() -> int:
    """Email every posting currently in the dedup store, scraping nothing.

    A plain snapshot of what the watcher already knows — useful after a silent
    seeding run. The store is left unchanged, so this does not affect which roles
    count as "new" on the next pass; you can safely run it more than once.
    """
    con = db_connect()
    try:
        rows = con.execute(
            "SELECT company, job_id, title, location, department, url FROM seen_tech "
            "ORDER BY company, title"
        ).fetchall()
    finally:
        con.close()

    if not rows:
        log.info("Store is empty — nothing to email. Run --once first to populate it.")
        return 0

    jobs = [Job(company=r[0], job_id=r[1], title=r[2], location=r[3] or "",
                department=r[4] or "", url=r[5] or "") for r in rows]
    log.info("Emailing %d tracked mid-level role(s)...", len(jobs))
    ok = send_email(jobs, intro="All mid-level roles currently in the database:")
    return 0 if ok else 1


def backfill_sheet() -> int:
    """Push every role already in the dedup store into the Google Sheets tracker.

    For the roles seeded by the first (silent) run, which were never emailed and
    so never synced. Each row keeps the date it was FIRST seen rather than today,
    so "Date Scraped" stays honest. Rows already in the sheet are skipped, so
    running this twice is harmless.
    """
    if not sheets_sync.is_enabled():
        log.error("GSHEET_CREDENTIALS is not set — see tech/SHEETS_SETUP.md.")
        return 2

    con = db_connect()
    try:
        rows = con.execute(
            "SELECT company, title, url, first_seen FROM seen_tech ORDER BY first_seen, company, title"
        ).fetchall()
    finally:
        con.close()

    if not rows:
        log.info("Store is empty — nothing to backfill. Run --once first to populate it.")
        return 0

    sheet_rows = [
        sheets_sync.SheetRow(
            company=r[0],
            job_name=r[1],
            date_scraped=(r[3] or "")[:10],       # the stored ISO timestamp, date part only
            app_link=r[2] or "",
        )
        for r in rows
    ]
    log.info("Backfilling %d tracked role(s) into the sheet...", len(sheet_rows))
    try:
        sheets_sync.append_rows(sheet_rows)
    except sheets_sync.SheetError as e:
        log.error("%s", e)
        return 2
    except Exception as e:  # noqa: BLE001
        log.error("Backfill failed: %s", e)
        return 1
    return 0


def fill_links(apply: bool = False) -> int:
    """Fill the App Link cell on tracker rows that name a job we have a URL for.

    For rows typed in by hand, which have a company and title but no link. The
    match is on company + title, since a linkless row has nothing else to go on.

    Dry run unless --apply is passed. Only ever writes into an EMPTY link cell,
    so a link already in the sheet is never overwritten.
    """
    if not sheets_sync.is_enabled():
        log.error("GSHEET_CREDENTIALS is not set — see tech/SHEETS_SETUP.md.")
        return 2

    con = db_connect()
    try:
        rows = con.execute(
            "SELECT company, title, url FROM seen_tech WHERE url IS NOT NULL AND url != ''"
        ).fetchall()
    finally:
        con.close()

    if not rows:
        log.info("Store is empty — nothing to match against. Run --once first.")
        return 0

    # company::title -> url. Two postings sharing a title at one company cannot be
    # told apart by a linkless row, so map those to "" and leave the row alone
    # rather than guess which posting the person meant.
    known: dict[str, str] = {}
    for company, title, url in rows:
        key = sheets_sync.dedup_key(company, title, "")
        if key in known and known[key] != url:
            known[key] = ""                      # ambiguous
        else:
            known.setdefault(key, url)

    ambiguous = sum(1 for v in known.values() if not v)
    if ambiguous:
        log.info("%d company+title pair(s) match more than one posting — skipping those.",
                 ambiguous)

    log.info("Matching against %d known job(s).", len(rows))
    try:
        sheets_sync.fill_links(known, apply=apply)
    except sheets_sync.SheetError as e:
        log.error("%s", e)
        return 2
    except Exception as e:  # noqa: BLE001
        log.error("Fill failed: %s", e)
        return 1
    return 0


def preview(query: str | None = None) -> int:
    """Scrape and PRINT the matching mid-level roles. Sends no email and never touches
    the dedup store — use it to sanity-check the filter before turning the watcher on."""
    cfgs = COMPANIES
    if query:
        cfg = find_company(query)
        if not cfg:
            log.error("Unknown company %r. Try --list.", query)
            return 2
        cfgs = [cfg]

    total = 0
    for cfg in cfgs:
        try:
            raw, kept = scrape_company(cfg)
        except Exception as e:  # noqa: BLE001
            log.error("%s: adapter error: %s", cfg["name"], e)
            continue
        if not kept:
            continue
        total += len(kept)
        print(f"\n{cfg['name']}  ({len(kept)} of {len(raw)} postings)")
        for j in sorted(kept, key=lambda x: x.title):
            loc = f" — {j.location}" if j.location else ""
            print(f"  • {j.title}{loc}")
            print(f"      {j.url}")
    print(f"\n{total} mid-level SWE role(s) across {len(cfgs)} company(ies).")
    return 0


# --------------------------------------------------------------------------- #
# Self-test (no network) — real titles taken from the live boards
# --------------------------------------------------------------------------- #
def selftest() -> int:
    ok = True
    J = lambda t, **kw: Job("X", "1", t, **kw)  # noqa: E731

    # --- keep: mid-level / unlevelled software roles ------------------------- #
    keep = [
        "Software Engineer",
        "Software Engineer II",
        "Software Engineer III, Payments",
        "Backend Engineer, Growth",
        "Full Stack Developer",
        "Android Engineer, Money Experience",            # Robinhood (real)
        "Member of Technical Staff (Software Engineer, Monetization)",  # Perplexity (real)
        "Design Engineer",                               # Replit (real)
        "Internal Tools Engineer",                       # 'intern' substring trap
        "Site Reliability Engineer",
        "Infrastructure Engineer, Platform",
    ]
    bad_keep = [t for t in keep if not is_mid_level_swe(J(t))]
    ok &= _check(f"keeps mid-level/unlevelled SWE (offenders: {bad_keep})", not bad_keep)

    # --- drop: senior tier --------------------------------------------------- #
    drop_senior = [
        "Senior Software Engineer",
        "Sr. Software Engineer, Backend",
        "Staff Software Engineer, AI Platform",          # Harvey (real)
        "Senior / Staff Fullstack Engineer",             # Linear (real)
        "Principal Engineer",
        "Lead Software Engineer",
        "Engineering Manager",
        "Application Systems Engineering Manager",       # Gusto (real)
        "Director, Solutions Architecture - Enterprise", # ClickHouse (real)
        "Head of Engineering",
        "Distinguished Engineer",
        "Software Architect",
    ]
    bad_senior = [t for t in drop_senior if is_mid_level_swe(J(t))]
    ok &= _check(f"drops senior tier (offenders: {bad_senior})", not bad_senior)

    # --- drop: entry level --------------------------------------------------- #
    drop_entry = [
        "Software Engineer Intern",
        "Software Engineering Internship - Summer 2027",
        "New Grad Software Engineer",
        "Junior Software Engineer",
        "Software Engineer, University Graduate",
        "Software Engineer Co-op",
    ]
    bad_entry = [t for t in drop_entry if is_mid_level_swe(J(t))]
    ok &= _check(f"drops entry level (offenders: {bad_entry})", not bad_entry)

    # --- drop: not software at all ------------------------------------------- #
    drop_nonswe = [
        "Account Executive, Mid Market",
        "Accountant, Capital Markets",
        "Sales Engineer",
        "Solutions Engineer, EMEA",
        "Customer Support Engineer",
        "Hardware Engineer",
        "Network Engineer",
        "Technical Recruiter",
        "Product Manager, Payments",
        # HR/finance "business partner" roles that name Engineering as the org they
        # support — all three were real false positives caught in the live DB.
        "Compensation Partner (Engineering)",              # Figma (real)
        "People Partner, Engineering",                     # Figma (real)
        "Finance & Strategy Partner, Central Engineering", # Stripe (real)
    ]
    bad_nonswe = [t for t in drop_nonswe if is_mid_level_swe(J(t))]
    ok &= _check(f"drops non-software roles (offenders: {bad_nonswe})", not bad_nonswe)

    # ...but a genuine software role containing the word "partner" must survive.
    keep_partner = [
        "Software Engineer II, Backend (Merchant & Partner Lifecycle)",  # Affirm (real)
        "Salesforce Developer, Partnerships",                            # Anthropic (real)
    ]
    bad_partner = [t for t in keep_partner if not is_mid_level_swe(J(t))]
    ok &= _check(f"keeps real SWE roles that merely contain 'partner' "
                 f"(offenders: {bad_partner})", not bad_partner)

    # --- ATS employmentType beats the title ---------------------------------- #
    ok &= _check("drops ATS-flagged intern even if the title looks mid",
                 is_mid_level_swe(J("Software Engineer", employment_type="Intern")) is False)

    # --- Greenhouse parse (flat /jobs) --------------------------------------- #
    gh = {"jobs": [
        {"id": 1, "title": "Software Engineer, Payments", "location": {"name": "New York, NY"},
         "absolute_url": "https://boards.greenhouse.io/stripe/jobs/1"},
        {"id": 2, "title": "Senior Software Engineer", "location": {"name": "Seattle"},
         "absolute_url": "https://boards.greenhouse.io/stripe/jobs/2"},
    ]}
    g = _parse_greenhouse(gh, "Stripe")
    ok &= _check("greenhouse parses 2 postings with URLs", len(g) == 2
                 and g[0].url.endswith("/jobs/1"))
    ok &= _check("greenhouse keeps the mid role, drops the senior one",
                 [j.title for j in g if is_mid_level_swe(j)] == ["Software Engineer, Payments"])

    # --- Lever parse (real record shape) ------------------------------------- #
    lv = [{"id": "abc-123", "text": "Software Engineer, Infrastructure",
           "hostedUrl": "https://jobs.lever.co/palantir/abc-123",
           "categories": {"commitment": "Full-time", "team": "Infrastructure",
                          "location": "Palo Alto, CA", "allLocations": ["Palo Alto, CA"]}},
          {"id": "def-456", "text": "Software Engineer, Intern",
           "hostedUrl": "https://jobs.lever.co/palantir/def-456",
           "categories": {"commitment": "Intern", "team": "Eng", "location": "NYC"}}]
    lj = _parse_lever(lv, "Palantir")
    ok &= _check("lever parses title/url/location/team", len(lj) == 2
                 and lj[0].title == "Software Engineer, Infrastructure"
                 and lj[0].url == "https://jobs.lever.co/palantir/abc-123"
                 and lj[0].location == "Palo Alto, CA"
                 and lj[0].department == "Infrastructure")
    ok &= _check("lever keeps mid role, drops the intern",
                 [j.title for j in lj if is_mid_level_swe(j)]
                 == ["Software Engineer, Infrastructure"])

    # --- Ashby parse (real record shape) ------------------------------------- #
    ab = {"jobs": [
        {"id": "u-1", "title": "Member of Technical Staff (Software Engineer, Monetization)",
         "location": "San Francisco", "department": "AI Products", "employmentType": "FullTime",
         "jobUrl": "https://jobs.ashbyhq.com/perplexity/u-1", "isListed": True},
        {"id": "u-2", "title": "Staff Software Engineer, AI Platform", "location": "New York",
         "department": "Eng", "employmentType": "FullTime",
         "jobUrl": "https://jobs.ashbyhq.com/perplexity/u-2", "isListed": True},
        {"id": "u-3", "title": "Software Engineer", "location": "SF", "employmentType": "FullTime",
         "jobUrl": "https://jobs.ashbyhq.com/perplexity/u-3", "isListed": False},
    ]}
    aj = _parse_ashby(ab, "Perplexity")
    ok &= _check("ashby skips unlisted postings (2 of 3)", len(aj) == 2)
    ok &= _check("ashby keeps MTS (mid IC), drops Staff",
                 [j.title for j in aj if is_mid_level_swe(j)]
                 == ["Member of Technical Staff (Software Engineer, Monetization)"])

    # --- US location filter --------------------------------------------------- #
    us_send = ["", "New York, NY", "San Francisco", "Remote - US", "Seattle, WA",
               "United States", "Austin, TX", "London, New York",
               "N/A",                                            # unplaceable -> send
               "New York, San Francisco, Seattle, or Remote (US/Canada)",  # Stripe (real)
               "US-Remote, Chicago, Seattle, San Francisco"]     # Stripe (real)
    us_drop = ["London, United Kingdom", "Bengaluru, India", "Toronto, Canada",
               "Dublin, Ireland", "Singapore", "EMEA",
               # regressions the first live preview caught: a foreign posting must not
               # be rescued by an unplaceable sibling location.
               "Toronto, CAN-Remote",                            # Stripe (real)
               "Belgrade, London, Berlin"]                       # Perplexity (real)
    bad_send = [x for x in us_send if not location_in_scope(x)]
    bad_drop = [x for x in us_drop if location_in_scope(x)]
    ok &= _check(f"US filter sends US/no-loc/unplaceable (offenders: {bad_send})", not bad_send)
    ok &= _check(f"US filter drops foreign, even w/ unplaceable sibling (offenders: {bad_drop})",
                 not bad_drop)

    # --- sheet sync: layout is resolved from the header row ------------------ #
    HDR6 = ["Company", "Job Name", "Status", "Date Scraped", "Date Applied", "App Link"]
    IMC = '=HYPERLINK("https://www.imc.com/us/careers/jobs/4818790101","Link")'

    # The template rows carry a bare "Link" in the App Link column hundreds of
    # rows past the last real entry, so the insert point keys on Company alone.
    grid6 = [HDR6,
             ["IMC Trading", "Graduate Software Engineer", "Rejected", "2026-07-01", "", IMC],
             ["", "", "", "", "", "Link"],
             ["", "", "", "", "", "Link"]]
    lay6 = sheets_sync._layout(HDR6)
    ok &= _check("sheet: insert row lands under the last Company, not the last 'Link'",
                 sheets_sync._first_free_row(grid6, lay6) == 3)
    ok &= _check("sheet: blank tab inserts at row 2 (under the header)",
                 sheets_sync._first_free_row([HDR6], lay6) == 2)

    ok &= _check("sheet: reads the URL back out of a HYPERLINK formula",
                 sheets_sync._hyperlink_url(IMC)
                 == "https://www.imc.com/us/careers/jobs/4818790101")
    ok &= _check("sheet: the 'Link' placeholder is not a URL",
                 sheets_sync._hyperlink_url("Link") == "")
    ok &= _check("sheet: a bare URL cell still reads as a URL",
                 sheets_sync._hyperlink_url("https://x.com/j/1") == "https://x.com/j/1")

    keys = sheets_sync._existing_keys(grid6, lay6)
    ok &= _check(f"sheet: only real rows count as tracked (got {len(keys)})", len(keys) == 1)
    ok &= _check("sheet: an already-tracked URL is recognised",
                 keys.holds("IMC Trading", "Graduate Software Engineer",
                            "https://www.imc.com/us/careers/jobs/4818790101/"))
    ok &= _check("sheet: a trailing slash does not create a duplicate",
                 sheets_sync.dedup_key("A", "B", "https://x.com/j/1")
                 == sheets_sync.dedup_key("A", "B", "https://x.com/j/1/"))

    # A row typed in by hand has no link, so it can only be matched on title. A
    # scraped job always HAS a link — match strictly on URL and the hand-added
    # row goes unrecognised and the same job is filed twice.
    hand = sheets_sync._existing_keys(
        [HDR6, ["Stripe", "Software Engineer II", "App sent", "", "2026-09-01", ""]], lay6)
    ok &= _check("sheet: a hand-added row with no link still blocks a duplicate",
                 hand.holds("Stripe", "Software Engineer II", "https://stripe.com/j/1"))
    ok &= _check("sheet: a different role at the same company is still new",
                 not hand.holds("Stripe", "Backend Engineer", "https://stripe.com/j/2"))
    # ...but two linked rows are matched on URL alone, so two genuinely different
    # postings that share a title are not collapsed into one.
    linked = sheets_sync._existing_keys(
        [HDR6, ["Stripe", "Software Engineer", "", "", "",
                '=HYPERLINK("https://stripe.com/j/1","Link")']], lay6)
    ok &= _check("sheet: a same-title posting with its own URL is not over-merged",
                 not linked.holds("Stripe", "Software Engineer", "https://stripe.com/j/9"))

    ROW = sheets_sync.SheetRow("Stripe", "Software Engineer II", "2026-09-15",
                               "https://stripe.com/j/1")
    LINK = '=HYPERLINK("https://stripe.com/j/1","Link")'
    ok &= _check("sheet: full 6-column tracker lays out in order",
                 ROW.to_values(lay6)
                 == ["Stripe", "Software Engineer II", "", "2026-09-15", "", LINK])
    ok &= _check("sheet: Status and Date Applied left blank for a scraped role",
                 ROW.to_values(lay6)[2] == "" and ROW.to_values(lay6)[4] == "")

    # The live tracker has no Date Scraped column. Positional writing would slide
    # the date into Date Applied and the link off the end of the row.
    HDR5 = ["Company", "Job Name", "Status", "Date Applied", "App Link"]
    lay5 = sheets_sync._layout(HDR5)
    ok &= _check("sheet: a missing Date Scraped column is dropped, not shifted",
                 ROW.to_values(lay5) == ["Stripe", "Software Engineer II", "", "", LINK])
    ok &= _check("sheet: missing column is reported as such",
                 lay5.missing_optional() == ["date_scraped"])
    ok &= _check("sheet: row width always matches the header width",
                 len(ROW.to_values(lay5)) == 5 and len(ROW.to_values(lay6)) == 6)

    # Reordered / widened headers must still land each value correctly.
    HDRX = ["App Link", "Notes", "Job Name", "Company", "Date Scraped"]
    layx = sheets_sync._layout(HDRX)
    ok &= _check("sheet: reordered headers still place every value right",
                 ROW.to_values(layx)
                 == [LINK, "", "Software Engineer II", "Stripe", "2026-09-15"])
    ok &= _check("sheet: dedup reads Company/App Link from their real columns",
                 sheets_sync._existing_keys([HDRX, [IMC, "x", "Grad SWE", "IMC Trading", ""]],
                                            layx).urls
                 == {"https://www.imc.com/us/careers/jobs/4818790101"})
    ok &= _check("sheet: header matching ignores case and stray whitespace",
                 sheets_sync._layout(["  COMPANY ", "job  name"]).index
                 == {"company": 0, "job_name": 1})

    try:
        sheets_sync._layout(["Company", "Status", "App Link"])
        ok &= _check("sheet: a header row with no Job Name is rejected", False)
    except sheets_sync.SheetError:
        ok &= _check("sheet: a header row with no Job Name is rejected", True)

    # --- --fill-links: only ever fills an EMPTY link cell on a real row ------- #
    fill_grid = [HDR6,
                 # 2: hand-added, no link, job is in the store -> fill
                 ["Stripe", "Software Engineer II", "App sent", "", "2026-09-01", ""],
                 # 3: hand-added, the placeholder counts as empty -> fill
                 ["Databricks", "Backend Engineer", "", "", "", "Link"],
                 # 4: already linked -> never touched
                 ["Airbnb", "Android Engineer", "", "", "", IMC],
                 # 5: no company -> a blank template row, not a job
                 ["", "", "", "", "", "Link"],
                 # 6: no link and the store has never seen it -> reported, not filled
                 ["Some Startup", "Founding Engineer", "", "", "", ""]]
    known = {sheets_sync.dedup_key("Stripe", "Software Engineer II", ""):
             "https://stripe.com/j/1",
             sheets_sync.dedup_key("Databricks", "Backend Engineer", ""):
             "https://databricks.com/j/2",
             sheets_sync.dedup_key("Airbnb", "Android Engineer", ""):
             "https://airbnb.com/j/3"}
    fills, skipped = sheets_sync.plan_link_fills(fill_grid, lay6, known)
    ok &= _check(f"fill-links: fills the two linkless rows (got {[f[0] for f in fills]})",
                 [(f[0], f[3]) for f in fills]
                 == [(2, "https://stripe.com/j/1"), (3, "https://databricks.com/j/2")])
    ok &= _check("fill-links: never overwrites a link already in the sheet",
                 4 not in [f[0] for f in fills])
    ok &= _check("fill-links: leaves blank template rows alone despite their 'Link'",
                 5 not in [f[0] for f in fills] and 5 not in [x[0] for x in skipped])
    ok &= _check("fill-links: reports a row no known job matches",
                 [x[0] for x in skipped] == [6])

    # A title shared by two postings at one company can't be resolved from a
    # linkless row, so it is mapped to "" upstream and must be left alone.
    amb, amb_skipped = sheets_sync.plan_link_fills(
        [HDR6, ["Stripe", "Software Engineer", "", "", "", ""]],
        lay6, {sheets_sync.dedup_key("Stripe", "Software Engineer", ""): ""})
    ok &= _check("fill-links: an ambiguous company+title is skipped, not guessed",
                 amb == [] and [x[0] for x in amb_skipped] == [2])

    ok &= _check("sheet: column letters for the write range",
                 [sheets_sync._col_letter(i) for i in (0, 4, 5, 25, 26)]
                 == ["A", "E", "F", "Z", "AA"])

    print("\nSELF-TEST:", "ALL PASSED ✅" if ok else "FAILURES ❌")
    return 0 if ok else 1


def _check(label: str, cond: bool) -> bool:
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
    return bool(cond)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Watch tech-company boards for new mid-level software roles.")
    ap.add_argument("--once", action="store_true", help="Run one pass then exit (for cron).")
    ap.add_argument("--interval", type=int, default=int(os.getenv("CHECK_EVERY_MINUTES", "180")),
                    help="Minutes between checks when looping (default 180).")
    ap.add_argument("--list", action="store_true", help="Print monitored companies and exit.")
    ap.add_argument("--company", metavar="NAME",
                    help="Scrape ONE company now (e.g. --company Stripe), email new roles, exit.")
    ap.add_argument("--preview", action="store_true",
                    help="Print matching roles WITHOUT emailing or touching the store. "
                         "Combine with --company to preview a single firm.")
    ap.add_argument("--email-db", action="store_true",
                    help="Email every role already in the store (no scraping), then exit.")
    ap.add_argument("--sheet-check", action="store_true",
                    help="Verify the Google Sheet credentials, tab and permissions "
                         "without writing anything, then exit.")
    ap.add_argument("--backfill-sheet", action="store_true",
                    help="Push every role already in the store into the Google Sheet, "
                         "then exit. Skips rows the sheet already has.")
    ap.add_argument("--fill-links", action="store_true",
                    help="Fill the App Link cell on sheet rows that name a known job "
                         "but have no link (e.g. rows you typed in by hand). Dry run "
                         "unless --apply is also passed.")
    ap.add_argument("--apply", action="store_true",
                    help="Commit the changes --fill-links would make.")
    ap.add_argument("--notify-seed", action="store_true",
                    help="Email on the first (seeding) run too.")
    ap.add_argument("--selftest", action="store_true", help="Run offline tests and exit.")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.preview:
        return preview(args.company)
    if args.email_db:
        return email_db()
    if args.sheet_check:
        return sheets_sync.check()
    if args.backfill_sheet:
        return backfill_sheet()
    if args.fill_links:
        return fill_links(apply=args.apply)
    if args.apply:
        log.error("--apply only means something with --fill-links.")
        return 2
    if args.list:
        by_ats: dict[str, list[str]] = {}
        for c in COMPANIES:
            by_ats.setdefault(c["adapter"], []).append(c["name"])
        print(f"Monitoring {len(COMPANIES)} tech companies for mid-level SWE roles:")
        for ats in sorted(by_ats):
            print(f"\n  {ats} ({len(by_ats[ats])}):")
            for name in sorted(by_ats[ats]):
                print(f"    • {name}")
        return 0
    if args.company:
        return run_company(args.company)

    if args.once:
        run_once(notify_seed=args.notify_seed)
    else:
        run_loop(args.interval, notify_seed=args.notify_seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
