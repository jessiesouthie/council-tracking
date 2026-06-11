"""
Read-only client for the Eagle Mountain CivicClerk public OData API.

  list_council_events()             -> iterable of event summaries
  get_meeting(event_id)             -> meeting detail w/ publishedFiles[]
  download_file(file_id, dest_path) -> writes PDF bytes to dest_path

The API requires no authentication. Endpoints discovered from the portal
SPA bundle (assets/index-*.js):

  Base: https://eaglemountainut.api.civicclerk.com/v1
  - GET  Events?$filter=categoryName eq 'City Council'
        -> each event already includes publishedFiles[] with the attachments.
  - GET  Meetings/{eventId}
        -> returns agenda items, not the file list (publishedFiles is empty).
  - GET  Meetings/GetMeetingFileStream(fileId={fileId},plainText=false)
        -> streams the PDF bytes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import requests

# Every body we crawl lives on Eagle Mountain's single CivicClerk portal, so the
# base URL is a module constant. If a future body lives on a different portal,
# promote BASE into the body config (see ingest/bodies.py).
BASE = "https://eaglemountainut.api.civicclerk.com/v1"
USER_AGENT = "council-tracking/0.1 (+https://github.com/jessiesouthie/Council-Tracking)"

# Default CivicClerk category. Callers pass other categories (e.g. "Planning
# Commission") for non-council bodies. If a label ever shifts ("City Council" ->
# "City Council Meetings" etc.) update the body config in ingest/bodies.py.
COUNCIL_CATEGORY = "City Council"

# Published-file type for approved minutes (observed from the API payload).
FILE_TYPE_MINUTES = 4


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def list_council_events(
    session: requests.Session | None = None,
    category: str = COUNCIL_CATEGORY,
) -> Iterator[dict]:
    """Yield every published event in `category` the API exposes, newest first.

    The API silently caps each response at ~15 rows regardless of $top, so we
    advance $skip by the page length we actually received and stop when the
    server returns an empty page. A guard cap prevents an infinite loop if the
    API ever starts returning duplicate pages.
    """
    s = session or _session()
    skip = 0
    GUARD_CAP = 2000
    while skip < GUARD_CAP:
        r = s.get(
            f"{BASE}/Events",
            params={
                "$top": 100,  # server may ignore but doesn't hurt
                "$skip": skip,
                "$orderby": "eventDate desc",
                "$filter": f"categoryName eq '{category}'",
            },
            timeout=30,
        )
        r.raise_for_status()
        page = r.json().get("value", [])
        if not page:
            return
        for ev in page:
            yield ev
        skip += len(page)


def get_meeting(event_id: int, session: requests.Session | None = None) -> dict:
    """Return the full meeting record (includes publishedFiles array)."""
    s = session or _session()
    r = s.get(f"{BASE}/Meetings/{event_id}", timeout=30)
    r.raise_for_status()
    return r.json()


def find_approved_minutes(meeting: dict) -> dict | None:
    """Return the publishedFiles entry that represents the approved minutes,
    or None if the meeting doesn't have one yet.

    We match on fileType == 4 (Minutes) AND a name that signals the published
    version. Drafts get an "Approved" prefix in practice; if a meeting only
    has a draft, we still pick it up.
    """
    files = meeting.get("publishedFiles") or []
    minutes = [f for f in files if f.get("fileType") == FILE_TYPE_MINUTES]
    if not minutes:
        return None
    # Prefer files whose name signals approval, then fall back to most recent.
    minutes.sort(
        key=lambda f: (
            "approved" not in (f.get("name") or "").lower(),
            -_publish_ts(f),
        )
    )
    return minutes[0]


def _publish_ts(file_entry: dict) -> int:
    s = file_entry.get("publishOn") or ""
    # Lex-order is fine for ISO timestamps; convert for sort stability.
    return int("".join(c for c in s if c.isdigit())[:14] or "0")


def download_file(
    file_id: int, dest: Path, session: requests.Session | None = None
) -> int:
    """Stream a PDF for the given publishedFile.fileId to dest. Returns bytes written."""
    s = session or _session()
    url = (
        f"{BASE}/Meetings/GetMeetingFileStream("
        f"fileId={file_id},plainText=false)"
    )
    with s.get(url, timeout=60, stream=True) as r:
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with dest.open("wb") as fh:
            for chunk in r.iter_content(64 * 1024):
                if chunk:
                    fh.write(chunk)
                    n += len(chunk)
    return n
