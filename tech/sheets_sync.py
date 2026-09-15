#!/usr/bin/env python3
"""
sheets_sync.py — Append newly-scraped mid-level roles to the Google Sheets
application tracker, at the same moment tech_watcher.py emails them.

Columns written (A-F), matching the tracker:

    Company | Job Name | Status | Date Scraped | Date Applied | App Link

Status and Date Applied are left BLANK on purpose — an empty Status means
"found, not applied yet". You fill in "App sent" and the date by hand when you
actually apply, so the six defined statuses keep meaning what they say.

Two things about the tracker shape that this module has to work around
----------------------------------------------------------------------
  * Column F of the template rows already contains the literal text "Link",
    dragged down hundreds of rows past the last real entry. Sheets' own
    `values.append` looks for the last row containing ANY data, so it would
    land far below the real data and leave a several-hundred-row hole. We find
    the last non-empty **Company** cell instead and write directly under it.
  * That "Link" is display text over a hyperlink, not a bare URL. We write
    `=HYPERLINK("...","Link")` so new rows look like the ones already there.

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
from dataclasses import dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger("tech_watcher")

# The tracker this repo was wired up against. Override with GSHEET_ID / GSHEET_GID.
DEFAULT_SHEET_ID = "1kntHhfWMeW1EeT8ZPfse8FzMQQkOzUnIg2AQqTp7z3w"
DEFAULT_GID = 1940679258

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetError(RuntimeError):
    """Raised for configuration problems worth showing the user verbatim."""


@dataclass(frozen=True)
class SheetRow:
    """One tracker row, in column order."""
    company: str
    job_name: str
    date_scraped: str          # ISO yyyy-mm-dd; Sheets parses this into a real date
    app_link: str

    def to_values(self) -> list[str]:
        link = f'=HYPERLINK("{self.app_link}","Link")' if self.app_link else "Link"
        #      Company        Job Name        Status  Date Scraped        Date Applied
        return [self.company, self.job_name, "", self.date_scraped, "", link]


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


def _tab_title(svc, sheet_id: str, gid: int) -> str:
    """Resolve a numeric gid to its tab title, so renaming the tab can't break us."""
    meta = svc.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties(sheetId,title)").execute()
    for sh in meta.get("sheets", []):
        if sh["properties"]["sheetId"] == gid:
            return sh["properties"]["title"]
    known = ", ".join(
        f'{sh["properties"]["title"]} (gid={sh["properties"]["sheetId"]})'
        for sh in meta.get("sheets", [])
    )
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
    """Columns A-F of the tab, formulas intact so HYPERLINK URLs survive."""
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{title}'!A:F",
        valueRenderOption="FORMULA",
    ).execute()
    return resp.get("values", [])


def _existing_keys(grid: list[list[str]]) -> set[str]:
    keys: set[str] = set()
    for row in grid[1:]:                       # skip the header
        company = row[0] if len(row) > 0 else ""
        job_name = row[1] if len(row) > 1 else ""
        url = _hyperlink_url(row[5] if len(row) > 5 else "")
        if not (company.strip() or url):
            continue
        keys.add(dedup_key(company, job_name, url))
    return keys


def _first_free_row(grid: list[list[str]]) -> int:
    """
    1-based row number just past the last row with a Company set.

    Deliberately keyed on column A alone. The template rows below the real data
    carry a "Link" in column F, so anything that asks "where does the data end?"
    across all columns answers several hundred rows too far down.
    """
    last = 1                                   # the header row always exists
    for i, row in enumerate(grid, start=1):
        if row and row[0].strip():
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
    seen = _existing_keys(grid)

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

    start = _first_free_row(grid)
    end = start + len(fresh) - 1
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{title}'!A{start}:F{end}",
        valueInputOption="USER_ENTERED",       # dates become dates, HYPERLINK runs
        body={"values": [r.to_values() for r in fresh]},
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
    """Verify credentials, tab and permissions without writing anything."""
    try:
        creds_path, sheet_id, gid = _config()
        svc = _service()
        title = _tab_title(svc, sheet_id, gid)
        grid = _read_grid(svc, sheet_id, title)
    except SheetError as e:
        log.error("%s", e)
        return 2
    except Exception as e:  # noqa: BLE001
        log.error("Could not reach the sheet: %s", e)
        log.error("If this says 403 / PERMISSION_DENIED, share the spreadsheet with "
                  "the service account's client_email as an Editor.")
        return 1

    header = grid[0] if grid else []
    tracked = len(_existing_keys(grid))
    print(f"  credentials : {creds_path}")
    print(f"  spreadsheet : {sheet_id}")
    print(f"  tab         : {title!r} (gid={gid})")
    print(f"  header      : {header}")
    print(f"  tracked rows: {tracked}")
    print(f"  next write  : row {_first_free_row(grid)}")

    expected = ["Company", "Job Name", "Status", "Date Scraped", "Date Applied", "App Link"]
    if [h.strip() for h in header[:6]] != expected:
        print(f"\n  WARNING: header is not {expected} — check GSHEET_GID points at the "
              f"right tab.")
        return 1
    print("\nSheet check: OK ✅")
    return 0
