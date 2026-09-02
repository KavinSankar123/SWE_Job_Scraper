# New-Grad Watcher — new-graduate SWE roles at tech companies

Checks **301 tech companies** and emails you when a **new graduate software engineering
role** is posted. Its own dedup store (`newgrad/seen_newgrad_jobs.sqlite3`, table
`seen_newgrad`), completely separate from the quant and tech watchers — wiping one never
affects the others.

This is the deliberate **inverse** of [../tech/](../tech/): that watcher throws new-grad
titles away, this one keeps only those.

Every company is reached through a **public JSON API** — no scraping, no browser. Across all
301 boards that's ~34,000 postings, of which ~66 are new-grad US SWE roles. A full pass
takes about 5 minutes.

**Greenhouse (164)** · Stripe, Databricks, Anthropic, Airbnb, Coinbase, Cloudflare, MongoDB,
Reddit, Pinterest, Figma, Discord, Robinhood, Datadog, Okta, Roblox, Waymo, CoreWeave, xAI,
Block, Nuro, Samsara, Twitch, Justworks, Affirm, Twilio, GitLab, Lyft, Instacart, Dropbox,
Duolingo, Flexport, Carta, SoFi, SpaceX, Toast, Epic Games, Riot Games, Adyen, Intercom,
Ripple, Remote, Upstart, Smartsheet, OpenTable, Mixpanel, New Relic, Fastly, Fireblocks,
LaunchDarkly, Zocdoc, Checkr, Salesloft, Coursera, Khan Academy, Nextdoor, Lucid Motors,
Oscar Health, Via, Cribl, Everlaw, Tenstorrent, Graphcore, PsiQuantum, IonQ, Wing, …

**Lever (22)** · Palantir, Spotify, Shield AI, Zoox, Gopuff, Anchorage Digital, AngelList,
Outreach, Wealthfront, Ro, Secureframe, LogRocket, Olo, 15Five, Hermeus, Waabi, Lyra Health,
Pipedrive, Sysdig, Veo, Payactiv, Rigetti

**Ashby (110)** · OpenAI, Notion, Ramp, Plaid, Cursor, Cerebras, Crusoe, Zip, Quora,
Perplexity, Snowflake, Linear, Supabase, Replit, Deepgram, Baseten, ClickUp, Suno, Drata,
Thumbtack, Hex, Alchemy, Anyscale, Redis, Midjourney, Poolside, PostHog, WHOOP, Skydio,
LangChain, LlamaIndex, Patreon, Substack, Socure, Headway, Abridge, Exa, Attio, Etched, …

**Workday (3)** · NVIDIA, Micron, Adobe

**Own career API (2)** · Amazon, Netflix — neither is on any ATS, and Amazon is the largest
new-grad SWE employer in the US, so each gets a dedicated adapter.

The authoritative list is `COMPANIES` at the top of [newgrad_watcher.py](newgrad_watcher.py).

### How much more coverage is worth adding?

Measured 2026-08-30, because the answer is counter-intuitive. Adding companies is trivial —
Greenhouse/Ashby/Lever host tens of thousands, and one probe run verified 88 new boards at
once. **But yield collapses on small companies.** Those 88 boards carried 3,689 postings and
produced **4** new-grad roles; meanwhile 6 large firms produce two-thirds of the total.

New-grad reqs concentrate almost entirely in big companies with formal university programs.
Startups under a few hundred people effectively never post them. So the useful question is
never "how many more companies" but "which big employers are still missing".

What's left, and why it's hard:

| Target | Status |
|---|---|
| Microsoft | `gcsservices.careers.microsoft.com` — SSL-blocked from this machine; may work elsewhere |
| Apple, Google, Meta, Tesla, TikTok, Bloomberg | blocked, or need a headless browser |
| SmartRecruiters | platform API works (one adapter → many firms) but carries little US big tech |
| More Workday tenants | work, but each site path has to be found by hand (see below) |

> **Why Workday matters.** The Greenhouse/Lever/Ashby universe misses the biggest new-grad
> employers entirely — they're on Workday. NVIDIA alone runs a ~2,000-posting board.
>
> ⚠️ **Workday pagination gotcha.** Several tenants (NVIDIA, Salesforce, eBay, PayPal)
> report a real `total` on the **first** page and then `"total": 0` on every page after it.
> Re-reading `total` each page makes `offset + limit >= total` true at offset 20, silently
> truncating the board to **40 postings** — NVIDIA was returning 40 of ~2,000, and so
> yielded zero new-grad roles. `fetch_workday` now trusts `total` from the first page only,
> and stops on an empty page as a backstop. Micron reports `total` consistently, which is
> exactly why the bug hid. (`quant/job_watcher.py` has the original version of this loop;
> its only Workday firm, Arrowstreet, has 4 postings, so it isn't affected today.)

> Setup (virtualenv + `run_newgrad.sh` with your Gmail app password) is in the
> [root README](../README.md). Run every command **from the repo root**.

## Commands

```bash
./run_newgrad.sh --list                     # the 301 companies, grouped by ATS
./run_newgrad.sh --preview                  # print every matching role — NO email, NO DB write
./run_newgrad.sh --preview --company NVIDIA # sanity-check one firm's filter output
./run_newgrad.sh --once                     # single pass
./run_newgrad.sh --once --quiet-seed        # ...seed the store silently instead
./run_newgrad.sh --interval 120             # loop, checking every 120 min
./run_newgrad.sh --company Palantir         # scrape ONE firm now, email anything new
./run_newgrad.sh --email-db                 # email everything already in the store
./run_newgrad.sh --selftest                 # offline filter/parser tests
```

**Start with `--preview`** — it shows exactly what would be emailed, without sending
anything or seeding the store.

### First run emails, unlike the other two watchers

Its siblings seed **silently** on the first run, because otherwise you'd get 500+ roles at
once. Here the whole matching set is a few dozen and it's time-sensitive, so the first
`--once` **emails what's open right now**. Use `--quiet-seed` if you'd rather it start quiet.

## Run it in the background (no terminal open)

```bash
./newgrad/install_agent.sh install                     # every 2 hours
./newgrad/install_agent.sh install --interval-hours 4
./newgrad/install_agent.sh status                      # loaded? last exit code? recent log
./newgrad/install_agent.sh run-now                     # trigger a pass immediately
./newgrad/install_agent.sh uninstall
```

This installs a macOS **LaunchAgent** (`~/Library/LaunchAgents/com.kavin.newgradwatcher.plist`).
Unlike `nohup ... &`, it survives closing the terminal, logging out, **and rebooting**, and a
run missed while the laptop slept fires once on wake. Output goes to
`launchd.newgrad.out.log` at the repo root.

It runs `--once` on a timer rather than holding an `--interval` loop open. Each fire is a
fresh process, so a `git pull` that adds companies is picked up automatically on the next
run — no restart needed.

`install` **refuses to run** while `run_newgrad.sh` still holds the placeholder password,
since the agent would otherwise fail silently every two hours.

## What counts as "new grad"

A title is kept when it looks like software engineering, is neither senior-tier nor an
internship, **and** carries an entry signal — either an explicit label or an unlevelled
entry title:

- **explicit** — `New Grad`, `New College Grad`, `University Graduate`, `Campus`,
  `Early Career`, `Entry Level`, `Class of 2027`, `2027 Start`, `Graduate Program`
- **unlevelled** — `Software Engineer I`, `Associate Software Engineer`

**US-only** by default, using the strict rule: any US → send; no US but something foreign →
drop; nothing placeable → fall back to the title, else send.

That title fallback is there for Workday, which collapses a multi-office posting's location
to `"3 Locations"`. That's unplaceable, so the rule above would send it — but NVIDIA names
the country in the title (`NVIDIA 2027 New College Graduate: Software Engineering - China`),
so when the location says nothing the title gets a vote.

### Traps this handles (don't regress these)

All locked in by `--selftest`, and every one of them was a real posting:

| Title | Verdict | Why it's tricky |
|---|---|---|
| `Systems Software Engineer - New College Grad 2026` | **keep** | "New College Grad" does **not** contain "new grad" — NVIDIA's phrasing needs its own pattern, or every NVIDIA role is silently missed |
| `Software Engineer II` | **drop** | must not match the `Engineer I` rule |
| `System Software Engineer, SOC - New College Graduate` | **keep** | contains the hardware keyword `SOC`, but an explicit "Software Engineer" outranks it |
| `Software Quality Assurance Engineer - 2026 New College Grad` | **drop** | contains "software", but it's QA — the override above deliberately doesn't rescue it |
| `New College Grad - DRAM Design Engineer` | **drop** | says "Engineer", but it's silicon (see `strict_swe`) |
| `Engineer I, Electrical Integration & Test` | **drop** | `Engineer, <domain>` word order — the reverse of what `NON_SWE_RE` catches |
| `Internal Tools Engineer I` | **keep** | contains "intern" as a substring |
| `Software Engineer` | **drop** | unlevelled with no entry signal — that's the *tech* watcher's job |

### `strict_swe` — the hardware-company switch

`SWE_RE` accepts a bare "engineer", which is right at Notion (nearly every Engineer writes
software) and catastrophic at a chip maker. Micron's board returned 23 New College Grad
"Engineer" roles of which exactly **one** was software — the rest were DRAM Design, Device,
Equipment, Process Integration, Wet Etch/CMP/Bond Shift.

Blocklisting each of those is unwinnable, so hardware-heavy firms get `"strict_swe": True`
in `COMPANIES`, which requires the title to name a real software discipline (software, SWE,
developer, compiler, kernel, firmware, SRE, backend, ML, …). Set it on any semiconductor,
aerospace, defense, automotive, robotics or wearable firm you add.

SpaceX is the clearest case: it posts 16 `New Graduate Engineer` roles, of which only 10 are
software — the rest are Propulsion, GNC, Civil/Structural and Launch & Test. The 14 firms
currently flagged are NVIDIA, Micron, SpaceX, Astranis, Lucid Motors, Helsing, Epirus, Ursa
Major, Kodiak Robotics, Hermeus, Waabi, Skydio, WHOOP and insitro.

## Tuning

All at the top of [newgrad_watcher.py](newgrad_watcher.py):
- `NEWGRAD_RE` / `ENTRY_LEVEL_RE` — what counts as an entry signal
- `SWE_RE` / `STRICT_SWE_RE` — what looks like software (default vs. `strict_swe` firms)
- `NON_SWE_RE` / `HARDWARE_RE` — "engineer" roles that aren't software
- `SOFTWARE_TITLE_RE` — the override that lets an explicit "Software Engineer" beat a
  hardware keyword
- `SENIOR_RE` / `INTERN_RE` — excluded above and below new-grad
- `US_ONLY` — set `False` to get roles anywhere

## Adding a company

```python
{"name": "Some Firm", "adapter": "greenhouse", "board_token": "<token>"},
{"name": "Chip Co",   "adapter": "workday", "strict_swe": True,
 "wd_host": "chipco.wd1.myworkdayjobs.com", "wd_tenant": "chipco", "wd_site": "External"},
```

> **Verify a token before adding it** — a wrong one fails silently and the firm simply
> never returns jobs. Greenhouse/Lever/Ashby tokens come out of the careers URL
> (`boards.greenhouse.io/<token>`, `jobs.lever.co/<token>`, `jobs.ashbyhq.com/<token>`).
>
> Workday is different: the tenant is usually guessable but the **site path is not**. An
> `HTTP 422` means the tenant is right and the site is wrong — read the real path out of
> the company's careers URL (`myworkdayjobs.com/<site>`). Every campus-specific site name
> tried during development (`..._Campus`, `..._University`, `Futureforce_Career_Site`) was
> a 404; dedicated campus boards exist but have to be found, not guessed.

## The store

```bash
sqlite3 newgrad/seen_newgrad_jobs.sqlite3 "SELECT COUNT(*) FROM seen_newgrad;"
sqlite3 newgrad/seen_newgrad_jobs.sqlite3 \
  "SELECT company, COUNT(*) FROM seen_newgrad GROUP BY company ORDER BY 2 DESC;"
rm -f newgrad/seen_newgrad_jobs.sqlite3     # wipe & start fresh
```

⚠️ Never use `--email-db` as a "is the store OK?" check — it really does send mail.

## Seasonality

New-grad hiring is seasonal. A live scan on 2026-08-30 found ~66 US roles across all 301
boards, with Palantir alone accounting for over half. That's the *start* of the 2027 cycle —
expect the count to climb sharply through September–November. A quiet week is normal and is
not evidence the watcher is broken; confirm with `--preview`.
