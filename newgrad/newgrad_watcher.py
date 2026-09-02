#!/usr/bin/env python3
"""
newgrad_watcher.py — Watch tech-company job boards for NEW GRADUATE software roles
and email you when something new appears.

Third sibling of job_watcher.py (quant/HFT new-grad + events) and tech_watcher.py
(tech mid-level). Same design, different target — and deliberately the *inverse* of
tech_watcher: that one throws new-grad titles away, this one keeps only those.

Four public ATS feeds cover every firm below — no scraping, no browser:

  * Greenhouse public JSON API  -> Stripe, Databricks, Anthropic, Airbnb, ... (70)
  * Lever public JSON API       -> Palantir, Shield AI, Zoox, ... (6)
  * Ashby public posting API    -> OpenAI, Notion, Cerebras, Zip, ... (43)
  * Workday CXS JSON API        -> NVIDIA, Micron, Adobe (3)

Workday matters: the Greenhouse/Lever/Ashby universe misses the biggest new-grad
employers entirely. NVIDIA alone posts ~40 "New College Grad" roles.

What counts as "new grad"
-------------------------
A title is kept when it looks like software engineering, is neither senior-tier nor
an internship, AND carries an entry signal — either an explicit label
("New Grad", "New College Grad", "University Graduate", "Campus", "Early Career")
or an unlevelled entry title ("Software Engineer I", "Associate Software Engineer").

Four title traps this handles that naive substring matching gets wrong:
  * "New College Grad" does NOT contain "new grad" — NVIDIA's phrasing needs its own
    alternative, or every NVIDIA role is silently missed.
  * "Software Engineer II" must NOT match the "Engineer I" rule (\bI\b won't match
    the token "II", plus a belt-and-braces negative lookahead).
  * "Internal Tools Engineer" contains "intern" -> kept (word-boundary matching).
  * "Member of Technical Staff" contains "staff" -> not senior (carved out first).

HARDWARE_RE is load-bearing, not cosmetic: NVIDIA's new-grad board is dominated by
ASIC / circuit / verification / DFT roles. Without it ~3 in 4 NVIDIA matches are junk.

Its own dedup store (seen_newgrad_jobs.sqlite3, table `seen_newgrad`), so it never
interferes with either sibling's store.

Run
---
    python newgrad_watcher.py --preview        # print matches, no email, no DB write
    python newgrad_watcher.py --once           # single pass (use with launchd/cron)
    python newgrad_watcher.py --interval 120   # loop forever, check every 120 min
    python newgrad_watcher.py --list           # show what will be monitored
    python newgrad_watcher.py --company NVIDIA # scrape ONE firm now
    python newgrad_watcher.py --selftest       # run parser/filter tests, no network

To run unattended with no terminal open, see ./install_agent.sh (macOS launchd).

Email is configured via the same environment variables as its siblings
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

# --------------------------------------------------------------------------- #
# Paths / logging
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "seen_newgrad_jobs.sqlite3"   # own store, separate from siblings
LOG_PATH = BASE_DIR / "newgrad_watcher.log"

log = logging.getLogger("newgrad_watcher")
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
# What counts as a "new grad software" role
#   Word-boundary regexes throughout, so "Internal Tools Engineer" is NOT read as
#   an intern and "Software Engineer II" is NOT read as "Engineer I".
# --------------------------------------------------------------------------- #
# 1. Must look like software engineering.
SWE_RE = re.compile(
    r"\b(software|swe|engineer|engineering|developer|programmer|sre|devops"
    r"|member\s+of\s+technical\s+staff)\b", re.I)

# 2. ...but NOT one of these non-software "engineer" roles.
#    The trailing "<function> ... partner" clause kills HR/finance business-partner
#    roles that merely name Engineering as the org they support — e.g. "Compensation
#    Partner (Engineering)". It deliberately does NOT fire on real software roles
#    that happen to contain "partner".
NON_SWE_RE = re.compile(
    r"\b(sales|solutions?|customer|support|field|implementation|deployment|hardware"
    r"|mechanical|electrical|manufacturing|process|chemical|civil|industrial|optical"
    r"|rf|qa|quality|network|validation)\s+engineer(ing)?\b"
    r"|\bsolutions?\s+architect\b|\baccount\s+executive\b|\brecruit"
    r"|\b(compensation|people|talent|hr|human\s+resources|finance|strategy|business)\b"
    r".*\bpartner\b", re.I)

# 3. ...and NOT a hardware / silicon / QA role.
#    This one exists because of Workday. NVIDIA and Micron post their new-grad reqs
#    on the same board as their silicon roles, and those titles all say "Engineer":
#    "ASIC Design Engineer - New College Grad 2026", "Formal Verification Engineer",
#    "DFT Engineer", "Circuit Design Engineer". Without this, ~30 of NVIDIA's 40
#    New College Grad hits are false positives.
#    NOTE: "Compiler Engineer" and "Systems Software Engineer" must survive it —
#    those are real software roles on the same board.
HARDWARE_RE = re.compile(
    # Bare domain words, matched anywhere in the title. NON_SWE_RE only catches the
    # "<domain> Engineer" word order, which misses Shield AI's "Engineer I, Electrical
    # Integration & Test" and "Associate Engineer, Manufacturing" — same roles, comma
    # the other way round. A genuine "Manufacturing Software Engineer" is still rescued
    # by SOFTWARE_TITLE_RE below.
    r"\b(asic|rtl|soc|vlsi|dft|fpga|silicon|semiconductor|photonic|analog"
    r"|mixed[\s-]signal|wafer|foundry|manufacturing|electrical|mechanical"
    r"|metrology|lithography)\b"
    r"|\b(circuit|physical|power|thermal|board|package|layout)\s+design\b"
    r"|\b(circuit|power|thermal|weld|test|verification|reliability|automation)"
    r"\s+engineer(ing)?\b"
    r"|\bdesign\s+verification\b|\bquality\s+assurance\b"
    r"|\b(gpu|cpu|chip)\s+(power|architecture|verification)\b", re.I)

# ...but an explicit "Software Engineer" outranks a domain keyword. NVIDIA posts
# "System Software Engineer, SOC" — a real software role that HARDWARE_RE's \bsoc\b
# would otherwise throw away. Note this deliberately does NOT rescue "Software
# Quality Assurance Engineer": that is "software" + "QA engineer", not "software
# engineer", so the pattern below doesn't match it.
SOFTWARE_TITLE_RE = re.compile(
    r"\bsoftware\s+(engineer|developer|development\s+engineer)\b", re.I)

# A much stricter "is this actually software?" gate, switched on per-company with
# "strict_swe": True.
#
# SWE_RE accepts a bare "engineer", which is right at Notion or Stripe (nearly every
# Engineer there writes software) and catastrophic at a chip maker. Micron's board
# returns 23 New College Grad "Engineer" roles of which exactly ONE is software —
# the rest are DRAM Design, Device, Equipment, Process Integration, Wet Etch/CMP/Bond
# Shift. Blocklisting each of those is unwinnable whack-a-mole, so hardware-heavy
# firms instead have to name a real software discipline in the title.
STRICT_SWE_RE = re.compile(
    r"\b(software|swe|developer|programmer|compiler|kernel|firmware|driver"
    r"|sre|devops|full[\s-]?stack|back[\s-]?end|front[\s-]?end|web|mobile|ios|android"
    r"|cloud|data\s+engineer|machine\s+learning|deep\s+learning|ml\s+engineer"
    r"|platform\s+engineer|infrastructure\s+engineer|systems?\s+software"
    r"|security\s+engineer)\b", re.I)

# 4. ...and NOT senior-tier.
SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|leader|director|manager|head|vp"
    r"|vice\s+president|distinguished|fellow|architect|chief|president|executive)\b", re.I)

# 5. ...and NOT an internship. Note this is much NARROWER than tech_watcher's
#    ENTRY_RE: we deliberately keep new-grad/campus/junior, and drop only interns.
INTERN_RE = re.compile(
    r"\b(intern|interns|internship|co-?op|apprentice|trainee|working\s+student"
    r"|placement|praktikum|summer\s+20\d{2}|winter\s+20\d{2})\b", re.I)

# 6. ...and MUST carry an entry signal — either (a) an explicit new-grad label...
#    "new college grad" needs its own alternative: it does NOT contain "new grad".
NEWGRAD_RE = re.compile(
    r"\bnew[\s-]?college[\s-]?grad(uate)?s?\b"          # NVIDIA / Micron phrasing
    r"|\bnew[\s-]?grad(uate)?s?\b"
    r"|\bnewgrad\b"
    r"|\b(college|university)[\s-]?grad(uate)?s?\b"
    r"|\buniversity\s+(hire|hiring|recruiting)\b"
    r"|\bcampus\b"
    r"|\bearly[\s-]?career\b"
    r"|\bentry[\s-]?level\b"
    r"|\brotational\b"
    r"|\bgrad(uate)?\s+(program|scheme|rotation)\b"
    r"|\bgraduate\s+(software|engineer)"
    r"|\bclass\s+of\s+20\d{2}\b"
    r"|\b20\d{2}\s+(start|grad)", re.I)

# ...or (b) an unlevelled entry title. "Software Engineer I" is how plenty of firms
#    ship a new-grad req without ever saying "new grad".
#    \bI\b already refuses to match the token "II"; the lookahead is belt-and-braces.
ENTRY_LEVEL_RE = re.compile(
    r"\b(engineer|developer)\s*(?:I|1)\b(?![IVX])"
    r"|\bassociate\s+(?:software\s+|platform\s+|data\s+|systems\s+)?(?:engineer|developer)\b"
    r"|\b(engineer|developer)\s*[-,]?\s*level\s*1\b", re.I)

# "Member of Technical Staff" is a mid-level IC title at Perplexity/Anthropic/etc,
# but it contains the word "staff". Strip it before the seniority check.
_MTS_RE = re.compile(r"member\s+of\s+technical\s+staff", re.I)

# ATS-provided employment types that are never full-time new-grad roles.
_BAD_EMPLOYMENT = ("intern", "temporary", "contract")


@dataclass
class Job:
    company: str
    job_id: str
    title: str
    location: str = ""
    department: str = ""
    url: str = ""
    employment_type: str = ""      # Ashby/Lever expose this; Greenhouse/Workday do not

    @property
    def key(self) -> str:
        return f"{self.company}::{self.job_id}"


def is_newgrad_swe(job: Job, strict_swe: bool = False) -> bool:
    """A software role that is entry-level and not an internship.

    Matches on the TITLE only — department is unreliable, since at a tech company
    almost everyone sits under "Engineering" (which would let recruiters through).

    `strict_swe` is set per-company for hardware-heavy firms (NVIDIA, Micron), where
    a bare "Engineer" is far more likely to mean silicon than software.
    """
    title = job.title or ""
    if not (STRICT_SWE_RE if strict_swe else SWE_RE).search(title):
        return False                          # not a software role at all
    if NON_SWE_RE.search(title):
        return False                          # sales/solutions/hardware/... "engineer"
    if HARDWARE_RE.search(title) and not SOFTWARE_TITLE_RE.search(title):
        return False                          # ASIC/circuit/verification/QA
    if SENIOR_RE.search(_MTS_RE.sub(" ", title)):
        return False                          # senior / staff / principal / manager
    if INTERN_RE.search(title):
        return False                          # intern / co-op / apprentice
    if any(b in (job.employment_type or "").lower() for b in _BAD_EMPLOYMENT):
        return False                          # ATS says intern/contract outright
    return bool(NEWGRAD_RE.search(title) or ENTRY_LEVEL_RE.search(title))


# --------------------------------------------------------------------------- #
# Company configuration — every board token below was verified live against its
# ATS API. Adding a firm is a one-liner; find its token in its careers-page URL:
#   boards.greenhouse.io/<token>   jobs.lever.co/<token>   jobs.ashbyhq.com/<token>
#
# Workday is different: the tenant is usually guessable but the SITE path is not.
# An HTTP 422 means the tenant is right and the site is wrong — read the real path
# out of the company's careers URL (myworkdayjobs.com/<site>) rather than guessing.
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
    {"name": "Adyen",        "adapter": "greenhouse", "board_token": "adyen"},
    {"name": "Toast",        "adapter": "greenhouse", "board_token": "toast"},
    {"name": "Epic Games",   "adapter": "greenhouse", "board_token": "epicgames"},
    {"name": "Riot Games",   "adapter": "greenhouse", "board_token": "riotgames"},
    {"name": "Intercom",     "adapter": "greenhouse", "board_token": "intercom"},
    {"name": "Ripple",       "adapter": "greenhouse", "board_token": "ripple"},
    {"name": "Remote",       "adapter": "greenhouse", "board_token": "remotecom"},
    {"name": "Upstart",      "adapter": "greenhouse", "board_token": "upstart"},
    {"name": "Smartsheet",   "adapter": "greenhouse", "board_token": "smartsheet"},
    {"name": "OpenTable",    "adapter": "greenhouse", "board_token": "opentable"},
    {"name": "Mixpanel",     "adapter": "greenhouse", "board_token": "mixpanel"},
    {"name": "Hightouch",    "adapter": "greenhouse", "board_token": "hightouch"},
    {"name": "Sigma Computing", "adapter": "greenhouse", "board_token": "sigmacomputing"},
    {"name": "New Relic",    "adapter": "greenhouse", "board_token": "newrelic"},
    {"name": "Fastly",       "adapter": "greenhouse", "board_token": "fastly"},
    {"name": "Fireblocks",   "adapter": "greenhouse", "board_token": "fireblocks"},
    {"name": "LaunchDarkly", "adapter": "greenhouse", "board_token": "launchdarkly"},
    {"name": "Zocdoc",       "adapter": "greenhouse", "board_token": "zocdoc"},
    {"name": "Checkr",       "adapter": "greenhouse", "board_token": "checkr"},
    {"name": "Salesloft",    "adapter": "greenhouse", "board_token": "salesloft"},
    {"name": "Culture Amp",  "adapter": "greenhouse", "board_token": "cultureamp"},
    {"name": "SingleStore",  "adapter": "greenhouse", "board_token": "singlestore"},
    {"name": "Customer.io",  "adapter": "greenhouse", "board_token": "customerio"},
    {"name": "Bitwarden",    "adapter": "greenhouse", "board_token": "bitwarden"},
    {"name": "6sense",       "adapter": "greenhouse", "board_token": "6sense"},
    {"name": "Pendo",        "adapter": "greenhouse", "board_token": "pendo"},
    {"name": "Flatiron Health", "adapter": "greenhouse", "board_token": "flatironhealth"},
    {"name": "Khan Academy", "adapter": "greenhouse", "board_token": "khanacademy"},
    {"name": "Nextdoor",     "adapter": "greenhouse", "board_token": "nextdoor"},
    {"name": "Slice",        "adapter": "greenhouse", "board_token": "slice"},
    {"name": "Coursera",     "adapter": "greenhouse", "board_token": "coursera"},
    {"name": "Honeycomb",    "adapter": "greenhouse", "board_token": "honeycomb"},
    {"name": "Dashlane",     "adapter": "greenhouse", "board_token": "dashlane"},
    {"name": "AssemblyAI",   "adapter": "greenhouse", "board_token": "assemblyai"},
    {"name": "Udemy",        "adapter": "greenhouse", "board_token": "udemy"},
    {"name": "Lattice",      "adapter": "greenhouse", "board_token": "lattice"},
    {"name": "Make",         "adapter": "greenhouse", "board_token": "make"},
    {"name": "Doximity",     "adapter": "greenhouse", "board_token": "doximity"},
    {"name": "Sendbird",     "adapter": "greenhouse", "board_token": "sendbird"},
    {"name": "Motive",       "adapter": "greenhouse", "board_token": "motive"},
    {"name": "OfferUp",      "adapter": "greenhouse", "board_token": "offerup"},
    {"name": "Instabase",    "adapter": "greenhouse", "board_token": "instabase"},
    {"name": "Buildkite",    "adapter": "greenhouse", "board_token": "buildkite"},
    {"name": "CircleCI",     "adapter": "greenhouse", "board_token": "circleci"},
    {"name": "Imply",        "adapter": "greenhouse", "board_token": "imply"},
    {"name": "Dremio",       "adapter": "greenhouse", "board_token": "dremio"},
    {"name": "Netlify",      "adapter": "greenhouse", "board_token": "netlify"},
    {"name": "Mercari",      "adapter": "greenhouse", "board_token": "mercari"},
    {"name": "Oscar Health",  "adapter": "greenhouse", "board_token": "oscar"},
    {"name": "Via",           "adapter": "greenhouse", "board_token": "via"},
    {"name": "Cribl",         "adapter": "greenhouse", "board_token": "cribl"},
    {"name": "Everlaw",       "adapter": "greenhouse", "board_token": "everlaw"},
    {"name": "Parloa",        "adapter": "greenhouse", "board_token": "parloa"},
    {"name": "Roofstock",     "adapter": "greenhouse", "board_token": "roofstock"},
    {"name": "Jumio",         "adapter": "greenhouse", "board_token": "jumio"},
    {"name": "Torq",          "adapter": "greenhouse", "board_token": "torq"},
    {"name": "Bird",          "adapter": "greenhouse", "board_token": "bird"},
    {"name": "Alloy",         "adapter": "greenhouse", "board_token": "alloy"},
    {"name": "Tines",         "adapter": "greenhouse", "board_token": "tines"},
    {"name": "Amperity",      "adapter": "greenhouse", "board_token": "amperity"},
    {"name": "Greenhouse",    "adapter": "greenhouse", "board_token": "greenhouse"},
    {"name": "Spin",          "adapter": "greenhouse", "board_token": "spin"},
    {"name": "StackBlitz",    "adapter": "greenhouse", "board_token": "stackblitz"},
    {"name": "Tavily",        "adapter": "greenhouse", "board_token": "tavily"},
    {"name": "Galileo",       "adapter": "greenhouse", "board_token": "galileo"},
    {"name": "Suki",          "adapter": "greenhouse", "board_token": "suki"},
    {"name": "Veriff",        "adapter": "greenhouse", "board_token": "veriff"},
    {"name": "Blend",         "adapter": "greenhouse", "board_token": "blend"},
    {"name": "Descript",      "adapter": "greenhouse", "board_token": "descript"},
    {"name": "Branch",        "adapter": "greenhouse", "board_token": "branch"},
    {"name": "OpenSpace",     "adapter": "greenhouse", "board_token": "openspace"},
    {"name": "Lithic",        "adapter": "greenhouse", "board_token": "lithic"},
    {"name": "Ghost",         "adapter": "greenhouse", "board_token": "ghost"},
    {"name": "Forward",       "adapter": "greenhouse", "board_token": "forward"},
    {"name": "Tigera",        "adapter": "greenhouse", "board_token": "tigera"},
    {"name": "Pacaso",        "adapter": "greenhouse", "board_token": "pacaso"},
    {"name": "Highnote",      "adapter": "greenhouse", "board_token": "highnote"},
    {"name": "Grailed",       "adapter": "greenhouse", "board_token": "grailed"},
    {"name": "Knock",         "adapter": "greenhouse", "board_token": "knock"},
    {"name": "Prisma",        "adapter": "greenhouse", "board_token": "prisma"},
    {"name": "Revel",         "adapter": "greenhouse", "board_token": "revel"},

    # ---- Lever ------------------------------------------------------------- #
    {"name": "Palantir",     "adapter": "lever", "board_token": "palantir"},
    {"name": "Shield AI",    "adapter": "lever", "board_token": "shieldai"},
    {"name": "Zoox",         "adapter": "lever", "board_token": "zoox"},
    {"name": "Outreach",     "adapter": "lever", "board_token": "outreach"},
    {"name": "Wealthfront",  "adapter": "lever", "board_token": "wealthfront"},
    {"name": "Ro",           "adapter": "lever", "board_token": "ro"},
    {"name": "Spotify",      "adapter": "lever", "board_token": "spotify"},
    {"name": "Gopuff",       "adapter": "lever", "board_token": "gopuff"},
    {"name": "Anchorage Digital", "adapter": "lever", "board_token": "anchorage"},
    {"name": "AngelList",    "adapter": "lever", "board_token": "angellist"},
    {"name": "Secureframe",  "adapter": "lever", "board_token": "secureframe"},
    {"name": "LogRocket",    "adapter": "lever", "board_token": "logrocket"},
    {"name": "Olo",          "adapter": "lever", "board_token": "olo"},
    {"name": "15Five",       "adapter": "lever", "board_token": "15five"},
    {"name": "Lyra Health",  "adapter": "lever", "board_token": "lyrahealth"},
    {"name": "Pipedrive",    "adapter": "lever", "board_token": "pipedrive"},
    {"name": "Sysdig",       "adapter": "lever", "board_token": "sysdig"},
    {"name": "Veo",          "adapter": "lever", "board_token": "veo"},
    {"name": "Payactiv",     "adapter": "lever", "board_token": "payactiv"},

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
    {"name": "Deepgram",     "adapter": "ashby", "board_token": "deepgram"},
    {"name": "Baseten",      "adapter": "ashby", "board_token": "baseten"},
    {"name": "ClickUp",      "adapter": "ashby", "board_token": "clickup"},
    {"name": "Suno",         "adapter": "ashby", "board_token": "suno"},
    {"name": "Drata",        "adapter": "ashby", "board_token": "drata"},
    {"name": "Thumbtack",    "adapter": "ashby", "board_token": "thumbtack"},
    {"name": "Hex",          "adapter": "ashby", "board_token": "hex"},
    {"name": "Alchemy",      "adapter": "ashby", "board_token": "alchemy"},
    {"name": "Anyscale",     "adapter": "ashby", "board_token": "anyscale"},
    {"name": "Redis",        "adapter": "ashby", "board_token": "redis"},
    {"name": "Midjourney",   "adapter": "ashby", "board_token": "midjourney"},
    {"name": "Poolside",     "adapter": "ashby", "board_token": "poolside"},
    {"name": "PostHog",      "adapter": "ashby", "board_token": "posthog"},
    {"name": "Pika",         "adapter": "ashby", "board_token": "pika"},
    {"name": "Circle",       "adapter": "ashby", "board_token": "circle"},
    {"name": "Pinecone",     "adapter": "ashby", "board_token": "pinecone"},
    {"name": "Monte Carlo",  "adapter": "ashby", "board_token": "montecarlodata"},
    {"name": "InfluxData",   "adapter": "ashby", "board_token": "influxdata"},
    {"name": "Materialize",  "adapter": "ashby", "board_token": "materialize"},
    {"name": "FullStory",    "adapter": "ashby", "board_token": "fullstory"},
    {"name": "OpenSea",      "adapter": "ashby", "board_token": "opensea"},
    {"name": "Weaviate",     "adapter": "ashby", "board_token": "weaviate"},
    {"name": "Expensify",    "adapter": "ashby", "board_token": "expensify"},
    {"name": "Legora",       "adapter": "ashby", "board_token": "legora"},
    {"name": "LangChain",    "adapter": "ashby", "board_token": "langchain"},
    {"name": "Alan",         "adapter": "ashby", "board_token": "alan"},
    {"name": "Socure",       "adapter": "ashby", "board_token": "socure"},
    {"name": "Headway",      "adapter": "ashby", "board_token": "headway"},
    {"name": "Commure",      "adapter": "ashby", "board_token": "commure"},
    {"name": "Exa",          "adapter": "ashby", "board_token": "exa"},
    {"name": "Attio",        "adapter": "ashby", "board_token": "attio"},
    {"name": "Rain",         "adapter": "ashby", "board_token": "rain"},
    {"name": "Abridge",      "adapter": "ashby", "board_token": "abridge"},
    {"name": "Sardine",      "adapter": "ashby", "board_token": "sardine"},
    {"name": "n8n",          "adapter": "ashby", "board_token": "n8n"},
    {"name": "Vapi",         "adapter": "ashby", "board_token": "vapi"},
    {"name": "Gamma",        "adapter": "ashby", "board_token": "gamma"},
    {"name": "Elliptic",     "adapter": "ashby", "board_token": "elliptic"},
    {"name": "Plane",        "adapter": "ashby", "board_token": "plane"},
    {"name": "Coder",        "adapter": "ashby", "board_token": "coder"},
    {"name": "DailyPay",     "adapter": "ashby", "board_token": "dailypay"},
    {"name": "RunPod",       "adapter": "ashby", "board_token": "runpod"},
    {"name": "Braintrust",   "adapter": "ashby", "board_token": "braintrust"},
    {"name": "Column",       "adapter": "ashby", "board_token": "column"},
    {"name": "Middesk",      "adapter": "ashby", "board_token": "middesk"},
    {"name": "Numeric",      "adapter": "ashby", "board_token": "numeric"},
    {"name": "Bland",        "adapter": "ashby", "board_token": "bland"},
    {"name": "Nabla",        "adapter": "ashby", "board_token": "nabla"},
    {"name": "Persona",      "adapter": "ashby", "board_token": "persona"},
    {"name": "LlamaIndex",   "adapter": "ashby", "board_token": "llamaindex"},
    {"name": "Resend",       "adapter": "ashby", "board_token": "resend"},
    {"name": "Substack",     "adapter": "ashby", "board_token": "substack"},
    {"name": "lemlist",      "adapter": "ashby", "board_token": "lemlist"},
    {"name": "Astra",        "adapter": "ashby", "board_token": "astra"},
    {"name": "Patreon",      "adapter": "ashby", "board_token": "patreon"},
    {"name": "Browserbase",  "adapter": "ashby", "board_token": "browserbase"},
    {"name": "E2B",          "adapter": "ashby", "board_token": "e2b"},
    {"name": "Clair",        "adapter": "ashby", "board_token": "clair"},
    {"name": "Vector",       "adapter": "ashby", "board_token": "vector"},
    {"name": "Close",        "adapter": "ashby", "board_token": "close"},
    {"name": "neptune.ai",   "adapter": "ashby", "board_token": "neptune"},
    {"name": "Wistia",       "adapter": "ashby", "board_token": "wistia"},

    # ---- Hardware-heavy firms: these need "strict_swe" ---------------------- #
    # Aerospace/defense/auto/robotics/wearable boards are dominated by non-software
    # "Engineer" titles. SpaceX posts 16 "New Graduate Engineer" roles of which only
    # 10 are software — the rest are Propulsion, GNC, Civil/Structural, Launch & Test.
    {"name": "SpaceX",       "adapter": "greenhouse", "board_token": "spacex", "strict_swe": True},
    {"name": "Astranis",     "adapter": "greenhouse", "board_token": "astranis", "strict_swe": True},
    {"name": "Lucid Motors", "adapter": "greenhouse", "board_token": "lucidmotors", "strict_swe": True},
    {"name": "Helsing",      "adapter": "greenhouse", "board_token": "helsing", "strict_swe": True},
    {"name": "Epirus",       "adapter": "greenhouse", "board_token": "epirus", "strict_swe": True},
    {"name": "Ursa Major",   "adapter": "greenhouse", "board_token": "ursamajor", "strict_swe": True},
    {"name": "Kodiak Robotics", "adapter": "greenhouse", "board_token": "kodiak", "strict_swe": True},
    {"name": "Hermeus",      "adapter": "lever", "board_token": "hermeus", "strict_swe": True},
    {"name": "Waabi",        "adapter": "lever", "board_token": "waabi", "strict_swe": True},
    {"name": "Skydio",       "adapter": "ashby", "board_token": "skydio", "strict_swe": True},
    {"name": "WHOOP",        "adapter": "ashby", "board_token": "whoop", "strict_swe": True},
    {"name": "insitro",      "adapter": "ashby", "board_token": "insitro", "strict_swe": True},
    # Chip / quantum / drone firms — same reasoning as NVIDIA and SpaceX.
    {"name": "Tenstorrent",  "adapter": "greenhouse", "board_token": "tenstorrent", "strict_swe": True},
    {"name": "Graphcore",    "adapter": "greenhouse", "board_token": "graphcore", "strict_swe": True},
    {"name": "Lightmatter",  "adapter": "greenhouse", "board_token": "lightmatter", "strict_swe": True},
    {"name": "PsiQuantum",   "adapter": "greenhouse", "board_token": "psiquantum", "strict_swe": True},
    {"name": "IonQ",         "adapter": "greenhouse", "board_token": "ionq", "strict_swe": True},
    {"name": "Wing",         "adapter": "greenhouse", "board_token": "wing", "strict_swe": True},
    {"name": "Rigetti",      "adapter": "lever", "board_token": "rigetti", "strict_swe": True},
    {"name": "Etched",       "adapter": "ashby", "board_token": "etched", "strict_swe": True},
    {"name": "Extropic",     "adapter": "ashby", "board_token": "extropic", "strict_swe": True},

    # ---- Big employers with their own career APIs (no ATS board) ------------ #
    # Amazon is the largest new-grad SWE employer in the US and Netflix has no
    # ATS board either; both are reachable through public JSON endpoints.
    {"name": "Amazon",       "adapter": "amazon"},
    {"name": "Netflix",      "adapter": "netflix"},

    # ---- Workday (big-tech new-grad hiring the ATS boards above never see) -- #
    # All three verified live against the CXS API. Adobe shows 0 new-grad roles
    # off-season — the board is live, the reqs are seasonal, so it stays in.
    # NVIDIA and Micron need "strict_swe": their boards are mostly silicon roles that
    # all say "Engineer". Without it, 22 of Micron's 23 new-grad hits are DRAM/etch/
    # equipment roles. See STRICT_SWE_RE.
    {"name": "NVIDIA",  "adapter": "workday", "strict_swe": True,
     "wd_host": "nvidia.wd5.myworkdayjobs.com", "wd_tenant": "nvidia",
     "wd_site": "NVIDIAExternalCareerSite"},
    {"name": "Micron",  "adapter": "workday", "strict_swe": True,
     "wd_host": "micron.wd1.myworkdayjobs.com", "wd_tenant": "micron",
     "wd_site": "External"},
    {"name": "Adobe",   "adapter": "workday",
     "wd_host": "adobe.wd5.myworkdayjobs.com", "wd_tenant": "adobe",
     "wd_site": "external_experienced"},
]


# --------------------------------------------------------------------------- #
# HTTP helpers with light retry
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


def http_post(url: str, json_body: dict | None = None, tries: int = 3, timeout: int = 30) -> requests.Response:
    last = None
    headers = {**HEADERS, "Content-Type": "application/json"}
    for attempt in range(1, tries + 1):
        try:
            r = requests.post(url, json=json_body or {}, headers=headers, timeout=timeout)
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


# --------------------------------------------------------------------------- #
# Adapter: Workday CXS JSON API  (ported from quant/job_watcher.py)
#   POST /wday/cxs/<tenant>/<site>/jobs  {"limit":20,"offset":N,"searchText":""}
#   Workday caps `limit` at 20, so a big board is genuinely many round-trips.
#   We pull the whole board and filter locally rather than trusting `searchText`,
#   whose matching is fuzzy enough to drop real hits.
# --------------------------------------------------------------------------- #
def _parse_workday_jobs(payload: dict, company: str, host: str, site: str) -> list[Job]:
    out: list[Job] = []
    for jp in (payload or {}).get("jobPostings", []):
        ext = (jp.get("externalPath") or "").strip()
        bullets = jp.get("bulletFields") or []
        jid = str(bullets[0] if bullets else ext).strip()
        if not jid:
            continue
        url = f"https://{host}/en-US/{site}{ext}" if ext else f"https://{host}/{site}"
        out.append(Job(company=company, job_id=jid,
                       title=(jp.get("title") or "").strip(),
                       location=(jp.get("locationsText") or "").strip(),
                       url=url))
    return out


def fetch_workday(cfg: dict) -> list[Job]:
    """Page through a Workday board.

    Trust `total` from the FIRST page only. Several tenants (NVIDIA, Salesforce, eBay,
    PayPal, ...) report a real total on page 1 and then `"total": 0` on every page after
    it. Re-reading it each time makes `offset + limit >= total` true at offset 20, which
    silently truncates the board to 40 postings — NVIDIA was returning 40 of its ~2000.
    Micron happens to report `total` consistently, which is why the bug hid there.

    An empty page is the backstop, for any tenant that never reports a usable total.
    """
    host, tenant, site = cfg["wd_host"], cfg["wd_tenant"], cfg["wd_site"]
    endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    limit = 20
    jobs: dict[str, Job] = {}
    total: int | None = None
    for offset in range(0, 6000, limit):      # safety cap
        r = http_post(endpoint, {"appliedFacets": {}, "limit": limit,
                                 "offset": offset, "searchText": ""})
        data = r.json()
        if total is None:
            total = int(data.get("total") or 0)
        page = _parse_workday_jobs(data, cfg["name"], host, site)
        if not page:
            break                             # ran off the end of the board
        for j in page:
            jobs.setdefault(j.job_id, j)
        if total and offset + limit >= total:
            break
    return list(jobs.values())


# --------------------------------------------------------------------------- #
# Adapter: Amazon  (www.amazon.jobs/en/search.json — public, no auth)
#   Amazon is the largest new-grad SWE employer in the US and is on none of the
#   ATSs above, so it gets its own adapter.
# --------------------------------------------------------------------------- #
_AMAZON_SEARCH = "https://www.amazon.jobs/en/search.json"


def _parse_amazon(payload: dict, company: str) -> list[Job]:
    out: list[Job] = []
    for j in (payload or {}).get("jobs", []):
        jid = str(j.get("id_icims") or j.get("id") or "").strip()
        if not jid:
            continue
        path = (j.get("job_path") or "").strip()
        out.append(Job(company=company, job_id=jid,
                       title=(j.get("title") or "").strip(),
                       location=(j.get("normalized_location")
                                 or j.get("location") or "").strip(),
                       url=f"https://www.amazon.jobs{path}" if path
                           else "https://www.amazon.jobs"))
    return out


def fetch_amazon(cfg: dict) -> list[Job]:
    """Page amazon.jobs.

    `base_query` ANDs every word, so a narrow phrase returns almost nothing —
    "university graduate software" and "new grad" both come back with ZERO hits,
    while the single broad phrase below returns ~1,675. So: cast one wide net and
    let is_newgrad_swe do the filtering. `result_limit` above 100 returns an empty
    page, so 100 is the real cap.
    """
    query = cfg.get("query", "software development engineer")
    limit = 100
    jobs: dict[str, Job] = {}
    for offset in range(0, 4000, limit):
        r = http_get(_AMAZON_SEARCH, params={"base_query": query,
                                             "result_limit": limit, "offset": offset})
        data = r.json()
        page = _parse_amazon(data, cfg["name"])
        if not page:
            break
        for j in page:
            jobs.setdefault(j.job_id, j)
        if offset + limit >= int(data.get("hits") or 0):
            break
    return list(jobs.values())


# --------------------------------------------------------------------------- #
# Adapter: Netflix  (explore.jobs.netflix.net public jobs API)
# --------------------------------------------------------------------------- #
_NETFLIX_JOBS = "https://explore.jobs.netflix.net/api/apply/v2/jobs"


def _parse_netflix(payload: dict, company: str) -> list[Job]:
    out: list[Job] = []
    for p in (payload or {}).get("positions", []):
        jid = str(p.get("id") or "").strip()
        if not jid:
            continue
        out.append(Job(company=company, job_id=jid,
                       title=(p.get("name") or "").strip(),
                       location=", ".join(x for x in (p.get("locations") or []) if x),
                       department=(p.get("department") or "").strip(),
                       url=(p.get("canonicalPositionUrl") or "").strip()))
    return out


def fetch_netflix(cfg: dict) -> list[Job]:
    """Page the Netflix board.

    The API hard-caps a page at 10 no matter what `num` says (25/50/100/200 all
    return 10), so a full board is ~50 round-trips. Stop on the first empty page.
    """
    limit = 10
    jobs: dict[str, Job] = {}
    for start in range(0, 2000, limit):
        r = http_get(_NETFLIX_JOBS, params={"domain": "netflix.com",
                                            "start": start, "num": limit})
        data = r.json()
        page = _parse_netflix(data, cfg["name"])
        if not page:
            break
        for j in page:
            jobs.setdefault(j.job_id, j)
        if start + limit >= int(data.get("count") or 0):
            break
    return list(jobs.values())


ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
    "amazon": fetch_amazon,
    "netflix": fetch_netflix,
}


# --------------------------------------------------------------------------- #
# Location filter — only notify about roles in the United States
#   Same STRICT rule as tech_watcher (not job_watcher's looser one): an unplaceable
#   sibling location must not rescue a clearly-foreign posting.
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
    # Workday-only office cities the NVIDIA/Micron boards surfaced
    "santa clara", "hillsboro", "westford", "boise", "manassas", "folsom",
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
    "belgrade", "serbia", "zagreb", "croatia", "ljubljana", "slovenia", "sofia",
    "bucharest", "tallinn", "riga", "vilnius", "estonia", "latvia", "lithuania",
    "reykjavik", "iceland", "tbilisi", "minsk", "belarus", "cairo", "nairobi", "lagos",
    "yerevan", "hsinchu", "penang", "xian", "suzhou",
}
_FOREIGN_EXACT = {"uk", "uae", "emea", "apac", "can"}


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


def location_in_scope(location: str, title: str = "") -> bool:
    """True if the posting should be emailed under the US-only rule.

        any US location             -> send
        no US, but something foreign -> drop (an unplaceable sibling can't rescue it)
        nothing placeable at all     -> fall back to the title, else send

    The title fallback exists for Workday, which collapses a multi-office posting's
    location to "2 Locations" / "3 Locations" — unplaceable, so the rule above would
    send it. NVIDIA names the country in the title instead ("NVIDIA 2027 New College
    Graduate: Software Engineering - China"), so when the location tells us nothing,
    the title gets a vote.
    """
    if not US_ONLY:
        return True
    parts = _split_locations(location)
    if not parts:
        return True
    classes = [_classify_location(p) for p in parts]
    if "US" in classes:
        return True
    if "FOREIGN" in classes:
        return False
    # Nothing placeable in the location — let the title break the tie if it can.
    if title:
        tclasses = [_classify_location(p) for p in _split_locations(title)]
        if "US" not in tclasses and "FOREIGN" in tclasses:
            return False
    return True


# --------------------------------------------------------------------------- #
# Scrape
# --------------------------------------------------------------------------- #
def scrape_company(cfg: dict) -> tuple[list[Job], list[Job]]:
    """Scrape one company and return (raw, kept). Raises on adapter failure."""
    raw = ADAPTERS[cfg["adapter"]](cfg)
    strict = bool(cfg.get("strict_swe"))
    kept = [j for j in raw
            if is_newgrad_swe(j, strict_swe=strict)
            and location_in_scope(j.location, j.title)]
    return raw, kept


def collect_all() -> list[Job]:
    found: list[Job] = []
    for cfg in COMPANIES:
        try:
            raw, kept = scrape_company(cfg)
        except Exception as e:  # noqa: BLE001
            log.error("%s: adapter error: %s", cfg["name"], e)
            continue
        log.info("%-20s scraped %4d roles, %3d match new-grad SWE",
                 cfg["name"], len(raw), len(kept))
        found.extend(kept)
    return found


# --------------------------------------------------------------------------- #
# Storage (SQLite) — its own file AND its own table, separate from both siblings
# --------------------------------------------------------------------------- #
def db_connect() -> sqlite3.Connection:
    # A background launchd `--once` and a manual run can overlap, so two processes
    # may touch this file at once. WAL + a busy timeout let them share it instead
    # of failing with "database is locked".
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute(
        """CREATE TABLE IF NOT EXISTS seen_newgrad (
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
    return {row[0] for row in con.execute("SELECT key FROM seen_newgrad")}


def save_jobs(con: sqlite3.Connection, jobs: list[Job]) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con.executemany(
        "INSERT OR IGNORE INTO seen_newgrad VALUES (?,?,?,?,?,?,?,?)",
        [(j.key, j.company, j.job_id, j.title, j.location, j.department, j.url, now)
         for j in jobs],
    )
    con.commit()


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_email(new_jobs: list[Job], intro: str = "New new-grad roles:") -> bool:
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
    subject = f"[New Grad] {len(new_jobs)} SWE role(s): {companies}"
    if len(subject) > 160:
        subject = f"[New Grad] {len(new_jobs)} SWE role(s) at {len(by_company)} companies"

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
        log.info("Emailed %d new-grad role(s) to %s", len(new_jobs), to)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Email send failed: %s", e)
        return False


# --------------------------------------------------------------------------- #
# Core cycle
# --------------------------------------------------------------------------- #
def run_once(quiet_seed: bool = False) -> None:
    """One full pass. Postings are stored only after the email actually sends, so a
    failed send is retried on the next pass rather than silently swallowed."""
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

        if not new_jobs:
            save_jobs(con, current)
            log.info("No new roles this pass (%d tracked).", len(current))
            return

        # Unlike its siblings, the FIRST run emails by default: the new-grad set is
        # small (tens, not hundreds) and time-sensitive, so a silent seed would just
        # hide every role that is open right now. --quiet-seed opts out.
        if is_first_run and quiet_seed:
            save_jobs(con, current)
            log.info("First run: seeded %d roles silently. Future postings will be emailed.",
                     len(new_jobs))
            return

        log.info("%d NEW role(s):", len(new_jobs))
        for j in new_jobs:
            log.info("   + [%s] %s — %s", j.company, j.title, j.url)

        if send_email(new_jobs, intro="New new-grad SWE roles:" if not is_first_run
                      else "New-grad SWE roles open right now:"):
            save_jobs(con, current)
        else:
            log.warning("Email failed — NOT saving, so these retry on the next pass.")
    finally:
        con.close()


def _source_fingerprint() -> tuple[int, int]:
    """(mtime, size) of this script — changes the moment a `git pull` rewrites it."""
    st = Path(__file__).resolve().stat()
    return (st.st_mtime_ns, st.st_size)


def _restart_if_source_changed(fingerprint: tuple[int, int]) -> tuple[int, int]:
    """Re-exec if this file changed on disk since the loop started.

    Only matters for `--interval`: Python binds COMPANIES once at process start, so a
    long-running loop would otherwise ignore every company added by a `git pull`,
    forever. The launchd path doesn't need this — each fire is a fresh process.
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
        return current

    log.info("%s changed on disk — restarting to pick up the new company list.", path.name)
    for h in log.handlers:
        h.flush()
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, str(path), *sys.argv[1:]])  # never returns


def run_loop(interval_min: int, quiet_seed: bool) -> None:
    fingerprint = _source_fingerprint()
    log.info("Watching %d companies for new-grad SWE roles every %d min. Ctrl+C to stop.",
             len(COMPANIES), interval_min)
    while True:
        try:
            run_once(quiet_seed=quiet_seed)
        except Exception as e:  # noqa: BLE001
            log.error("Cycle failed: %s", e)
        try:
            time.sleep(interval_min * 60)
        except KeyboardInterrupt:
            log.info("Stopped.")
            return
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
    """Scrape ONE company now, email any NEW new-grad roles, then store them.
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
    log.info("%-20s scraped %4d roles, %3d new-grad SWE", cfg["name"], len(raw), len(kept))

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
            return 0
        log.warning("%s: email failed — NOT saving, so they'll retry next run.", cfg["name"])
        return 1
    finally:
        con.close()


def email_db() -> int:
    """Email every posting currently in the dedup store, scraping nothing.

    The store is left unchanged, so this does not affect which roles count as "new"
    on the next pass; you can safely run it more than once.
    """
    con = db_connect()
    try:
        rows = con.execute(
            "SELECT company, job_id, title, location, department, url FROM seen_newgrad "
            "ORDER BY company, title"
        ).fetchall()
    finally:
        con.close()

    if not rows:
        log.info("Store is empty — nothing to email. Run --once first to populate it.")
        return 0

    jobs = [Job(company=r[0], job_id=r[1], title=r[2], location=r[3] or "",
                department=r[4] or "", url=r[5] or "") for r in rows]
    log.info("Emailing %d tracked new-grad role(s)...", len(jobs))
    ok = send_email(jobs, intro="All new-grad roles currently in the database:")
    return 0 if ok else 1


def preview(query: str | None = None) -> int:
    """Scrape and PRINT the matching new-grad roles. Sends no email and never touches
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
    print(f"\n{total} new-grad SWE role(s) across {len(cfgs)} company(ies).")
    return 0


# --------------------------------------------------------------------------- #
# Self-test (no network) — real titles taken from the live boards
# --------------------------------------------------------------------------- #
def selftest() -> int:
    ok = True
    J = lambda t, **kw: Job("X", "1", t, **kw)  # noqa: E731

    # --- keep: explicitly labelled new-grad roles ---------------------------- #
    keep_labelled = [
        "Software Engineer, New Grad",                       # Palantir (real)
        "Software Engineer, New Grad - Infrastructure",      # Palantir (real)
        "Forward Deployed Software Engineer, New Grad - Commercial",  # Palantir (real)
        "Software Engineer - New Grad 2026",                 # Cerebras (real)
        "Kernel Engineer - New Grad",                        # Cerebras (real)
        "DevOps Engineer - New Grad 2026",                   # Cerebras (real)
        "Software Engineer, Early Career",                   # Notion (real)
        "Software Engineer, New Grad (Dec 2026)",            # Notion (real)
        "[2027] Software Engineer, Early Career",            # Roblox (real)
        "Software Engineer, New Grad (2027 Start)",          # Zip (real)
        "Software Engineer I (New Grad)",                    # Samsara (real)
        "Software Engineer, AI Platform - New Grad",         # Nuro (real)
        "Software Engineer, University Graduate",
        "Software Engineer, Campus",
        "Graduate Software Engineer",
        "Entry Level Software Developer",
        # The NVIDIA/Micron phrasing — "New College Grad" does NOT contain "new grad",
        # so it needs its own alternative. Missing this misses every NVIDIA role.
        "Systems Software Engineer - New College Grad 2026",  # NVIDIA (real)
        "System Software Engineer, SOC - New College Graduate",  # NVIDIA (real)
        "Compiler Engineer, Backend- New College Grad 2026",  # NVIDIA (real)
        "New College Grad - Software Engineer",              # Micron (real)
    ]
    bad_labelled = [t for t in keep_labelled if not is_newgrad_swe(J(t))]
    ok &= _check(f"keeps labelled new-grad SWE (offenders: {bad_labelled})", not bad_labelled)

    # --- keep: unlevelled entry titles --------------------------------------- #
    keep_entry = [
        "Software Engineer I",                               # Twitch/Crusoe (real)
        "Software Engineer I, Commerce Engineering",          # Twitch (real)
        "Software Engineer I, Network",                       # Crusoe (real)
        "Associate Software Engineer, Expenses",             # Justworks (real)
        "Engineer 1, Platform",
    ]
    bad_entry = [t for t in keep_entry if not is_newgrad_swe(J(t))]
    ok &= _check(f"keeps unlevelled entry titles (offenders: {bad_entry})", not bad_entry)

    # --- drop: the roman-numeral trap ---------------------------------------- #
    #     "Software Engineer II" must NOT be read as "Software Engineer I".
    drop_levels = [
        "Software Engineer II",
        "Software Engineer III, Payments",
        "Platform Software Engineer II",                     # Braze (real)
        "Product Security Engineer II",                      # Affirm (real)
        "Software Engineer IV",
    ]
    bad_levels = [t for t in drop_levels if is_newgrad_swe(J(t))]
    ok &= _check(f"drops Engineer II/III/IV — the roman-numeral trap "
                 f"(offenders: {bad_levels})", not bad_levels)

    # --- drop: mid/senior and unlevelled roles with no entry signal ----------- #
    drop_senior = [
        "Software Engineer",                                 # unlevelled: NOT new-grad
        "Senior Software Engineer",
        "Staff Software Engineer, AI Platform",
        "Principal Engineer",
        "Lead Software Engineer",
        "Engineering Manager",
        "Head of Engineering",
        "Software Architect",
        "Member of Technical Staff (Software Engineer, Monetization)",
    ]
    bad_senior = [t for t in drop_senior if is_newgrad_swe(J(t))]
    ok &= _check(f"drops senior/unlevelled without an entry signal "
                 f"(offenders: {bad_senior})", not bad_senior)

    # --- drop: internships (we want full-time new-grad) ---------------------- #
    drop_intern = [
        "Software Engineer Intern",
        "Software Engineering Internship - Summer 2027",
        "Software Engineer Co-op",
        "New Grad Software Engineer Intern",                 # intern beats new-grad
        "Engineering Apprentice",
    ]
    bad_intern = [t for t in drop_intern if is_newgrad_swe(J(t))]
    ok &= _check(f"drops internships (offenders: {bad_intern})", not bad_intern)

    # ...but "Internal" is not "intern" — the inherited word-boundary trap.
    ok &= _check("keeps 'Internal Tools Engineer I' ('intern' substring trap)",
                 is_newgrad_swe(J("Internal Tools Engineer I")) is True)

    # --- drop: hardware / silicon / QA new-grad roles ------------------------ #
    #     All real NVIDIA New College Grad titles. Without HARDWARE_RE these are
    #     ~3 of every 4 NVIDIA matches.
    drop_hw = [
        "ASIC Design Engineer - New College Grad 2026",
        "ASIC Verification Engineer - New College Grad 2026",
        "Circuit Design Engineer - New College Grad 2026",
        "Formal Verification Engineer - New College Grad 2026",
        "GPU Verification Engineer - New College Grad 2026",
        "DFT Engineer - New College Grad",
        "GPU Power Architect - New College Grad 2026",
        "SoC ASIC Verification Engineer - New College Grad 2026",
        "Software Quality Assurance Engineer - 2026 New College Grad",
        "Automation Engineer I",                             # Flexport (real)
        "Weld Engineer (All Levels)",                        # Saronic (real)
        # Reversed word order — "Engineer, <domain>" rather than "<domain> Engineer".
        # Both were real false positives in the first live preview.
        "Engineer I, Electrical Integration & Test (R5136)",  # Shield AI (real)
        "Associate Engineer, Manufacturing",                 # Shield AI (real)
    ]
    bad_hw = [t for t in drop_hw if is_newgrad_swe(J(t))]
    ok &= _check(f"drops hardware/silicon/QA new-grad roles (offenders: {bad_hw})", not bad_hw)

    # ...but an explicit "Software Engineer" outranks the domain keyword, or NVIDIA's
    # "System Software Engineer, SOC" is lost to \bsoc\b.
    ok &= _check("'Software Engineer' outranks a hardware keyword (SOC)",
                 is_newgrad_swe(J("System Software Engineer, SOC - New College Graduate"))
                 is True)
    ok &= _check("...but the override does NOT rescue 'Software Quality Assurance'",
                 is_newgrad_swe(J("Software Quality Assurance Engineer - 2026 New College Grad"))
                 is False)

    # --- strict_swe: hardware-heavy boards (NVIDIA, Micron) ------------------- #
    #     All real Micron "New College Grad" titles. Under the DEFAULT gate a bare
    #     "Engineer" passes, so 22 of Micron's 23 hits were silicon roles; under
    #     strict_swe the title must name an actual software discipline.
    micron_drop = [
        "New College Grad - DRAM Design Engineer",
        "New College Grad - Device Engineer, DRAM",
        "New College Grad - EDA/CAD Engineer",
        "New College Grad - Equipment Engineer (RDA & Metrology)",
        "New College Grad - Memory Design Engineer, HBM",
        "New College Grad - Process Integration Engineer",
        "New College Grad - Product Yield Enhancement Engineer, HBM",
        "New College Grad - Wet Etch/CMP/Bond Shift Engineer",
        "New College Grad - ENGINEER, SIG ELECTRICAL DESIGN",
        "New College Grad - AI Innovation Research Engineer",
        "New College Grad - OI System Engineer",
    ]
    bad_micron = [t for t in micron_drop if is_newgrad_swe(J(t), strict_swe=True)]
    ok &= _check(f"strict_swe drops silicon 'Engineer' roles (offenders: {bad_micron})",
                 not bad_micron)
    micron_keep = [
        "New College Grad - Software Engineer",              # Micron (real)
        "New College Grad - Embedded Firmware Engineer",     # Micron (real)
        "Systems Software Engineer - New College Grad 2026",  # NVIDIA (real)
        "Compiler Engineer, Backend- New College Grad 2026",  # NVIDIA (real)
    ]
    bad_mkeep = [t for t in micron_keep if not is_newgrad_swe(J(t), strict_swe=True)]
    ok &= _check(f"strict_swe keeps real software roles (offenders: {bad_mkeep})",
                 not bad_mkeep)
    # ...and the same strictness must NOT be applied by default, or ordinary tech
    # titles like "Kernel Engineer - New Grad" would be fine but "Design Engineer"
    # at Replit would wrongly vanish.
    ok &= _check("default gate still accepts a bare 'Engineer' title",
                 is_newgrad_swe(J("Design Engineer I")) is True)

    # --- drop: not software at all ------------------------------------------- #
    drop_nonswe = [
        "Sales Engineer, New Grad",
        "Solutions Engineer - University Graduate",
        "Customer Support Engineer I",
        "Technical Recruiter, Campus",
        "Product Manager, New Grad",
        "Research Scientist - New College Grad 2025",        # NVIDIA (real)
        "New College Grad - IT Software Support Engineer",   # Micron (real)
        "Compensation Partner (Engineering)",
    ]
    bad_nonswe = [t for t in drop_nonswe if is_newgrad_swe(J(t))]
    ok &= _check(f"drops non-software roles (offenders: {bad_nonswe})", not bad_nonswe)

    # --- ATS employmentType beats the title ---------------------------------- #
    ok &= _check("drops ATS-flagged intern even if the title says new grad",
                 is_newgrad_swe(J("Software Engineer, New Grad",
                                  employment_type="Intern")) is False)

    # --- Greenhouse parse (flat /jobs) --------------------------------------- #
    gh = {"jobs": [
        {"id": 1, "title": "Software Engineer, New Grad", "location": {"name": "New York, NY"},
         "absolute_url": "https://boards.greenhouse.io/x/jobs/1"},
        {"id": 2, "title": "Senior Software Engineer", "location": {"name": "Seattle"},
         "absolute_url": "https://boards.greenhouse.io/x/jobs/2"},
    ]}
    g = _parse_greenhouse(gh, "Nuro")
    ok &= _check("greenhouse parses 2 postings with URLs",
                 len(g) == 2 and g[0].url.endswith("/jobs/1"))
    ok &= _check("greenhouse keeps the new-grad role, drops the senior one",
                 [j.title for j in g if is_newgrad_swe(j)] == ["Software Engineer, New Grad"])

    # --- Lever parse (real record shape) ------------------------------------- #
    lv = [{"id": "abc-123", "text": "Software Engineer, New Grad - Infrastructure",
           "hostedUrl": "https://jobs.lever.co/palantir/abc-123",
           "categories": {"commitment": "Full-time", "team": "Infrastructure",
                          "location": "Palo Alto, CA", "allLocations": ["Palo Alto, CA"]}},
          {"id": "def-456", "text": "Software Engineer, Intern",
           "hostedUrl": "https://jobs.lever.co/palantir/def-456",
           "categories": {"commitment": "Intern", "team": "Eng", "location": "NYC"}}]
    lj = _parse_lever(lv, "Palantir")
    ok &= _check("lever parses title/url/location/team", len(lj) == 2
                 and lj[0].url == "https://jobs.lever.co/palantir/abc-123"
                 and lj[0].location == "Palo Alto, CA"
                 and lj[0].department == "Infrastructure")
    ok &= _check("lever keeps the new-grad role, drops the intern",
                 [j.title for j in lj if is_newgrad_swe(j)]
                 == ["Software Engineer, New Grad - Infrastructure"])

    # --- Ashby parse (real record shape) ------------------------------------- #
    ab = {"jobs": [
        {"id": "u-1", "title": "Software Engineer, New Grad (2027 Start)",
         "location": "San Francisco", "department": "Eng", "employmentType": "FullTime",
         "jobUrl": "https://jobs.ashbyhq.com/zip/u-1", "isListed": True},
        {"id": "u-2", "title": "Staff Software Engineer, AI Platform", "location": "New York",
         "department": "Eng", "employmentType": "FullTime",
         "jobUrl": "https://jobs.ashbyhq.com/zip/u-2", "isListed": True},
        {"id": "u-3", "title": "Software Engineer, New Grad", "location": "SF",
         "employmentType": "FullTime",
         "jobUrl": "https://jobs.ashbyhq.com/zip/u-3", "isListed": False},
    ]}
    aj = _parse_ashby(ab, "Zip")
    ok &= _check("ashby skips unlisted postings (2 of 3)", len(aj) == 2)
    ok &= _check("ashby keeps the new-grad role, drops Staff",
                 [j.title for j in aj if is_newgrad_swe(j)]
                 == ["Software Engineer, New Grad (2027 Start)"])

    # --- Workday parse (real CXS record shape) ------------------------------- #
    wd = {"total": 2, "jobPostings": [
        {"title": "Systems Software Engineer - New College Grad 2026",
         "locationsText": "US, CA, Santa Clara",
         "externalPath": "/job/Santa-Clara/SWE_JR100", "bulletFields": ["JR100"]},
        {"title": "ASIC Design Engineer - New College Grad 2026",
         "locationsText": "US, CA, Santa Clara",
         "externalPath": "/job/Santa-Clara/ASIC_JR200", "bulletFields": ["JR200"]},
    ]}
    wj = _parse_workday_jobs(wd, "NVIDIA", "nvidia.wd5.myworkdayjobs.com",
                             "NVIDIAExternalCareerSite")
    ok &= _check("workday parses id/title/location and builds an absolute URL",
                 len(wj) == 2 and wj[0].job_id == "JR100"
                 and wj[0].url == ("https://nvidia.wd5.myworkdayjobs.com/en-US/"
                                   "NVIDIAExternalCareerSite/job/Santa-Clara/SWE_JR100"))
    ok &= _check("workday keeps the software NCG role, drops the ASIC one",
                 [j.title for j in wj if is_newgrad_swe(j)]
                 == ["Systems Software Engineer - New College Grad 2026"])

    # --- Workday pagination: `total` is only trustworthy on the FIRST page ---- #
    #     NVIDIA/Salesforce/eBay report a real total on page 1 and "total": 0 after,
    #     which made the old loop stop at offset 20 and read 40 of ~2000 postings.
    class _FakeResp:
        def __init__(self, payload): self._p = payload
        def json(self): return self._p

    def _fake_workday(_url, body, *a, **kw):
        off, lim, tot = body["offset"], body["limit"], 95
        page = [{"title": f"Software Engineer {i} - New Grad",
                 "locationsText": "US, CA, Santa Clara",
                 "externalPath": f"/job/x/R{i}", "bulletFields": [f"R{i}"]}
                for i in range(off, min(off + lim, tot))]
        # the quirk: a real total on page 1, then 0 forever after
        return _FakeResp({"total": tot if off == 0 else 0, "jobPostings": page})

    _real_post = globals()["http_post"]
    globals()["http_post"] = _fake_workday
    try:
        paged = fetch_workday({"name": "Fake", "wd_host": "h", "wd_tenant": "t", "wd_site": "s"})
    finally:
        globals()["http_post"] = _real_post
    ok &= _check(f"workday pages past a page-2 'total: 0' (got {len(paged)} of 95)",
                 len(paged) == 95)

    # --- Amazon parse (real search.json record shape) ------------------------ #
    az = {"hits": 2, "jobs": [
        {"id_icims": "2938471", "title": "Software Development Engineer, Amazon Leo, Early Career - 2026",
         "normalized_location": "Redmond, Washington, USA",
         "job_path": "/en/jobs/2938471/sde-leo"},
        {"id_icims": "2938472", "title": "Senior Software Development Engineer",
         "normalized_location": "Seattle, Washington, USA",
         "job_path": "/en/jobs/2938472/sr-sde"},
    ]}
    aj2 = _parse_amazon(az, "Amazon")
    ok &= _check("amazon parses id/title/location and builds an absolute URL",
                 len(aj2) == 2 and aj2[0].job_id == "2938471"
                 and aj2[0].url == "https://www.amazon.jobs/en/jobs/2938471/sde-leo")
    ok &= _check("amazon keeps the early-career role, drops the senior one",
                 [j.title for j in aj2 if is_newgrad_swe(j)]
                 == ["Software Development Engineer, Amazon Leo, Early Career - 2026"])

    # --- Netflix parse (real API record shape) -------------------------------- #
    nf = {"count": 2, "positions": [
        {"id": 790299, "name": "Software Engineer I, Playback",
         "locations": ["Los Gatos, California"], "department": "Streaming",
         "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/790299"},
        {"id": 790300, "name": "Full Stack Software Engineer 5 - Studio",
         "locations": ["Los Angeles, California"], "department": "Studio",
         "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/790300"},
    ]}
    nj2 = _parse_netflix(nf, "Netflix")
    ok &= _check("netflix parses id/title/location/url",
                 len(nj2) == 2 and nj2[0].job_id == "790299"
                 and nj2[0].location == "Los Gatos, California"
                 and nj2[0].url.endswith("/790299"))
    ok &= _check("netflix keeps 'Engineer I', drops the level-5 role",
                 [j.title for j in nj2 if is_newgrad_swe(j)]
                 == ["Software Engineer I, Playback"])

    # --- US location filter --------------------------------------------------- #
    us_send = ["", "New York, NY", "San Francisco", "Remote - US", "Seattle, WA",
               "United States", "Austin, TX", "London, New York",
               "N/A", "2 Locations",                          # unplaceable -> send
               "US, CA, Santa Clara", "US, OR, Hillsboro",    # NVIDIA (real)
               "Mountain View, CA USA;  San Francisco"]       # Waymo (real)
    us_drop = ["London, United Kingdom",                      # Palantir (real)
               "Seoul, South Korea",                          # Palantir (real)
               "Toronto, CAN",                                # Cerebras (real)
               "London - UK2",                                # Samsara (real)
               "Toronto, Canada", "Remote Poland", "Beijing, China",
               "Bengaluru, India", "Singapore", "EMEA",
               "Belgrade, London, Berlin"]
    bad_send = [x for x in us_send if not location_in_scope(x)]
    bad_drop = [x for x in us_drop if location_in_scope(x)]
    ok &= _check(f"US filter sends US/no-loc/unplaceable (offenders: {bad_send})", not bad_send)
    ok &= _check(f"US filter drops foreign, even w/ unplaceable sibling "
                 f"(offenders: {bad_drop})", not bad_drop)

    # Workday collapses multi-office postings to "N Locations" — unplaceable, so the
    # title has to break the tie. Both titles below are real NVIDIA postings.
    ok &= _check("unplaceable location + foreign title -> drop",
                 location_in_scope(
                     "3 Locations",
                     "NVIDIA 2027 New College Graduate: Software Engineering - China")
                 is False)
    ok &= _check("unplaceable location + neutral title -> send",
                 location_in_scope(
                     "2 Locations",
                     "Systems Software Engineer - New College Grad 2026") is True)
    ok &= _check("a US location still wins over a foreign-sounding title",
                 location_in_scope("Santa Clara, CA",
                                   "Software Engineer, India Market") is True)

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
        description="Watch tech-company boards for new NEW-GRAD software roles.")
    ap.add_argument("--once", action="store_true", help="Run one pass then exit (for launchd/cron).")
    ap.add_argument("--interval", type=int, default=int(os.getenv("CHECK_EVERY_MINUTES", "120")),
                    help="Minutes between checks when looping (default 120).")
    ap.add_argument("--list", action="store_true", help="Print monitored companies and exit.")
    ap.add_argument("--company", metavar="NAME",
                    help="Scrape ONE firm now (e.g. --company NVIDIA), email new roles, exit.")
    ap.add_argument("--preview", action="store_true",
                    help="Print matching roles WITHOUT emailing or touching the store. "
                         "Combine with --company to preview a single firm.")
    ap.add_argument("--email-db", action="store_true",
                    help="Email every role already in the store (no scraping), then exit.")
    ap.add_argument("--quiet-seed", action="store_true",
                    help="Seed the store silently on the first run instead of emailing it.")
    ap.add_argument("--selftest", action="store_true", help="Run offline tests and exit.")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.preview:
        return preview(args.company)
    if args.email_db:
        return email_db()
    if args.list:
        by_ats: dict[str, list[str]] = {}
        for c in COMPANIES:
            by_ats.setdefault(c["adapter"], []).append(c["name"])
        print(f"Monitoring {len(COMPANIES)} companies for new-grad SWE roles:")
        for ats in sorted(by_ats):
            print(f"\n  {ats} ({len(by_ats[ats])}):")
            for name in sorted(by_ats[ats]):
                print(f"    • {name}")
        return 0
    if args.company:
        return run_company(args.company)

    if args.once:
        run_once(quiet_seed=args.quiet_seed)
    else:
        run_loop(args.interval, quiet_seed=args.quiet_seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
