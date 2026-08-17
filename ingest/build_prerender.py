"""
Put the record into the HTML, so a crawler that runs no JavaScript can read it.

Every page but definitions.html is a shell that fetches data.json and builds
itself. Measured with the scripts stripped out, that leaves between 137 and 484
characters of body text per page — a nav bar and a footer credit. Google renders
JavaScript on a second pass and eventually sees the rest. Bing barely does, and
GPTBot, PerplexityBot and ClaudeBot don't render at all, so to an AI answer
engine this site currently has no content on it.

So: write the same record into the shells as plain HTML at build time. The
containers this fills are the exact ones the page's own script assigns
innerHTML to a moment later, which makes this progressive enhancement rather
than a second rendering path to keep in step — the static copy is what a reader
sees for the few hundred milliseconds data.json is in flight, and the scripted
copy replaces it wholesale. There is no arrangement where both are visible, and
no way for the two to disagree for longer than one paint.

What gets written, and why it stops where it does:

  meetings.html  every meeting, each a real <a> to the static page
                 ingest/build_meeting_pages.py writes for it. This is the only
                 crawlable path to those several hundred pages — the scripted
                 card links to them too, but nothing follows that before render.
  members.html   the sitting roster as table rows, linked to each member page.
  motions.html   the most recent MOTION_LIMIT motions. There are over a thousand
                 and they're addressed by fragment on this one page, so listing
                 all of them would add page weight without adding a single URL.

Run:  python -m ingest.build_prerender
      python -m ingest.build_prerender --check   # exit 1 if any page is stale
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DATA = DOCS / "data.json"

BEGIN = "<!-- BEGIN generated:prerender (ingest/build_prerender.py) -->"
END = "<!-- END generated:prerender -->"

# Motions are fragments of motions.html, not pages of their own, so past a point
# each extra one is weight with no crawl benefit. A hundred covers roughly the
# last two years of business, which is what anyone arrives searching for.
MOTION_LIMIT = 100
# Per meeting, in the list. One is enough to say what the night was about, and
# the list runs to 200 meetings — a second headline each cost 60KB of HTML.
HEADLINES_PER_MEETING = 1

# Only `headline` and `summary` are ever published here. Both are written from
# the minutes; `item_title` and `motion` are the OCR of the PDF itself, and 302
# of the 1,119 item_titles carry scanning damage — "Eagle Mountain Citg",
# "an ordínance amendÍng the ... MunÍcÍpal Code". That damage is survivable in a
# table cell a reader can see through to the source, but this text is going into
# the crawlable HTML as the site's own prose, so a motion with no clean headline
# is left out of the static copy rather than published misspelt. It still renders
# from data.json like everything else.

MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")


def esc(text: object) -> str:
    return html.escape(str(text or ""), quote=True)


def fmt_date(iso: str) -> str:
    """2026-07-21 -> 21 July 2026. Matches CT.fmtDate closely enough to read."""
    try:
        d = date.fromisoformat((iso or "")[:10])
    except ValueError:
        return esc(iso)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def load_data() -> dict:
    with DATA.open(encoding="utf-8") as fh:
        return json.load(fh)


def is_current_member(member: dict, today: str) -> bool:
    """Mirrors CT.isCurrentMember: no end date, or one that hasn't passed."""
    end = member.get("tenure_end")
    return not end or end >= today


# ---------------------------------------------------------------------------
# Page bodies
# ---------------------------------------------------------------------------

def render_meetings(data: dict, body_id: str = "city-council") -> str:
    meetings = sorted(data.get("meetings") or [],
                      key=lambda m: m.get("date") or "", reverse=True)
    if not meetings:
        return ""

    by_meeting: dict[int, list[dict]] = {}
    for motion in data.get("motions") or []:
        by_meeting.setdefault(motion.get("meeting_id"), []).append(motion)

    newest = fmt_date(meetings[0].get("date", ""))
    oldest = fmt_date(meetings[-1].get("date", ""))

    rows = []
    for meeting in meetings:
        mid = meeting.get("id")
        motions = by_meeting.get(mid) or []
        # Decisions that reach residents first, the same ordering the card uses.
        motions = sorted(motions,
                         key=lambda m: 0 if m.get("significance") == "notable" else 1)
        heads = [m["headline"] for m in motions
                 if m.get("headline")][:HEADLINES_PER_MEETING]

        counts = (f"{meeting.get('motion_count', 0)} motions · "
                  f"{meeting.get('ord_count', 0)} ordinance lines · "
                  f"{meeting.get('res_count', 0)} resolution lines")
        summary = f"<span class=\"pre-heads\">{esc('; '.join(heads))}</span>" if heads else ""
        # The meeting's own page, not meetings.html?id= — the static page is the
        # canonical URL for a meeting, and this list is the crawl path to it.
        href = f'/meetings/{meeting.get("date")}-{body_id}-{mid}.html'
        rows.append(
            f'        <li><a href="{esc(href)}">{fmt_date(meeting.get("date", ""))}</a>'
            f' <span class="muted">{esc(counts)}</span>{summary}</li>'
        )

    return (
        f'      <h1 class="page-title">Meetings</h1>\n'
        f'      <p class="page-sub">{len(meetings)} Eagle Mountain City Council meetings on file, '
        f'from {esc(oldest)} to {esc(newest)}. Each one lists the motions decided that night '
        f'and how every member voted.</p>\n'
        f'      <ul class="pre-list">\n' + "\n".join(rows) + "\n      </ul>"
    )


def render_members(data: dict, today: str) -> str:
    members = [m for m in (data.get("members") or [])
               if m.get("role") in ("Councilmember", "Mayor")
               and is_current_member(m, today)]
    if not members:
        return ""

    by_member = (data.get("stats") or {}).get("by_member") or {}
    rows = []
    for member in members:
        s = by_member.get(member.get("id")) or {}
        total = s.get("total", 0)
        yes = s.get("yes", 0)
        opposed = s.get("no", 0) + s.get("nay", 0)
        other = s.get("abstain", 0) + s.get("excused", 0) + s.get("absent", 0)
        rate = f"{round((s.get('yes_rate') or 0) * 100)}%"
        start = fmt_date(member.get("tenure_start", ""))
        end = fmt_date(member["tenure_end"]) if member.get("tenure_end") else "present"
        href = f'member.html?id={esc(member.get("id"))}'
        rows.append(
            f"              <tr>\n"
            f'                <td data-role="title" data-label="Member">'
            f'<a href="{href}"><strong>{esc(member.get("name"))}</strong></a></td>\n'
            f'                <td data-label="Role">{esc(member.get("role"))}</td>\n'
            f'                <td data-label="Tenure" class="muted nowrap">{esc(start)}&ndash; {esc(end)}</td>\n'
            f'                <td data-label="Total votes" class="text-right mono">{total}</td>\n'
            f'                <td data-label="Yes" class="text-right mono">{yes}</td>\n'
            f'                <td data-label="No" class="text-right mono">{opposed}</td>\n'
            f'                <td data-label="Abstain" class="text-right mono muted">{other}</td>\n'
            f'                <td data-label="Yes rate" class="text-right mono">{rate}</td>\n'
            f"              </tr>"
        )
    return "\n".join(rows)


def render_motions(data: dict) -> str:
    written = [m for m in (data.get("motions") or []) if m.get("headline")]
    motions = sorted(written,
                     key=lambda m: (m.get("date") or "", m.get("id") or 0),
                     reverse=True)[:MOTION_LIMIT]
    if not motions:
        return ""

    items = []
    for motion in motions:
        title = motion["headline"]
        detail = motion.get("summary") or ""
        impact = motion.get("impact") or ""
        items.append(
            f'        <li id="m{esc(motion.get("id"))}">\n'
            f'          <h3 class="pre-motion-title">{esc(title)}</h3>\n'
            f'          <p class="muted pre-motion-meta">{fmt_date(motion.get("date", ""))}'
            f' · {esc(motion.get("outcome") or "outcome not recorded")}</p>\n'
            f"          <p>{esc(detail)}</p>\n"
            + (f'          <p class="muted">{esc(impact)}</p>\n' if impact else "")
            + "        </li>"
        )

    total = len(data.get("motions") or [])
    return (
        f'      <p class="page-sub">The {len(motions)} most recent of {total:,} Eagle Mountain '
        f'City Council motions on file. Use the search and filters above to reach the rest.</p>\n'
        f'      <ol class="pre-list">\n' + "\n".join(items) + "\n      </ol>"
    )


# ---------------------------------------------------------------------------
# Splicing
# ---------------------------------------------------------------------------

def splice(source: str, container: re.Pattern[str], body: str, indent: str) -> str:
    """Replace the marked block inside `container`, adding markers if absent."""
    match = container.search(source)
    if not match:
        raise SystemExit(f"couldn't find the container matching {container.pattern}")

    open_tag, inner, close_tag = match.group(1), match.group(2), match.group(3)
    # Close at whatever column the opening tag sits on, read from the file rather
    # than assumed, so the generated block doesn't leave the page's indentation
    # a little wrong every time it runs.
    line_start = source.rfind("\n", 0, match.start()) + 1
    closing = source[line_start:match.start()]
    if closing.strip():
        closing = indent[:-2]

    block = f"\n{indent}{BEGIN}\n{body}\n{indent}{END}\n{closing}"

    # Anything already between the markers is ours to replace; anything outside
    # them is hand-written and must survive untouched.
    start = inner.find(BEGIN)
    if start != -1:
        stop = inner.find(END, start)
        if stop == -1:
            raise SystemExit("found a BEGIN marker with no matching END")
        keep_before = inner[:start].rstrip("\n ")
        keep_after = inner[stop + len(END):].lstrip("\n ")
        new_inner = keep_before + block + keep_after
    else:
        new_inner = inner.rstrip() + block if inner.strip() else block

    return source[:match.start()] + open_tag + new_inner + close_tag + source[match.end():]


# (open tag)(inner)(close tag) for each container the pages hand to innerHTML.
CONTAINERS = {
    "meetings.html": re.compile(r'(<div id="content">)(.*?)(</div>)', re.S),
    "members.html": re.compile(r'(<tbody id="rows">)(.*?)(</tbody>)', re.S),
    "motions.html": re.compile(r'(<div id="rows">)(.*?)(</div>)', re.S),
}

INDENTS = {"meetings.html": "      ", "members.html": "            ",
           "motions.html": "      "}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--check", action="store_true",
                    help="don't write; exit 1 if any page would change")
    args = ap.parse_args(argv)

    if not DATA.exists():
        print("docs/data.json is missing; run the dataset build first",
              file=sys.stderr)
        return 1

    data = load_data()
    today = (data.get("generated_at") or "")[:10] or date.today().isoformat()

    bodies = {
        "meetings.html": render_meetings(data),
        "members.html": render_members(data, today),
        "motions.html": render_motions(data),
    }

    stale = False
    for name, body in bodies.items():
        path = DOCS / name
        if not body:
            print(f"  {name}: nothing to write, skipped", file=sys.stderr)
            continue
        if not path.exists():
            print(f"  {name}: missing, skipped", file=sys.stderr)
            continue

        source = path.read_text(encoding="utf-8")
        updated = splice(source, CONTAINERS[name], body, INDENTS[name])
        if updated == source:
            print(f"{name} already current")
            continue
        if args.check:
            print(f"{name} is stale", file=sys.stderr)
            stale = True
            continue
        path.write_text(updated, encoding="utf-8")
        added = len(updated) - len(source)
        print(f"wrote {name} ({added:+,d} bytes)")

    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
