# Quant/HFT Watcher — new-grad SWE roles + recruiting events

Checks 29 quant/HFT career pages on a schedule and emails you when a **new** US-based
new-grad software role appears. It also surfaces **recruiting events** (info sessions,
networking nights, trading challenges) with a **direct registration link**.

Only alerts once per posting (SQLite dedup store at `quant/seen_jobs.sqlite3`).

> Setup (virtualenv + `run.sh` with your Gmail app password) is in the
> [root README](../README.md). Day-to-day commands are in **[COMMANDS.md](COMMANDS.md)**.
> Run every command **from the repo root**.

## Jobs

```bash
./run.sh --list          # show what will be monitored
./run.sh --once          # single pass (first pass just seeds, no email)
./run.sh --interval 120  # loop forever, checking every 120 minutes
./run.sh --company DRW   # scrape ONE firm now for SWE roles (see below)
./run.sh --email-db      # email everything already stored (no scraping)
```

The **first run seeds silently** so you don't get flooded. From then on you're emailed
only about newly-posted roles. Use `--notify-seed` to email the first batch too.

### Scrape a single company on demand (`--company`)

```bash
./run.sh --company DRW                  # name match ignores case/spaces/punctuation
./run.sh --company "Citadel Securities"
./run.sh --company deshaw
```

`--company` does one targeted pass over a single firm and intentionally differs from
the background watcher:
- it keeps **all software roles** (not just new-grad, but still US-only) — your "what
  SWE roles are open here right now?" button;
- it **emails only postings you haven't seen before**, and sends **nothing** when
  there's nothing new;
- postings are added to the dedup store **only after the email actually sends**, so a
  failed send is retried next time rather than silently lost.

## Recruiting events

Surfaces events — info sessions, networking nights, trading challenges, tech talks,
women-in-trading days — and emails you the **direct registration link** so you can sign
up fast. Unlike the job filters, there is **no** relevance/US/date filtering: if a
posting is classified as an event, you get it.

```bash
./run.sh --list-events          # see what's detected right now (no email sent)
./run.sh --events-once          # email register links for any NEW events
./run.sh --events-interval 120  # keep checking every 120 min
```

The first `--events-once` emails everything currently open (**no** silent seeding — the
event set is small and time-sensitive), then only new events after that. Events dedupe in
a separate `seen_events` table inside the same SQLite file, so the job watcher is
unaffected.

**Event sources:**
- **Greenhouse boards** — events detected by title on the 22 boards already watched.
  Low yield (events sit mixed into the job board with no dedicated events page).
- **Jane Street** — its server-rendered `programs-and-events` page; every entry (AMP,
  INSIGHT, WiSE, QTC, JSIP, Graduate Research Fellowship, …) is a real program/event.
- **JS-rendered firm pages** (opt-in) — Hudson River Trading is wired via the optional
  Playwright renderer; install Playwright to activate it, then tune its link pattern with
  `--list-events --debug`. Citadel / Two Sigma are candidates for the same path (Citadel
  WAF-blocks plain requests; Two Sigma has no public events listing yet).

## How each firm is reached

Most firms expose a clean data feed and work out of the box:
- **Greenhouse JSON API** — the majority: Radix, Hudson River Trading, Five Rings, Jump
  Trading, Flow Traders, Tower Research, Blackedge, Walleye (students board), Akuna,
  Point72, QRT/Qube, IMC, WorldQuant, Squarepoint, DV Trading, Schonfeld, AQR, Virtu,
  Old Mission, PDT Partners, Vatic Labs, Aquatic
- **Workday CXS JSON API** — Arrowstreet Capital (`Campus_Careers` site)
- **Custom JSON / embedded data** — D.E. Shaw (server HTML), DRW (Next.js `__NEXT_DATA__`),
  SIG (`careers.sig.com/api/jobs`, pre-filtered to New Graduates)
- **Avature HTML** — Two Sigma (server-rendered `JobDetail` links, paginated via `?jobOffset=`)
- **WordPress `admin-ajax.php`** — Citadel, Citadel Securities (the only JS-rendered ones)

> **Note:** the jobs adapter reads Greenhouse's `/departments` endpoint (it needs the
> department for new-grad filtering). That endpoint **omits postings with no department** —
> which is exactly how firms post events — so the *events* adapter reads the flat `/jobs`
> endpoint instead. Don't "simplify" one into the other.

If a run log shows `Citadel scraped 0 roles`, dump the raw response and inspect it:

```bash
./run.sh --once --debug     # writes quant/debug_citadel.html etc.
```

Open the dumped file, find the job-card anchors, and either adjust the link hints for that
company in `COMPANIES` or switch its `"adapter"`. For Citadel, Chrome DevTools → Network →
filter `admin-ajax` shows the real request.

## Tuning what counts as a match

Everything lives at the top of [job_watcher.py](job_watcher.py):
- `SWE_TERMS` — a title/department must contain one of these.
- `NEWGRAD_TERMS` — required in `"filter"` mode.
- `INTERN_TERMS` + `EXCLUDE_INTERN` — interns are dropped by default; flip to include.
- `COMPANIES[].newgrad_mode` — `scoped` (source already new-grad), `filter` (require a
  new-grad hint), or `all_swe` (all software roles; firm doesn't label level).
- `EVENT_TERMS` / `EVENT_EXCLUDE` — what counts as an event vs. an ordinary job posting.
- `EVENT_SOURCES` — dedicated firm events pages (Jane Street, HRT).
- `US_ONLY` — `True` by default. A posting is kept if it has **no** location **or** lists
  **at least one** US location; it's dropped only when *every* listed location is
  recognisably outside the US. If a city is mis-placed, add it to `_US_CITIES` / `_FOREIGN`.

## Add another company

Append to `COMPANIES`. If it's Greenhouse-backed (check its apply URL for
`boards.greenhouse.io/<token>`), it's a one-liner:

```python
{"name": "Some Firm", "adapter": "greenhouse", "board_token": "<token>", "newgrad_mode": "filter"},
```

Workday is supported too (`"adapter": "workday"` with `wd_host` / `wd_tenant` / `wd_site`).
For Lever/Ashby-backed firms, see the adapters in [../tech/tech_watcher.py](../tech/tech_watcher.py).
