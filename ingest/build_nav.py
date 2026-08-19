"""
Write ingest/nav.py's destination list into every place that carries a copy.

There are four, and they drifted apart because each was hand-edited:

  docs/*.html        the <nav class="nav"> block on twelve pages
  docs/404.html      "where to go instead", a second list on the same page
  docs/site.js       the TABBAR array the mobile bottom bar mounts from
  ingest/build_meeting_pages.py
                     imports ingest.nav directly, so it needs nothing from here

Same marker-and-splice arrangement as build_prerender.py: everything between
BEGIN and END belongs to this script, everything outside them is hand-written
and survives untouched. Run --check in CI so a hand-edit that drifts fails
there instead of shipping.

Run:  python -m ingest.build_nav
      python -m ingest.build_nav --check   # exit 1 if any file is stale
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from ingest.nav import BEGIN, END, fallback_list, nav_links, tabbar_js

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

NAV_CONTAINER = re.compile(r'(<nav class="nav" aria-label="Primary">)(.*?)(</nav>)', re.S)
FALLBACK_CONTAINER = re.compile(r'(<ul id="fallback">)(.*?)(</ul>)', re.S)
TABBAR_CONTAINER = re.compile(r'(const TABBAR = \[)(.*?)(\];)', re.S)

# 404.html is served for whatever path was missed, so it cannot use relative
# hrefs. Everything else in docs/ sits at the site root and can.
ROOT_ABSOLUTE = {"404.html"}


def splice(source: str, container: re.Pattern[str], body: str,
           indent: str, comment: tuple[str, str]) -> str:
    """Replace the marked block inside `container`, adding markers if absent."""
    match = container.search(source)
    if not match:
        raise SystemExit(f"couldn't find the container matching {container.pattern}")

    open_tag, inner, close_tag = match.group(1), match.group(2), match.group(3)
    lead, trail = comment

    # Close at whatever column the opening tag sits on, read from the file
    # rather than assumed, so the block doesn't leave the page's indentation a
    # little wrong every time it runs.
    line_start = source.rfind("\n", 0, match.start()) + 1
    closing = source[line_start:match.start()]
    if closing.strip():
        closing = indent[:-2]

    block = (f"\n{indent}{lead}{BEGIN}{trail}\n{body}\n"
             f"{indent}{lead}{END}{trail}\n{closing}")

    start = inner.find(BEGIN)
    if start != -1:
        # Back up over the comment opener so it is replaced along with the rest.
        opener = inner.rfind(lead, 0, start)
        start = opener if opener != -1 else start
        stop = inner.find(END, start)
        if stop == -1:
            raise SystemExit("found a BEGIN marker with no matching END")
        stop = inner.find(trail, stop) + len(trail) if trail else stop + len(END)
        new_inner = inner[:start].rstrip("\n ") + block + inner[stop:].lstrip("\n ")
    else:
        new_inner = block

    return source[:match.start()] + open_tag + new_inner + close_tag + source[match.end():]


HTML = ("<!-- ", " -->")
JS = ("// ", "")


def targets() -> dict[Path, list[tuple[re.Pattern[str], str, str, tuple[str, str]]]]:
    """Every file this writes, and the blocks it writes into each."""
    out: dict[Path, list] = {}

    for page in sorted(DOCS.glob("*.html")):
        root = page.name in ROOT_ABSOLUTE
        jobs = [(NAV_CONTAINER, nav_links(root=root), "        ", HTML)]
        if page.name == "404.html":
            jobs.append((FALLBACK_CONTAINER, fallback_list(), "          ", HTML))
        out[page] = jobs

    out[DOCS / "site.js"] = [(TABBAR_CONTAINER, tabbar_js(), "    ", JS)]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--check", action="store_true",
                    help="don't write; exit 1 if any file would change")
    args = ap.parse_args(argv)

    stale = False
    for path, jobs in targets().items():
        if not path.exists():
            print(f"  {path.name}: missing, skipped", file=sys.stderr)
            continue

        source = path.read_text(encoding="utf-8")
        updated = source
        for container, body, indent, comment in jobs:
            updated = splice(updated, container, body, indent, comment)

        if updated == source:
            print(f"{path.name} already current")
            continue
        if args.check:
            print(f"{path.name} is stale", file=sys.stderr)
            stale = True
            continue
        path.write_text(updated, encoding="utf-8")
        print(f"wrote {path.name} ({len(updated) - len(source):+,d} bytes)")

    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
