"""
Publish the meetings that haven't happened yet.

Everything else in this repo looks backwards — minutes, votes, transcripts, all
of it after the fact. This is the one forward-looking file: the city's own
calendar, so the site can tell a resident when the council next sits and what
time to turn up.

Source is the same CivicClerk portal the crawl reads. Writes
docs/data.upcoming.json:

  {
    "generated_at": ISO timestamp,
    "source": "Eagle Mountain CivicClerk portal",
    "timezone": "America/Denver",          # what the naive times below mean
    "bodies": {
      "<body id>": [
        {"id", "title", "date": "YYYY-MM-DD",
         "start": "HH:MM", "start_label": "4:00 PM",   # local wall clock
         "location", "url",
         "agenda_posted": bool,
         # sessions come from the agenda PDF, when one is posted
         "sessions": [{"start", "start_label", "label", "public_comment"}]}
      ]
    }
  }

Times are Eagle Mountain wall-clock with no offset attached, matching how the
API reports them and how the agenda prints them — see civicclerk.LOCAL_TZ.

Run:  python -m ingest.build_upcoming                  # every CivicClerk body
      python -m ingest.build_upcoming --body city-council
      python -m ingest.build_upcoming --horizon 6      # keep 6 meetings ahead
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pdfplumber
import requests

from . import bodies, civicclerk

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data.upcoming.json"
AGENDA_CACHE = ROOT / "data" / "agendas"

PORTAL_EVENT = "https://eaglemountainut.portal.civicclerk.com/event/{id}/overview"

# How many scheduled meetings to publish. The calendar is booked a year out, and
# the site only ever shows the front of it, but keeping a handful means the file
# stays useful for a few weeks if an ingest run is missed.
DEFAULT_HORIZON = 6

# Only the next couple of meetings are worth opening an agenda PDF for: agendas
# are posted days, not months, ahead, so anything further out has none.
AGENDA_LOOKAHEAD = 3

# "4:00 PM WORK SESSION - CITY COUNCIL CHAMBERS" — the agenda's own session
# headings. Eagle Mountain runs a work session first and the voting (policy)
# session hours later, and which one a resident wants is the whole question.
SESSION_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*([AP])\.?M\.?\s+([A-Z][A-Z' \-]*SESSION)\b",
    re.MULTILINE,
)

# The agenda's own heading for the open-mic slot, numbered as an agenda item
# ("7. PUBLIC COMMENTS"). Anchored to the line start so a passing mention in the
# body text of some other item doesn't count as a comment period.
PUBLIC_COMMENT_RE = re.compile(
    r"^\s*(?:\d+\.\s*)?PUBLIC COMMENT", re.MULTILINE | re.IGNORECASE
)


def local_today() -> str:
    """Today in Eagle Mountain, as YYYY-MM-DD."""
    return datetime.now(ZoneInfo(civicclerk.LOCAL_TZ)).strftime("%Y-%m-%d")


def split_event_time(event_date: str) -> tuple[str, str]:
    """('2026-08-18T16:00:00Z') -> ('2026-08-18', '16:00'), no timezone maths.

    The trailing Z is a lie the API tells (civicclerk.LOCAL_TZ); the digits in
    front of it are already Eagle Mountain wall-clock.
    """
    day, _, rest = (event_date or "").partition("T")
    return day[:10], rest[:5]


def label_time(hhmm: str) -> str:
    """'16:00' -> '4:00 PM'. Empty in, empty out."""
    if not re.fullmatch(r"\d{2}:\d{2}", hhmm or ""):
        return ""
    h, m = int(hhmm[:2]), hhmm[3:]
    suffix = "AM" if h < 12 else "PM"
    return f"{h % 12 or 12}:{m} {suffix}"


def format_location(loc: dict | None) -> str:
    """Flatten the API's address record into one line, skipping blanks."""
    if not loc:
        return ""
    parts = [loc.get("address1"), loc.get("address2")]
    return ", ".join(p.strip() for p in parts if (p or "").strip())


def agenda_pdf(body: dict, event: dict, file_entry: dict,
               session: requests.Session) -> Path | None:
    """Fetch (and cache) the agenda PDF for an event. None if it won't download.

    Cached under the file id: CivicClerk issues a new one when an agenda is
    amended, so a re-published agenda is a cache miss rather than a stale hit.
    """
    dest = AGENDA_CACHE / body["id"] / f"{event['id']}__{file_entry['fileId']}.pdf"
    if dest.exists():
        return dest
    try:
        civicclerk.download_file(file_entry["fileId"], dest, session=session)
    except Exception as exc:
        print(f"  ! agenda download failed for event {event['id']}: {exc}")
        return None
    return dest


def read_sessions(pdf_path: Path) -> list[dict]:
    """Pull the session headings ("4:00 PM WORK SESSION") out of an agenda.

    Each session is also checked for a public-comment slot, because that — not
    the meeting's nominal start — is the hour a resident who wants to be heard
    needs to show up for. The flag is read off this agenda rather than assumed
    from how the council usually runs its night.

    Returns [] rather than raising for an unreadable or differently-shaped
    agenda — the meeting's own start time still carries the page.
    """
    try:
        data = pdf_path.read_bytes()
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as exc:
        print(f"  ! could not read {pdf_path.name}: {exc}")
        return []

    matches = list(SESSION_RE.finditer(text))
    out: list[dict] = []
    seen: set[str] = set()
    for i, m in enumerate(matches):
        hh, mm, ampm, name = m.groups()
        h = int(hh) % 12 + (12 if ampm == "P" else 0)
        start = f"{h:02d}:{mm}"
        if start in seen:
            continue
        seen.add(start)
        # Everything up to the next session heading belongs to this one.
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append({
            "start": start,
            "start_label": label_time(start),
            "label": name.title().replace("'S", "'s"),
            "public_comment": bool(PUBLIC_COMMENT_RE.search(text[m.end():end])),
        })
    out.sort(key=lambda s: s["start"])
    return out


def build_body(body: dict, *, horizon: int, since: str,
               session: requests.Session) -> list[dict]:
    events = civicclerk.list_upcoming_events(
        since, session=session, category=body["category"], limit=horizon * 3
    )

    out: list[dict] = []
    for ev in events:
        date, start = split_event_time(ev.get("eventDate") or "")
        if not date:
            continue
        entry = {
            "id": ev["id"],
            "title": (ev.get("eventName") or f"{body['label']} Meeting").strip(),
            "date": date,
            "start": start,
            "start_label": label_time(start),
            "location": format_location(ev.get("eventLocation")),
            "url": PORTAL_EVENT.format(id=ev["id"]),
            "agenda_posted": False,
            "sessions": [],
        }

        agenda = civicclerk.find_agenda(ev)
        if agenda and agenda.get("fileId"):
            entry["agenda_posted"] = True
            if len(out) < AGENDA_LOOKAHEAD:
                pdf = agenda_pdf(body, ev, agenda, session)
                if pdf:
                    entry["sessions"] = read_sessions(pdf)

        out.append(entry)
        if len(out) >= horizon:
            break

    for e in out:
        extra = " · ".join(f"{s['label']} {s['start_label']}" for s in e["sessions"])
        print(f"  {e['date']} {e['start_label']:>8}  {e['title']}"
              + (f"   [{extra}]" if extra else ""))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--body", help="single body id (default: every CivicClerk body)")
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON,
                    help=f"meetings to publish per body (default {DEFAULT_HORIZON})")
    ap.add_argument("--since", help="keep meetings on/after this day (default: today)")
    args = ap.parse_args()

    since = args.since or local_today()
    targets = [bodies.get_body(args.body)] if args.body else bodies.all_bodies()

    session = requests.Session()
    session.headers.update(
        {"User-Agent": civicclerk.USER_AGENT, "Accept": "application/json"}
    )

    manifest: dict[str, list[dict]] = {}
    for body in targets:
        if body.get("source") != "civicclerk":
            continue  # manual bodies have no calendar to read
        print(f"\n== {body['label']} ({body['id']}) ==", flush=True)
        try:
            entries = build_body(body, horizon=args.horizon, since=since,
                                 session=session)
        except Exception as exc:
            print(f"  ! calendar fetch failed: {exc}", file=sys.stderr)
            continue
        if entries:
            manifest[body["id"]] = entries
        else:
            print("  (nothing scheduled)")

    if not manifest:
        print("no upcoming meetings found; leaving the existing file alone",
              file=sys.stderr)
        return 1

    OUT.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": "Eagle Mountain CivicClerk portal",
                "timezone": civicclerk.LOCAL_TZ,
                "bodies": manifest,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
