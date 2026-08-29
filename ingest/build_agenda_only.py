"""
Publish the meetings that left no record but the agenda.

The archive is built from approved minutes, and the recordings fill in the
meetings whose minutes haven't landed yet. Between those two there is a third
kind: a special session, a joint work session, a strategic planning conference —
held, noticed, and then never minuted and never recorded. The city's own
calendar knows about them; nothing on this site did, so a resident searching for
the night the council actually met found a gap where a meeting had been.

An agenda is a thinner record than minutes: it says what the council intended to
take up, not what it decided. That is worth publishing anyway — it is the only
public account of the night that exists — as long as the page says plainly which
it is. The pages built from this file carry that caveat, and every item on them
is the city's own wording, linked back to the PDF it came from.

Writes docs/data.agenda-only.json:

  {
    "generated_at": ISO timestamp,
    "source": "Eagle Mountain CivicClerk portal",
    "timezone": "America/Denver",           # what the naive times below mean
    "bodies": {
      "<body id>": [
        {"id", "title", "date": "YYYY-MM-DD",
         "start": "HH:MM", "start_label": "6:00 PM",
         "location", "url",                 # the portal's own event page
         "agenda_url": direct PDF link,
         "recording_url": the portal's video, when one was posted,
         "agenda_posted_on": "YYYY-MM-DD",  # the recorder's posting certificate
         "sessions": [...], "agenda": [...]}   # same shapes as data.upcoming.json
      ]
    }
  }

Deliberately the same entry shape as ingest.build_upcoming, because it is the
same document parsed by the same code — only read after the fact rather than
before. The site and ingest.build_meeting_pages render the two from one branch.

A meeting qualifies only if it has *no* other record: no approved minutes (it
would be in the body's data file) and no transcript (build_meeting_pages already
gives those a page of their own). So an agenda-only page never competes with a
fuller account of the same night, and one disappears from this file the week the
minutes are approved.

Run:  python -m ingest.build_agenda_only
      python -m ingest.build_agenda_only --body city-council
      python -m ingest.build_agenda_only --since 2024-01-01
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import agenda_summaries, bodies, civicclerk
from .build_upcoming import (
    PORTAL_EVENT,
    agenda_pdf,
    agenda_text,
    format_location,
    label_time,
    local_today,
    read_agenda,
    read_posted_on,
    read_sessions,
    split_event_time,
)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data.agenda-only.json"
TRANSCRIPT_INDEX = ROOT / "docs" / "transcripts" / "index.json"

PORTAL_MEDIA = "https://eaglemountainut.portal.civicclerk.com/event/{id}/media"


def minuted_ids(body: dict) -> set[int]:
    """Event ids that already have a page built from approved minutes."""
    path = bodies.data_file(body)
    if not path.exists():
        return set()
    data = json.loads(path.read_text())
    return {m["id"] for m in (data.get("meetings") or []) if m.get("id") is not None}


def transcribed_ids(body: dict) -> set[int]:
    """Event ids that have a published transcript, minuted or not."""
    if not TRANSCRIPT_INDEX.exists():
        return set()
    manifest = json.loads(TRANSCRIPT_INDEX.read_text())
    return {e["id"] for e in (manifest.get(body["id"]) or []) if e.get("id") is not None}


def build_body(body: dict, *, since: str, until: str,
               session: requests.Session) -> list[dict]:
    covered = minuted_ids(body) | transcribed_ids(body)

    out: list[dict] = []
    for ev in civicclerk.list_council_events(session=session, category=body["category"],
                                             strays=body.get("strays")):
        date, start = split_event_time(ev.get("eventDate") or "")
        if not date or date > until:
            continue          # still to come — that's build_upcoming's file
        if since and date < since:
            continue
        if ev["id"] in covered:
            continue

        agenda = civicclerk.find_agenda(ev)
        if not agenda or not agenda.get("fileId"):
            continue          # nothing published at all; there is no page to build

        entry = {
            "id": ev["id"],
            "title": (ev.get("eventName") or f"{body['label']} Meeting").strip(),
            "date": date,
            "start": start,
            "start_label": label_time(start),
            "location": format_location(ev.get("eventLocation")),
            "url": PORTAL_EVENT.format(id=ev["id"]),
            "agenda_url": civicclerk.file_url(agenda["fileId"]),
            # The portal has video for some of these. Untranscribed, it's no use
            # to a reader or a search engine, but it is the fuller record and the
            # page should send anyone who wants it there rather than claim the
            # night went unrecorded. A meeting leaves this file entirely once
            # that recording has been transcribed.
            "recording_url": PORTAL_MEDIA.format(id=ev["id"]) if ev.get("hasMedia") else "",
            "agenda_posted_on": "",
            "sessions": [],
            "agenda": [],
        }

        # Unlike the upcoming file there is no lookahead limit here: this set is
        # small and closed (a few dozen meetings across the whole archive), and
        # the PDFs are cached, so the second run costs nothing.
        pdf = agenda_pdf(body, ev, agenda, session)
        if pdf:
            text = agenda_text(pdf)
            entry["sessions"] = read_sessions(text)
            entry["agenda"] = read_agenda(text)
            entry["agenda_posted_on"] = read_posted_on(text)

        out.append(entry)

    out.sort(key=lambda e: (e["date"], e["id"]), reverse=True)

    # Same cache the upcoming file draws on: an item summarized while a meeting
    # was still ahead keeps its plain-English line now that it is behind us.
    cache = agenda_summaries.load_cache()
    carried = 0
    if cache:
        for entry in out:
            for head in entry["agenda"]:
                for item in head["items"]:
                    got = cache.get(agenda_summaries.item_key(item))
                    if got and got.get("plain"):
                        item["plain"] = got["plain"]
                        carried += 1

    for e in out:
        heads = len(e["agenda"])
        n = sum(len(h["items"]) for h in e["agenda"])
        # Headings with nothing under them is the normal shape of a work-session
        # agenda, not a parse failure — only an empty list means the PDF didn't
        # follow the template and the page will fall back to linking it.
        detail = (f"   {heads} headings, {n} items" if heads
                  else "   (agenda didn't parse — page links the PDF)")
        print(f"  {e['date']} {e['start_label']:>8}  {e['title']}{detail}")
    if carried:
        print(f"  ({carried} items carried a plain-English line over from the cache)")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--body", help="single body id (default: every CivicClerk body)")
    ap.add_argument("--since", default="",
                    help="only meetings on/after this day (default: the whole archive)")
    args = ap.parse_args(argv)

    until = local_today()
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
            entries = build_body(body, since=args.since, until=until, session=session)
        except Exception as exc:
            print(f"  ! calendar fetch failed: {exc}", file=sys.stderr)
            return 1          # a half-built file would silently drop meetings
        if entries:
            manifest[body["id"]] = entries
        else:
            print("  (every meeting on the calendar has minutes or a recording)")

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
    total = sum(len(v) for v in manifest.values())
    print(f"\nwrote {OUT.relative_to(ROOT)}  ({total} meetings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
