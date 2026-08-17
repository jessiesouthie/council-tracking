"""
Publish the site's URL list for crawlers.

Every page here is a static HTML shell that fills itself in from JSON at run
time, so a crawler that only follows <a href> sees the shells but has no way to
know that member.html is really eight pages, one per councilmember. This writes
those query-string URLs out explicitly.

Writes docs/sitemap.xml and docs/robots.txt. Both are generated files — edit
this script, not them.

What goes in:
  /                             index.html, canonical without the filename
  /<page>.html                  the seven other top-level pages
  /member.html?id=<member>      one per member, per body
  /<page>.html?body=<body>      meetings, members and motions for the
                                non-default bodies (city council is the
                                default and needs no param)
  /meetings.html?id=<meeting>   only with --meetings; see below

The per-meeting URLs are real pages with real content (summary, votes,
transcript) but there are hundreds of them and they are not linked from a
crawlable index, so they're opt-in:

    python -m ingest.build_sitemap --meetings

<lastmod> is the later of the page's last git commit and the generated_at of
whatever data files that page reads — the shell and its data both change what a
visitor sees, and the data changes far more often.

Run:  python -m ingest.build_sitemap
      python -m ingest.build_sitemap --meetings
      python -m ingest.build_sitemap --base https://example.com
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from . import bodies

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = DOCS / "sitemap.xml"
ROBOTS = DOCS / "robots.txt"

# The custom domain GitHub Pages serves from. Falls back to the known host if
# the file ever goes missing, so a bad checkout can't publish relative URLs.
CNAME = DOCS / "CNAME"
DEFAULT_HOST = "civicrollcall.com"

# Top-level pages, with the data files each one reads. The shells are listed in
# rough order of what a resident wants first, which is also the priority order.
# `body_scoped` names the key a body's data must fill for that page to be worth
# listing — the community services board has no members, so its members page is
# a blank table nobody should be sent to. The pages without the key (budget,
# tax, projections) are city-council material with no body switcher at all.
PAGES: list[dict] = [
    {"path": "", "file": "index.html", "priority": "1.0",
     "changefreq": "daily", "data": ["data.upcoming.json"]},
    {"path": "meetings.html", "priority": "0.9", "changefreq": "weekly",
     "data": ["data.json", "data.upcoming.json"], "body_scoped": "meetings"},
    {"path": "members.html", "priority": "0.9", "changefreq": "monthly",
     "data": ["data.json"], "body_scoped": "members"},
    {"path": "tax.html", "priority": "0.8", "changefreq": "monthly",
     "data": ["data.tax.json", "data.tax-bill.json", "data.tax-case.json",
              "data.tax-county.json", "data.tax-state.json"]},
    {"path": "budget.html", "priority": "0.8", "changefreq": "monthly",
     "data": ["data.budget.json", "data.budget-book.json",
              "data.budget-next.json"]},
    {"path": "projections.html", "priority": "0.8", "changefreq": "monthly",
     "data": ["data.projections.json"]},
    {"path": "motions.html", "priority": "0.7", "changefreq": "weekly",
     "data": ["data.json"], "body_scoped": "motions"},
    {"path": "definitions.html", "priority": "0.7", "changefreq": "monthly",
     "data": []},
    # Low priority and rarely changing, but it has to be listed: it's the page
    # that says who publishes this and where every figure came from, and a site
    # making tax claims about a named city needs that reachable.
    {"path": "about.html", "priority": "0.4", "changefreq": "yearly",
     "data": []},
]

MEMBER_PRIORITY = "0.6"
MEETING_PRIORITY = "0.5"
# A non-default body's pages carry the same content at a lower priority than
# the council's own, since the council is what most people arrive looking for.
OTHER_BODY_DROP = 0.2


def site_base(override: str | None = None) -> str:
    """Origin the sitemap's absolute URLs hang off, no trailing slash."""
    if override:
        return override.rstrip("/")
    host = DEFAULT_HOST
    if CNAME.exists():
        first = CNAME.read_text(encoding="utf-8").strip().splitlines()
        if first and first[0].strip():
            host = first[0].strip()
    return f"https://{host}"


def _parse_iso(text: str) -> datetime | None:
    """Read a git or generated_at timestamp into an aware datetime."""
    try:
        stamp = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def git_committed_at(path: Path) -> datetime | None:
    """Last commit date for one file, or None if git can't say."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", str(path)],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return _parse_iso(out.stdout)


def data_generated_at(name: str) -> datetime | None:
    """The generated_at a build stamped into one of the docs/*.json files."""
    path = DOCS / name
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            stamp = json.load(fh).get("generated_at")
    except (OSError, ValueError):
        return None
    return _parse_iso(stamp) if isinstance(stamp, str) else None


def file_changed_at(path: Path, data_files: list[str],
                    cache: dict[str, datetime | None]) -> datetime:
    """Newest of a page's own commit and the data it renders from."""
    stamps = [git_committed_at(path)]
    for name in data_files:
        if name not in cache:
            cache[name] = data_generated_at(name)
        stamps.append(cache[name])
    known = [s for s in stamps if s]
    if known:
        return max(known)
    # Nothing to go on: an uncommitted page with no data behind it. Its mtime
    # is at least honest about this checkout.
    if path.exists():
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return datetime.now(timezone.utc)


def lower_priority(priority: str, drop: float) -> str:
    return f"{max(0.1, float(priority) - drop):.1f}"


def load_body_data(body: dict) -> dict:
    """The published JSON for one body, or {} if it was never built."""
    path = DOCS / (body.get("data_file") or f"data.{body['id']}.json")
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def body_registry() -> tuple[list[dict], str]:
    """Bodies as the site sees them, plus the default body's id."""
    listed = []
    default_id = "city-council"
    for body in bodies.all_bodies():
        data_file = ("data.json" if body.get("default")
                     else f"data.{body['id']}.json")
        # bodies.py owns the real path; docs/bodies.json is what the site reads.
        try:
            data_file = Path(bodies.data_file(body)).name
        except (AttributeError, KeyError, TypeError):
            pass
        listed.append({"id": body["id"], "label": body.get("label", body["id"]),
                       "data_file": data_file,
                       "default": bool(body.get("default"))})
        if body.get("default"):
            default_id = body["id"]
    return listed, default_id


def build_urls(base: str, include_meetings: bool = False) -> list[dict]:
    registry, default_id = body_registry()
    cache: dict[str, datetime | None] = {}
    urls: list[dict] = []

    def add(loc: str, changed: datetime, changefreq: str, priority: str) -> None:
        urls.append({"loc": loc, "lastmod": changed.astimezone(timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                     "changefreq": changefreq, "priority": priority})

    # The council's own pages, at the bare URLs the nav links to.
    for page in PAGES:
        shell = DOCS / (page.get("file") or page["path"])
        changed = file_changed_at(shell, page["data"], cache)
        add(f"{base}/{page['path']}", changed,
            page["changefreq"], page["priority"])

    member_shell = DOCS / "member.html"
    member_committed = git_committed_at(member_shell)

    for body in registry:
        data = load_body_data(body)
        if not data:
            print(f"  {body['id']}: no data file, skipped", file=sys.stderr)
            continue
        is_default = body["id"] == default_id
        drop = 0.0 if is_default else OTHER_BODY_DROP
        stamped = _parse_iso(data.get("generated_at") or "")
        body_changed = max([s for s in (stamped, member_committed) if s]
                           or [datetime.now(timezone.utc)])
        suffix = "" if is_default else f"&body={body['id']}"

        # The pages that re-render per body. The default body already has these
        # at their bare URLs above, so only the others need a ?body= copy, and
        # only where this body actually has that kind of record.
        if not is_default:
            for page in PAGES:
                key = page.get("body_scoped")
                if not key:
                    continue
                if not (data.get(key) or []):
                    print(f"  {body['id']}: no {key}, skipped {page['path']}",
                          file=sys.stderr)
                    continue
                shell = DOCS / (page.get("file") or page["path"])
                changed = file_changed_at(shell, [body["data_file"]], cache)
                add(f"{base}/{page['path']}?body={body['id']}", changed,
                    page["changefreq"], lower_priority(page["priority"], drop))

        for member in data.get("members") or []:
            member_id = member.get("id")
            if not member_id:
                continue
            add(f"{base}/member.html?id={member_id}{suffix}", body_changed,
                "monthly", lower_priority(MEMBER_PRIORITY, drop))

        if include_meetings:
            for meeting in data.get("meetings") or []:
                meeting_id = meeting.get("id")
                if meeting_id is None:
                    continue
                # A meeting's record stops changing once it's minuted, so its
                # own date beats the build stamp as a lastmod.
                held = _parse_iso(f"{meeting.get('date', '')}T00:00:00+00:00")
                add(f"{base}/meetings.html?id={meeting_id}{suffix}",
                    held or body_changed, "yearly",
                    lower_priority(MEETING_PRIORITY, drop))

    return urls


def render_sitemap(urls: list[dict]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url in urls:
        lines.append("  <url>")
        lines.append(f"    <loc>{escape(url['loc'])}</loc>")
        lines.append(f"    <lastmod>{url['lastmod']}</lastmod>")
        lines.append(f"    <changefreq>{url['changefreq']}</changefreq>")
        lines.append(f"    <priority>{url['priority']}</priority>")
        lines.append("  </url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def render_robots(base: str) -> str:
    return ("# Generated by ingest/build_sitemap.py — do not edit by hand.\n"
            "User-agent: *\n"
            "Allow: /\n"
            "\n"
            f"Sitemap: {base}/sitemap.xml\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--base", help="site origin (default: read docs/CNAME)")
    ap.add_argument("--meetings", action="store_true",
                    help="include one URL per meeting (hundreds of them)")
    ap.add_argument("--no-robots", action="store_true",
                    help="leave docs/robots.txt alone")
    args = ap.parse_args(argv)

    base = site_base(args.base)
    urls = build_urls(base, include_meetings=args.meetings)
    if not urls:
        print("no URLs built; leaving the existing files alone", file=sys.stderr)
        return 1

    OUT.write_text(render_sitemap(urls), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(urls)} URLs)")
    if not args.no_robots:
        ROBOTS.write_text(render_robots(base), encoding="utf-8")
        print(f"wrote {ROBOTS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
