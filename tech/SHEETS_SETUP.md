# Google Sheets sync — setup

The tech watcher can append every new mid-level role straight into the
**Mid Level SWE Job App Tracker**, at the same moment it emails you.

It writes the row from the scraper's own `Job` objects, in `run_once()` right
next to `send_email()` — it does **not** read your inbox. The email and the
sheet row come from the same data, so nothing has to be parsed back out of an
email, and changing the email template can never break the sync.

Values are matched to columns by **header name**, so a reordered or partial
tracker still gets correct rows. What lands in the sheet:

| Column | Value |
|---|---|
| Company | the company name |
| Job Name | the job title |
| Status | **blank** — an empty Status means "found, not applied yet" |
| Date Scraped | the day the watcher found it (only if your tab has this column) |
| Date Applied | **blank** — you fill this in when you apply |
| App Link | `Link`, hyperlinked to the posting |

Status and Date Applied stay yours to fill in. Your six statuses
(App sent / Ghosted / OA / Interview / Offer / Rejected) all describe something
*you* did, and the scraper hasn't done any of them.

The whole feature is **off until you set `GSHEET_CREDENTIALS`**. Without it the
watcher behaves exactly as it always has.

---

## 1. Make a service account

A service account is a robot Google account with its own email address. It
suits a background launchd job because it never needs a browser and its
credentials don't expire — unlike normal OAuth, which would pop a consent
screen your watcher can't answer at 3am.

1. Go to <https://console.cloud.google.com/> and create a project (any name —
   `job-scraper` is fine).
2. **APIs & Services → Library →** search **Google Sheets API → Enable**.
3. **APIs & Services → Credentials → Create credentials → Service account**.
   Give it a name, click through the optional role/access steps — it needs **no**
   project roles at all; access comes from sharing the sheet in step 2 below.
4. Open the new service account → **Keys → Add key → Create new key → JSON**.
   A `.json` file downloads.

## 2. Share the sheet with it

Open that JSON and copy the `client_email` value — it looks like
`job-scraper@your-project.iam.gserviceaccount.com`.

In the tracker, hit **Share**, paste that address, give it **Editor**, and
untick "Notify people". Nothing is written until you do this; the service
account starts with access to nothing.

## 3. Park the key outside the repo

It's a credential, so keep it where a stray `git add` can't reach it:

```bash
mkdir -p ~/.config/job-scraper
mv ~/Downloads/your-project-*.json ~/.config/job-scraper/gsheet-service-account.json
chmod 600 ~/.config/job-scraper/gsheet-service-account.json
```

## 4. Point the watcher at it

Install the libraries and uncomment the line in your (git-ignored) `run_tech.sh`:

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
export GSHEET_CREDENTIALS="$HOME/.config/job-scraper/gsheet-service-account.json"
```

`GSHEET_ID` and `GSHEET_GID` are already defaulted to this tracker and its tab,
so leave them commented unless you move to a different sheet.

## 5. Check it

```bash
./run_tech.sh --sheet-check
```

Writes nothing. It reports the tab it resolved, the header row it found, which
column each value will go in, and which row the next write lands on:

```
  tab         : 'Fall 2026 New Grad Recruiting' (gid=1940679258)
  header      : ['Company', 'Job Name', 'Status', 'Date Applied', 'App Link']
  tracked rows: 1
  next write  : row 3
  columns     :
      Company       column A
      Job Name      column B
      Status        column C
      Date Scraped  NOT IN SHEET
      Date Applied  column D
      App Link      column E

  NOTE: this tab has no Date Scraped column.
```

**Check the `columns` block.** Values are placed by **header name**, not by
position, so a reordered or widened tracker still gets correct rows and a column
the sheet doesn't have is left out rather than shifting everything after it.

If a column you want reads `NOT IN SHEET`, add one whose header is exactly that
text — anywhere in the row — and re-run the check. To get **Date Scraped**
recorded, insert a column with that header; until then rows sync fine without it.

Also confirm the tab name is the one you expect. The gid in the tracker link
belongs to a tab called *Fall 2026 New Grad Recruiting*, which is an odd name for
a mid-level tracker. If that's a leftover name, fine — the sync follows the
**gid**, not the name, so renaming the tab won't break it. If it's genuinely the
wrong tab, set `GSHEET_GID` (open the tab and read `gid=` out of the URL).

If this prints `403 / PERMISSION_DENIED`, step 2 didn't take — re-share the
sheet with the `client_email`.

### If pip won't run at all

```
zsh: .venv/bin/pip: bad interpreter: .../.venv/bin/python: no such file
```

That's a broken virtualenv, not a missing package — a venv bakes an absolute
path into every script in `.venv/bin/`, so copying the project folder to another
machine or path (or losing the Python it was built from) kills it. `setup.sh`
reuses an existing `.venv`, so you have to delete it first:

```bash
deactivate 2>/dev/null
rm -rf .venv
./setup.sh
```

Your git-ignored `run_tech.sh` and sqlite store are untouched by this.

## 6. Backfill what's already been seen

The very first watcher run seeds its store **silently** (no email), so those
roles would otherwise never reach the sheet. Push them in:

```bash
./run_tech.sh --backfill-sheet
```

Each backfilled row keeps the date it was *first seen*, not today, so
Date Scraped stays honest (when your tab has that column). Rows already in the sheet are skipped, so running it
twice is harmless.

From here it's automatic: every `--once` run that emails you also writes those
same roles into the tracker.

## 7. Fill in links on rows you added by hand

A row you typed in yourself has a company and a title but no link. If the
watcher has since seen that job, it can fill the App Link cell in for you:

```bash
./run_tech.sh --fill-links           # dry run — prints what it would change
./run_tech.sh --fill-links --apply   # actually write
```

It is a **dry run by default**, so you always see the list before anything is
written. What it will and won't touch:

* Only an **empty** App Link cell is written. The bare `Link` placeholder counts
  as empty; a real hyperlink does not, so a link already in the sheet is never
  overwritten.
* Only rows with a **Company** set. The hundreds of blank template rows say
  `Link` too, and are left alone.
* Only the App Link cell, one cell at a time — no other column on the row is
  touched, so your Status and dates are safe.
* Matching is on **company + title**, because a linkless row has nothing else to
  go on. If two postings at one company share a title, that row is skipped and
  reported rather than guessed at.

Rows it can't match are listed too, so you can see what was left behind.

---

## How duplicates are avoided

Two independent guards:

* The watcher's own sqlite store (`seen_tech_jobs.sqlite3`) means a job is only
  ever "new" once, so it's only ever offered to the sheet once.
* `sheets_sync` reads the sheet before every write and skips anything already
  there. Rows are indexed two ways: a row **with** a link is matched on that
  link, a row **without** one on company + title. Both are needed. A scraped job
  always has a URL, so matching only on URL would fail to recognise a row you
  typed in by hand and would file a second copy of the same job; matching
  everything on title would merge two genuinely different postings that happen
  to share a title at one company.

So wiping the sqlite store, running a backfill twice, or having already added a
row yourself won't double up rows.

## Where new rows go

The tracker has formatted template rows running hundreds of rows past the last
real entry, each carrying the literal text `Link` in the App Link column.
Google's own
`values.append` looks for the last row containing *any* data, so it would land
below all of those and leave a several-hundred-row gap. The sync finds the last
non-empty **Company** cell instead and writes directly underneath it, into your
existing formatting.

## Turning it off

Comment out `GSHEET_CREDENTIALS` in `run_tech.sh`. The watcher goes back to
email-only.

## If the sheet breaks

Sheet failures are logged and swallowed — a Google outage, a revoked key or a
deleted tab will never cost you a job alert or corrupt the dedup store. Check
`tech/tech_watcher.log` for a line starting `Sheet sync failed`, then re-run
`--sheet-check`. Anything missed while the sheet was down can be recovered with
`--backfill-sheet`.
