"""
Find recent meetings whose recording is posted but whose transcript or summary
still isn't — the work list for the nightly "publish the day after" automation.

A meeting is pending when all of these hold:
  * it belongs to a CivicClerk-sourced body we transcribe
  * it already happened, within the last --days (default 6)
  * the portal has a playable recording (hasMedia + an mp4/stream path)
  * data/transcripts/<body>/<YYYY-MM-DD>__<id>.summary.md is missing

The summary — not the .txt — is what marks a meeting done. If transcription
succeeded last night but the summary step failed, the meeting stays pending and
the next run picks it up; scripts/transcribe_meeting.sh skips the (expensive,
already-finished) transcription and only fills in the summary.

The window matters: the city publishes its meeting calendar months ahead, so
without it a first run would try to transcribe the entire archive. Meetings
that should never be transcribed go in data/transcripts/skip.json:

    { "city-council": [755] }

Run:
  python -m ingest.pending_transcripts                      # tsv, city council
  python -m ingest.pending_transcripts --days 14 --json
  python -m ingest.pending_transcripts --body planning-commission
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import bodies, civicclerk

ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPTS = ROOT / "data" / "transcripts"
SKIPFILE = TRANSCRIPTS / "skip.json"

# Bodies the automation transcribes unless told otherwise. Council only: the
# summary prompt in scripts/transcribe_meeting.sh carries the council roster,
# and every other body would add its own per-meeting transcription cost.
DEFAULT_BODIES = ["city-council"]

# How far back to look. Recordings usually post the same night, but a late
# upload (the 2026-07-21 meeting posted the following afternoon) has to still
# be caught by a later run.
DEFAULT_DAYS = 6


def _load_skips() -> dict[str, set[int]]:
    if not SKIPFILE.exists():
        return {}
    try:
        raw = json.loads(SKIPFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"warning: ignoring unreadable {SKIPFILE.name}: {exc}", file=sys.stderr)
        return {}
    return {bid: {int(i) for i in ids} for bid, ids in raw.items()}


def _has_recording(ev: dict) -> bool:
    """True when the portal exposes something playable for this event.

    Newer events carry a storage-relative mediaSourcePathMp4 that only resolves
    through the media endpoint, so presence — not shape — is what we test here;
    transcribe_meeting.sh does the resolving.
    """
    if not ev.get("hasMedia"):
        return False
    return bool(ev.get("mediaSourcePathMp4") or ev.get("mediaStreamPath"))


def pending(
    body_ids: list[str] | None = None,
    *,
    days: int = DEFAULT_DAYS,
    today: date | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Return pending meetings, oldest first, at most `limit` of them."""
    today = today or datetime.now(timezone.utc).date()
    floor = today - timedelta(days=days)
    skips = _load_skips()
    session = civicclerk._session()
    out: list[dict] = []

    for bid in body_ids or DEFAULT_BODIES:
        body = bodies.get_body(bid)
        if body.get("source") != "civicclerk":
            print(f"  {bid}: not a CivicClerk body — skipped", file=sys.stderr)
            continue
        skip_ids = skips.get(bid, set())

        for ev in civicclerk.list_council_events(session=session, category=body["category"]):
            day = (ev.get("eventDate") or "")[:10]
            if not day:
                continue
            ev_date = datetime.strptime(day, "%Y-%m-%d").date()
            # Events come back newest first and the calendar runs months into
            # the future, so keep walking until we're past the window.
            if ev_date > today:
                continue
            if ev_date < floor:
                break
            if ev.get("isDeleted") or ev.get("isPublished") != "Published":
                continue
            eid = int(ev["id"])
            if eid in skip_ids:
                continue
            if not _has_recording(ev):
                continue

            stem = f"{day}__{eid}"
            src = TRANSCRIPTS / bid / stem
            if src.with_suffix(".summary.md").exists():
                continue

            out.append({
                "body": bid,
                "id": eid,
                "date": day,
                "stem": stem,
                "name": ev.get("eventName") or body["label"],
                # What's left to do — the script figures this out too, but it
                # makes the run log say why a meeting is in the list.
                "need": "summary" if src.with_suffix(".txt").exists() else "transcript",
            })

    out.sort(key=lambda r: (r["date"], r["id"]))
    return out[:limit] if limit is not None else out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--body", action="append", dest="body_ids",
                    help="body id to check (repeatable; default: city-council)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"how many days back to look (default {DEFAULT_DAYS})")
    ap.add_argument("--limit", type=int, default=None,
                    help="return at most N meetings (oldest first)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of TSV")
    args = ap.parse_args()

    rows = pending(args.body_ids, days=args.days, limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    for r in rows:
        print(f"{r['body']}\t{r['id']}\t{r['date']}\t{r['need']}\t{r['name']}")
    print(f"{len(rows)} meeting(s) pending", file=sys.stderr)


if __name__ == "__main__":
    main()
