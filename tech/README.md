# Tech Watcher — mid-level SWE roles

Checks **119 tech companies** and emails you when a **new mid-level software engineering
role** is posted. Its own dedup store (`tech/seen_tech_jobs.sqlite3`, table `seen_tech`),
completely separate from the quant watcher — wiping one never affects the other.

Every company is reached through a **public ATS JSON API** — no scraping, no browser.
Across all 119 boards that's ~15,800 postings, of which ~1,450 are mid-level US SWE roles.

**Greenhouse (70)** · Stripe, Databricks, Anthropic, Airbnb, Coinbase, Cloudflare, MongoDB,
Reddit, Pinterest, Figma, Discord, Robinhood, Datadog, Okta, Roblox, Waymo, CoreWeave, xAI,
Block, Verkada, Grafana Labs, Figure AI, Nuro, Fivetran, Postman, Klaviyo, Together AI,
Tailscale, Braze, Chainguard, Abnormal Security, Twitch, Peloton, Faire, Mercury, Gemini,
Cockroach Labs, PlanetScale, Marqeta, Starburst, Affirm, Brex, Chime, Samsara, Scale AI,
Twilio, Asana, GitLab, Lyft, Instacart, Elastic, Vercel, Dropbox, Gusto, Duolingo, Flexport,
Amplitude, Webflow, Carta, Airtable, SoFi, Squarespace, Justworks, Attentive, project44,
Betterment, Komodo Health, Iterable, Calendly, StockX

**Lever (6)** · Palantir, Shield AI, Zoox, Outreach, Wealthfront, Ro

**Ashby (43)** · OpenAI, Snowflake, Notion, Ramp, Plaid, Cursor, ElevenLabs, Sierra, Sentry,
Modal, Confluent, Cerebras, Crusoe, Saronic, Physical Intelligence, Decagon, Writer, Docker,
1Password, Temporal, Benchling, Render, Railway, Airbyte, Neon, Zapier, Miro, Strava,
Poshmark, Modern Treasury, Dave, Quora, Perplexity, Harvey, ClickHouse, Cohere, Replit,
Vanta, Supabase, Linear, Zip, Watershed, Runway

The authoritative list is `COMPANIES` at the top of [tech_watcher.py](tech_watcher.py).

> Setup (virtualenv + `run_tech.sh` with your Gmail app password) is in the
> [root README](../README.md). Run every command **from the repo root**.

## Commands

```bash
./run_tech.sh --list                      # the 119 companies, grouped by ATS
./run_tech.sh --preview                   # print every matching role — NO email, NO DB write
./run_tech.sh --preview --company Stripe  # sanity-check one firm's filter output
./run_tech.sh --once                      # single pass (first pass seeds silently)
./run_tech.sh --once --notify-seed        # ...or email that whole first batch
./run_tech.sh --interval 180              # loop, checking every 180 min
./run_tech.sh --company Stripe            # scrape ONE firm now, email anything new
./run_tech.sh --email-db                  # email everything already in the store (no scraping)
./run_tech.sh --selftest                  # offline filter/parser tests
```

**Start with `--preview`** — it shows exactly what would be emailed, without sending
anything or seeding the store.

⚠️ The **first** `--once` **seeds silently** so you aren't flooded with ~570 existing
roles. Use `--once --notify-seed` if you want that first batch in your inbox.
`--email-db` does the same for a store that's *already* populated, and leaves it untouched
(safe to re-run).

## Run it in the background (no terminal open)

```bash
./tech/install_agent.sh install                     # every 3 hours
./tech/install_agent.sh install --interval-hours 6
./tech/install_agent.sh status                      # loaded? last exit code? recent log
./tech/install_agent.sh run-now                     # trigger a pass immediately
./tech/install_agent.sh uninstall
```

This installs a macOS **LaunchAgent** (`~/Library/LaunchAgents/com.kavin.techwatcher.plist`).
Unlike `nohup ... &`, it survives closing the terminal, logging out, **and rebooting**, and a
run missed while the laptop slept fires once on wake. Output goes to `launchd.tech.out.log`
at the repo root.

It runs `--once` on a timer rather than holding an `--interval` loop open. Each fire is a
fresh process, so a `git pull` that adds companies is picked up automatically on the next
run — no restart needed.

**If you have no store yet, `install` seeds it first, in the foreground.** That pass sweeps
every board and takes a few minutes, and emails nothing. Doing it up front means the first
launchd fire is a normal incremental pass — otherwise the agent looks broken for hours while
it silently seeds. Skip it with `--no-seed` if you'd rather install instantly.

`install` **refuses to run** while `run_tech.sh` still holds the placeholder password, since
the agent would otherwise fail silently every few hours. It also refuses to install if the
repo sits in `~/Downloads`, `~/Documents` or `~/Desktop`, where macOS privacy protection
(TCC) blocks LaunchAgents from executing files — that combination installs cleanly and then
fails every single run with exit code 126, writing nothing to the log.

### Checking on it

```bash
./tech/install_agent.sh status          # the one command to remember
launchctl list | grep techwatcher       # PID | last exit code | label
tail -f launchd.tech.out.log            # watch a pass live
```

`state = not running` is normal, not an error: this is a periodic agent, not a daemon. It
wakes, sweeps all 119 boards, and exits, so it is only a live process for a few minutes out
of every few hours. The fields that tell you it's healthy are **`last exit code = 0`** and
the recent log lines.

## What counts as "mid-level"

Deliberately **broad**: any software-engineering title that is **neither** senior-tier
(senior / staff / principal / lead / manager / director / …) **nor** entry-level
(intern / new-grad / junior / …). An unlevelled `Software Engineer` counts, as does
`Software Engineer II/III`. **US-only** by default.

### Traps this handles (don't regress these)

Naive substring matching gets all of these wrong — they're locked in by `--selftest`:

| Title | Verdict | Why it's tricky |
|---|---|---|
| `Member of Technical Staff (Software Engineer)` | **keep** | contains **"staff"**, but MTS is a mid-level IC title (Perplexity, Anthropic) |
| `Internal Tools Engineer` | **keep** | contains **"intern"** as a substring |
| `Software Engineer II, Backend (Merchant & Partner Lifecycle)` | **keep** | contains **"partner"**, but is a real SWE role |
| `Compensation Partner (Engineering)` | **drop** | an HR role that merely names Engineering as the org it supports |
| `Staff Software Engineer` | **drop** | genuinely senior-tier |

### US location rule

Stricter than the quant watcher's. That one sends whenever *any* location part is
unrecognised, which let clearly-foreign postings through as soon as they listed an
unplaceable sibling (`Toronto, CAN-Remote`; `Belgrade, London, Berlin`). Here:

- any **US** location → send
- no US, but something **foreign** → **drop** (an unplaceable sibling can't rescue it)
- nothing placeable at all (`N/A`, `Remote`) → send

## Tuning

All at the top of [tech_watcher.py](tech_watcher.py):
- `SWE_RE` — what looks like a software role
- `NON_SWE_RE` — "engineer" roles that aren't software (sales/solutions/hardware/HR partner)
- `SENIOR_RE` / `ENTRY_RE` — what's excluded above and below mid-level
- `US_ONLY` — set `False` to get roles anywhere
- `COMPANIES` — add a firm; find its token in its careers URL:
  `boards.greenhouse.io/<token>` · `jobs.lever.co/<token>` · `jobs.ashbyhq.com/<token>`

> **Verify a token before adding it.** These 404 (they're *not* on Greenhouse): `ramp`,
> `doordash`, `datadoghq`, `plaid`, `notion`, `openai`, `hashicorp`, `confluent`,
> `grammarly`, `sentry`, `retool`. A wrong token fails silently — the firm just never
> returns jobs.

## The store

```bash
sqlite3 tech/seen_tech_jobs.sqlite3 "SELECT COUNT(*) FROM seen_tech;"
sqlite3 tech/seen_tech_jobs.sqlite3 "SELECT company, COUNT(*) FROM seen_tech GROUP BY company ORDER BY 2 DESC;"
rm -f tech/seen_tech_jobs.sqlite3     # wipe & start fresh (next --once re-seeds)
```

## Sharing this watcher

`../dist/build_zip.sh` packages this script into a zip someone else can set up and run.
See [../dist/README.md](../dist/README.md).
