"""
Crawl the CivicClerk API and download every approved Minutes PDF that we don't
already have, for one body or all of them.

  python -m ingest.crawl                       # default body (City Council), incremental
  python -m ingest.crawl --body planning-commission
  python -m ingest.crawl --all-bodies          # every CivicClerk-sourced body
  python -m ingest.crawl --full                # re-download even if present
  python -m ingest.crawl --since 2024-01-01

Files land at each body's raw dir, e.g.:
  data/raw/<YYYY-MM-DD>__<eventId>.pdf                       (City Council, legacy flat)
  data/raw/planning-commission/<YYYY-MM-DD>__<eventId>.pdf

Manual-source bodies (e.g. Community Services Board) have no CivicClerk category
and are skipped here — their PDFs are dropped into the raw dir by hand.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import bodies, civicclerk

ROOT = Path(__file__).resolve().parent.parent


def raw_path_for(raw_dir: Path, event_date: str, event_id: int) -> Path:
    iso_day = event_date[:10]
    return raw_dir / f"{iso_day}__{event_id}.pdf"


def parse_iso_day(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)


def crawl_body(body: dict, *, since, full: bool, limit: int | None,
               session) -> tuple[int, list[tuple[int, str]]]:
    """Download a single body's approved minutes. Returns (downloaded, failures)."""
    raw_dir = bodies.raw_dir(body)
    seen = downloaded = skipped_present = skipped_nofile = skipped_old = 0
    failures: list[tuple[int, str]] = []

    print(f"\n== {body['label']} ({body['id']}) ==", flush=True)
    for ev in civicclerk.list_council_events(session=session, category=body["category"]):
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

        dest = raw_path_for(raw_dir, ed, eid)
        if dest.exists() and not full:
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

        if limit is not None and downloaded >= limit:
            break

    print(
        f"  seen={seen} downloaded={downloaded} "
        f"skipped_present={skipped_present} skipped_no_minutes={skipped_nofile} "
        f"skipped_old={skipped_old} failures={len(failures)}"
    )
    return downloaded, failures


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--body", default=None,
                   help="body id to crawl (default: the default body)")
    p.add_argument("--all-bodies", action="store_true",
                   help="crawl every CivicClerk-sourced body")
    p.add_argument("--full", action="store_true",
                   help="re-download files that already exist locally")
    p.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                   help="ignore events before this date")
    p.add_argument("--limit", type=int, default=None,
                   help="stop after N downloads per body (useful for testing)")
    args = p.parse_args()

    if args.all_bodies:
        targets = bodies.all_bodies()
    elif args.body:
        targets = [bodies.get_body(args.body)]
    else:
        targets = [bodies.default_body()]

    since = parse_iso_day(args.since)
    session = civicclerk._session()

    total_downloaded = 0
    all_failures: list[tuple[int, str]] = []
    for body in targets:
        if body.get("source") != "civicclerk":
            print(f"\n== {body['label']} ({body['id']}) == skipped "
                  f"(source={body.get('source')!r}; drop PDFs into "
                  f"{body['raw_dir']}/ by hand)", flush=True)
            continue
        n, failures = crawl_body(body, since=since, full=args.full,
                                 limit=args.limit, session=session)
        total_downloaded += n
        all_failures.extend(failures)

    print(f"\nDone. total downloaded={total_downloaded} failures={len(all_failures)}")
    for eid, msg in all_failures:
        print(f"  ! event {eid}: {msg}", file=sys.stderr)
    return 0 if not all_failures else 1


if __name__ == "__main__":
    sys.exit(main())
