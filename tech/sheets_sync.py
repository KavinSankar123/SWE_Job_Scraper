#!/usr/bin/env python3
"""
sheets_sync.py — Append newly-scraped mid-level roles to the Google Sheets
application tracker, at the same moment tech_watcher.py emails them.

Columns are matched by HEADER NAME, not by position
---------------------------------------------------
The tracker's header row is read on every run and each value is placed in the
column whose header names it:

    Company | Job Name | Status | Date Scraped | Date Applied | App Link

Only Company and Job Name are required; the rest are filled in when the sheet
has them. This is deliberate. Position-based writing looks fine right up until
a tracker turns out to be missing a column, or someone inserts or reorders one
— at which point every value after it lands one column off and nothing
complains. Matching on the header means a reordered, widened or partial tracker
still gets correct rows, and a column the sheet does not have is skipped rather
than shifting everything after it.

Status and Date Applied are left BLANK on purpose — an empty Status means
"found, not applied yet". You fill in "App sent" and the date by hand when you
actually apply, so the six defined statuses keep meaning what they say.

One more thing about the tracker's shape
----------------------------------------
Its App Link column carries the literal text "Link" for hundreds of formatted
template rows past the last real entry. Sheets' own `values.append` looks for
the last row containing ANY data, so it would land far below the real data and
leave a several-hundred-row hole. We find the last non-empty **Company** cell
instead and write directly under it. That "Link" is display text over a
hyperlink, not a bare URL, so rows are written as `=HYPERLINK("...","Link")`
and read back with `valueRenderOption=FORMULA` to recover the URL for dedup.

Config (environment variables, set in run_tech.sh):

    GSHEET_CREDENTIALS   path to the service-account JSON key   (required)
    GSHEET_ID            spreadsheet id                         (default: the tracker)
    GSHEET_GID           numeric tab id to write into           (default: 1940679258)

If GSHEET_CREDENTIALS is unset, syncing is simply OFF and the watcher behaves
exactly as it did before. Any API failure is logged and swallowed: a sheet
problem must never cost you a job alert.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger("tech_watcher")

# The tracker this repo was wired up against. Override with GSHEET_ID / GSHEET_GID.
DEFAULT_SHEET_ID = "1kntHhfWMeW1EeT8ZPfse8FzMQQkOzUnIg2AQqTp7z3w"
DEFAULT_GID = 1940679258

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# How many columns to read. Generous — the header row decides the real width.
_READ_COLS = "Z"

# Header text (normalised) -> the field it holds. The tracker's own spellings,
# plus obvious synonyms, so a lightly-renamed column still lines up.
_FIELD_BY_HEADER = {
    "company": "company",
    "job name": "job_name",
    "job title": "job_name",
    "role": "job_name",
    "status": "status",
    "date scraped": "date_scraped",
    "scraped": "date_scraped",
    "date found": "date_scraped",
    "date applied": "date_applied",
    "applied": "date_applied",
    "app link": "app_link",
    "application link": "app_link",
    "link": "app_link",
    "job link": "app_link",
}

# Without these two a row would not identify a job at all.
REQUIRED_FIELDS = ("company", "job_name")
# Written when present, skipped when the sheet has no such column.
OPTIONAL_FIELDS = ("status", "date_scraped", "date_applied", "app_link")

_PRETTY = {
    "company": "Company",
    "job_name": "Job Name",
    "status": "Status",
    "date_scraped": "Date Scraped",
    "date_applied": "Date Applied",
    "app_link": "App Link",
}


class SheetError(RuntimeError):
    """Raised for configuration problems worth showing the user verbatim."""


def _normalise(header: str) -> str:
    """Fold a header cell to its lookup key: lowercase, collapsed whitespace."""
    return re.sub(r"\s+", " ", (header or "").strip().lower())


@dataclass(frozen=True)
class Layout:
    """Which column each field lives in, read from the tab's header row."""
    index: dict[str, int]              # field -> 0-based column
    width: int                         # how many columns the header row spans
    headers: tuple[str, ...]

    def has(self, field: str) -> bool:
        return field in self.index

    def missing_optional(self) -> list[str]:
        return [f for f in OPTIONAL_FIELDS if f not in self.index]


def _layout(header_row: list[str]) -> Layout:
    index: dict[str, int] = {}
    for col, cell in enumerate(header_row):
        field = _FIELD_BY_HEADER.get(_normalise(cell))
        if field and field not in index:        # first column wins on a duplicate
            index[field] = col

    missing = [_PRETTY[f] for f in REQUIRED_FIELDS if f not in index]
    if missing:
        raise SheetError(
            f"The tab's header row is missing {', '.join(missing)} — got "
            f"{header_row}. Check GSHEET_GID points at the tracker tab."
        )
    return Layout(index=index, width=len(header_row), headers=tuple(header_row))


def _col_letter(i: int) -> str:
    """0-based column index -> spreadsheet letter (0 -> A, 26 -> AA)."""
    letter = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        letter = chr(ord("A") + rem) + letter
    return letter


@dataclass(frozen=True)
class SheetRow:
    """One tracker row, before it is laid out against the sheet's own columns."""
    company: str
    job_name: str
    date_scraped: str          # ISO yyyy-mm-dd; Sheets parses this into a real date
    app_link: str

    def to_values(self, layout: Layout) -> list[str]:
        """Place each value in the column the header row says it belongs in."""
        values = [""] * layout.width
        cells = {
            "company": self.company,
            "job_name": self.job_name,
            "date_scraped": self.date_scraped,
            # Status and Date Applied stay blank — see the module docstring.
            "app_link": f'=HYPERLINK("{self.app_link}","Link")' if self.app_link else "",
        }
        for field, value in cells.items():
            col = layout.index.get(field)
            if col is not None:
                values[col] = value
        return values


# --------------------------------------------------------------------------- #
# Config / auth
# --------------------------------------------------------------------------- #
def is_enabled() -> bool:
    return bool(os.getenv("GSHEET_CREDENTIALS"))


def _config() -> tuple[str, str, int]:
    creds = os.getenv("GSHEET_CREDENTIALS", "")
    if not creds:
        raise SheetError(
            "GSHEET_CREDENTIALS is not set — add the path to your service-account "
            "JSON key in run_tech.sh. See tech/SHEETS_SETUP.md."
        )
    if not Path(creds).expanduser().is_file():
        raise SheetError(f"GSHEET_CREDENTIALS points at a file that does not exist: {creds}")

    sheet_id = os.getenv("GSHEET_ID", DEFAULT_SHEET_ID)
    try:
        gid = int(os.getenv("GSHEET_GID", str(DEFAULT_GID)))
    except ValueError as e:
        raise SheetError(f"GSHEET_GID must be a number, got {os.getenv('GSHEET_GID')!r}") from e
    return str(Path(creds).expanduser()), sheet_id, gid


def _service():
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:  # pragma: no cover - depends on the venv
        raise SheetError(
            "Google API libraries missing — run:  .venv/bin/pip install -r requirements.txt"
        ) from e

    creds_path, _, _ = _config()
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _meta(svc, sheet_id: str) -> tuple[str, list[tuple[str, int]]]:
    """The spreadsheet's own title, and every tab as (title, gid)."""
    meta = svc.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="properties.title,sheets.properties(sheetId,title)").execute()
    tabs = [(sh["properties"]["title"], sh["properties"]["sheetId"])
            for sh in meta.get("sheets", [])]
    return meta.get("properties", {}).get("title", "?"), tabs


def tab_url(sheet_id: str, gid: int) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={gid}"


def _tab_title(svc, sheet_id: str, gid: int) -> str:
    """Resolve a numeric gid to its tab title, so renaming the tab can't break us."""
    _, tabs = _meta(svc, sheet_id)
    for title, sid in tabs:
        if sid == gid:
            return title
    known = ", ".join(f"{t} (gid={g})" for t, g in tabs)
    raise SheetError(f"No tab with gid={gid} in that spreadsheet. Tabs present: {known}")


# --------------------------------------------------------------------------- #
# Reading what's already there
# --------------------------------------------------------------------------- #
def _hyperlink_url(cell: str) -> str:
    """Pull the URL out of a =HYPERLINK("url","Link") formula, or a bare URL cell."""
    cell = (cell or "").strip()
    if cell.lower().startswith("=hyperlink("):
        inner = cell[len("=hyperlink("):].rstrip(")")
        first = inner.split(",", 1)[0].strip()
        return first.strip('"').strip()
    if cell.lower().startswith(("http://", "https://")):
        return cell
    return ""          # includes the bare "Link" placeholder in the template rows


def dedup_key(company: str, job_name: str, url: str) -> str:
    """A row's identity: its URL when it has one, else company + title."""
    if url:
        return url.strip().rstrip("/").lower()
    return f"{company.strip().lower()}::{job_name.strip().lower()}"


def _read_grid(svc, sheet_id: str, title: str) -> list[list[str]]:
    """The tab's used range, formulas intact so HYPERLINK URLs survive."""
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{title}'!A:{_READ_COLS}",
        valueRenderOption="FORMULA",
    ).execute()
    return resp.get("values", [])


def _cell(row: list[str], col: int | None) -> str:
    """Sheets truncates trailing empty cells, so short rows are normal."""
    if col is None or col >= len(row):
        return ""
    return row[col] or ""


def _existing_keys(grid: list[list[str]], layout: Layout) -> set[str]:
    company_col = layout.index["company"]
    job_col = layout.index["job_name"]
    link_col = layout.index.get("app_link")

    keys: set[str] = set()
    for row in grid[1:]:                       # skip the header
        company = _cell(row, company_col)
        job_name = _cell(row, job_col)
        url = _hyperlink_url(_cell(row, link_col))
        if not (company.strip() or url):
            continue
        keys.add(dedup_key(company, job_name, url))
    return keys


def _first_free_row(grid: list[list[str]], layout: Layout) -> int:
    """
    1-based row number just past the last row with a Company set.

    Deliberately keyed on the Company column alone. The template rows below the
    real data carry a "Link" in the App Link column, so anything that asks
    "where does the data end?" across all columns answers several hundred rows
    too far down.
    """
    company_col = layout.index["company"]
    last = 1                                   # the header row always exists
    for i, row in enumerate(grid, start=1):
        if _cell(row, company_col).strip():
            last = i
    return last + 1


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def append_rows(rows: list[SheetRow]) -> int:
    """
    Write rows into the tracker, skipping any already present. Returns the
    number actually written. Raises SheetError for config problems; callers in
    the watcher catch everything so a sheet outage can't break an email run.
    """
    if not rows:
        return 0

    _, sheet_id, gid = _config()
    svc = _service()
    title = _tab_title(svc, sheet_id, gid)

    grid = _read_grid(svc, sheet_id, title)
    if not grid:
        raise SheetError(f"The tab '{title}' is empty — it needs a header row.")
    layout = _layout(grid[0])

    for field in layout.missing_optional():
        log.warning("Sheet: '%s' has no %s column — leaving that value out.",
                    title, _PRETTY[field])

    seen = _existing_keys(grid, layout)
    fresh: list[SheetRow] = []
    for r in rows:
        key = dedup_key(r.company, r.job_name, r.app_link)
        if key in seen:
            continue
        seen.add(key)                          # guard against dupes within one batch
        fresh.append(r)

    if not fresh:
        log.info("Sheet: all %d role(s) already tracked in '%s'.", len(rows), title)
        return 0

    start = _first_free_row(grid, layout)
    end = start + len(fresh) - 1
    last_col = _col_letter(layout.width - 1)
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{title}'!A{start}:{last_col}{end}",
        valueInputOption="USER_ENTERED",       # dates become dates, HYPERLINK runs
        body={"values": [r.to_values(layout) for r in fresh]},
    ).execute()

    log.info("Sheet: added %d role(s) to '%s' at row %d.", len(fresh), title, start)
    return len(fresh)


def sync_jobs(jobs, when: str | None = None) -> int:
    """
    Push Job objects (from tech_watcher) into the tracker. Never raises — a
    sheet failure is logged and the caller carries on.
    """
    if not jobs:
        return 0
    if not is_enabled():
        log.debug("Sheet sync off (GSHEET_CREDENTIALS unset).")
        return 0

    scraped = when or date.today().isoformat()
    rows = [SheetRow(j.company, j.title, scraped, j.url) for j in jobs]
    try:
        return append_rows(rows)
    except SheetError as e:
        log.error("Sheet sync skipped — %s", e)
    except Exception as e:  # noqa: BLE001 - a sheet problem must not lose an alert
        log.error("Sheet sync failed: %s", e)
    return 0


def check() -> int:
    """Verify credentials, tab and columns without writing anything."""
    try:
        creds_path, sheet_id, gid = _config()
        svc = _service()
        title = _tab_title(svc, sheet_id, gid)
        grid = _read_grid(svc, sheet_id, title)
        if not grid:
            raise SheetError(f"The tab '{title}' is empty — it needs a header row.")
        layout = _layout(grid[0])
    except SheetError as e:
        log.error("%s", e)
        return 2
    except Exception as e:  # noqa: BLE001
        log.error("Could not reach the sheet: %s", e)
        log.error("If this says 403 / PERMISSION_DENIED, share the spreadsheet with "
                  "the service account's client_email as an Editor.")
        return 1

    try:
        file_title, tabs = _meta(svc, sheet_id)
    except Exception:  # noqa: BLE001 - diagnostics only, never fail the check
        file_title, tabs = "?", []

    print(f"  credentials : {creds_path}")
    print(f"  spreadsheet : {file_title!r}")
    print(f"  tab         : {title!r} (gid={gid})")
    print(f"  OPEN THIS   : {tab_url(sheet_id, gid)}")
    print("                (this is the exact tab rows are written to — if it is not")
    print("                 the one you have been looking at, that is the mismatch)")
    print(f"  header      : {list(layout.headers)}")
    print(f"  tracked rows: {len(_existing_keys(grid, layout))}")
    print(f"  next write  : row {_first_free_row(grid, layout)}")
    print("  columns     :")
    for field in REQUIRED_FIELDS + OPTIONAL_FIELDS:
        col = layout.index.get(field)
        where = f"column {_col_letter(col)}" if col is not None else "NOT IN SHEET"
        print(f"      {_PRETTY[field]:<13} {where}")

    if len(tabs) > 1:
        print(f"  all {len(tabs)} tabs in this file:")
        try:
            resp = svc.spreadsheets().values().batchGet(
                spreadsheetId=sheet_id,
                ranges=[f"'{t}'!1:1" for t, _ in tabs]).execute()
            heads = [r.get("values", [[]])[0] if r.get("values") else []
                     for r in resp.get("valueRanges", [])]
        except Exception:  # noqa: BLE001
            heads = [[] for _ in tabs]
        for (t, g), head in zip(tabs, heads):
            mark = " <-- writing here" if g == gid else ""
            print(f"      {t!r} (gid={g}){mark}")
            print(f"          header: {head}")
    else:
        print("  this file has exactly one tab — so the sheet you are looking at with")
        print("  different columns is a DIFFERENT FILE (or a downloaded copy of this one).")

    missing = layout.missing_optional()
    if missing:
        pretty = ", ".join(_PRETTY[f] for f in missing)
        print()
        print(f"  NOTE: this tab has no {pretty} column.")
        print("  Rows will still sync correctly — that value is simply left out, and")
        print("  nothing shifts into the wrong column. To record it, add a column with")
        print("  that exact header (anywhere in the row — the sync matches on header")
        print("  text, not position) and re-run this check.")
        print("\nSheet check: OK — with the note above ✅")
        return 0

    print("\nSheet check: OK ✅")
    return 0
