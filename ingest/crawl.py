"""
Crawl the CivicClerk API and download every approved Council Minutes PDF
that we don't already have in data/raw/.

  python -m ingest.crawl                # incremental (skip files already present)
  python -m ingest.crawl --full         # re-download even if present
  python -m ingest.crawl --since 2024-01-01

Files land at:
  data/raw/<YYYY-MM-DD>__<eventId>.pdf
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import civicclerk

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"


def raw_path_for(event_date: str, event_id: int) -> Path:
    iso_day = event_date[:10]
    return RAW_DIR / f"{iso_day}__{event_id}.pdf"


def parse_iso_day(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--full", action="store_true",
                   help="re-download files that already exist locally")
    p.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                   help="ignore events before this date")
    p.add_argument("--limit", type=int, default=None,
                   help="stop after N downloads (useful for testing)")
    args = p.parse_args()

    since = parse_iso_day(args.since)
    session = civicclerk._session()

    seen = downloaded = skipped_present = skipped_nofile = skipped_old = 0
    failures: list[tuple[int, str]] = []

    for ev in civicclerk.list_council_events(session=session):
        seen += 1
        eid = ev["id"]
        ed = ev.get("eventDate") or ""
        ev_dt = parse_iso_day(ed)
        if since and ev_dt and ev_dt < since:
            skipped_old += 1
            continue

        # publishedFiles ships with the Events listing — no second call needed.
        f = civicclerk.find_approved_minutes(ev)
        if not f or not f.get("fileId"):
            skipped_nofile += 1
            continue

        dest = raw_path_for(ed, eid)
        if dest.exists() and not args.full:
            skipped_present += 1
            continue

        try:
            n = civicclerk.download_file(f["fileId"], dest, session=session)
            downloaded += 1
            print(f"  + {dest.relative_to(ROOT)}  ({n:,} bytes)  «{f.get('name','?')}»",
                  flush=True)
        except Exception as exc:
            failures.append((eid, f"download failed: {exc}"))
            continue

        if args.limit is not None and downloaded >= args.limit:
            break

    print(
        f"\nDone. seen={seen} downloaded={downloaded} "
        f"skipped_present={skipped_present} skipped_no_minutes={skipped_nofile} "
        f"skipped_old={skipped_old} failures={len(failures)}"
    )
    for eid, msg in failures:
        print(f"  ! event {eid}: {msg}", file=sys.stderr)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
