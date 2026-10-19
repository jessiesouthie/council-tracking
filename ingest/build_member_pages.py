"""
Give every member a page of their own.

A member's record used to live behind member.html?id=<member>, one shell that
fetched the whole dataset (2.7 MB) and built the page in the browser. To
anything that reads the HTML without running the script, which is Bing and
every AI crawler and Google's first pass, that was twenty-five URLs with the
same title, the same description, no canonical and no content. A search for a
councilmember by name is the highest-intent query this site gets, and the page
that should answer it was a blank template.

So each member gets a real file:

    /members/<body id>/<member id>.html

made from docs/member.html itself. The template's script still runs and still
replaces #content with the full interactive version, so there is one renderer
to keep in step, not two. What this adds is everything the script used to add
too late: the member pinned on <html data-member>, the body pinned on
<html data-body> (see currentBody() in site.js), the title, description,
canonical and Person schema in the head, and the record itself written into
#content as plain HTML.

member.html?id= keeps working. It now finds the member and moves the reader
here with location.replace, and is noindex itself.

The static record mirrors the render in member.html: change one, change the
other. Like build_prerender.py, it publishes only the clean fields. A motion
with no written headline shows as "Motion as recorded" rather than as the OCR
of the minutes, which the script still shows once it runs.

Run:  python -m ingest.build_member_pages
      python -m ingest.build_member_pages --check    # exit 1 if any page stale
      python -m ingest.build_member_pages --prune    # delete orphaned pages
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date
from pathlib import Path

from .build_meeting_pages import body_registry, load_json, site_base

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
TEMPLATE = DOCS / "member.html"
OUT_DIR = DOCS / "members"

MONTHS_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
                "Oct", "Nov", "Dec")

# How many votes the page lists before the "Show more" button; SHOW in member.html.
SHOW = 15
VOTED = {"yes": "Voted yes", "aye": "Voted yes", "no": "Voted no", "nay": "Voted no",
         "excused": "Excused", "absent": "Absent", "abstain": "Abstained"}


def esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def fmt_date(iso: str) -> str:
    """CT.fmtDate as an en-US browser prints it: "Oct 8, 2026"."""
    try:
        d = date.fromisoformat((iso or "")[:10])
    except ValueError:
        return esc(iso)
    return f"{MONTHS_SHORT[d.month - 1]} {d.day}, {d.year}"


def initials(name: str) -> str:
    parts = [p for p in str(name or "").split() if p]
    first = parts[0][0] if parts else ""
    last = parts[-1][0] if parts else ""
    return (first + last).upper() or "?"


def is_current(member: dict, today: str) -> bool:
    end = member.get("tenure_end")
    return not end or end >= today


# ---------------------------------------------------------------------------
# Ports of the small helpers in site.js that the member page leans on
# ---------------------------------------------------------------------------

def classify_outcome(outcome: str) -> str:
    o = (outcome or "").lower()
    if re.search(r"passed|approved|carried", o):
        return "passed"
    if re.search(r"fail|denied|not pass", o):
        return "failed"
    return "other"


def outcome_pill(outcome: str) -> str:
    raw = (outcome or "").strip()
    if not raw or re.search(r"not found", raw, re.I):
        return ('<span class="pill other" title="The minutes don’t record a result '
                'for this motion">Not recorded</span>')
    cls = classify_outcome(raw)
    if cls != "other":
        return f'<span class="pill {cls}">{cls}</span>'
    word = raw.rstrip(".")
    return f'<span class="pill other">{esc(word[:1].upper() + word[1:])}</span>'


def term_lines(member: dict) -> str:
    """renderTerms() in member.html."""
    terms = member.get("terms") or []
    if not terms:
        return f'<p class="mp-term">{esc(member.get("role") or "")}</p>'

    def fmt(t: dict) -> str:
        end = f" – {fmt_date(t['end'])}" if t.get("end") else " – present"
        return f"{esc(t.get('role'))} &middot; {fmt_date(t.get('start'))}{end}"

    head, rest = terms[0], terms[1:]
    prior = (f'<p class="mp-prior">Previously: {"; ".join(fmt(t) for t in rest)}</p>'
             if rest else "")
    return f'<p class="mp-term">{fmt(head)}</p>{prior}'


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def member_url(base: str, body_id: str, member_id: str) -> str:
    return f"{base}/members/{body_id}/{member_id}.html"


def member_href(body_id: str, member_id: str) -> str:
    """CT.memberHref, which has to agree with this byte for byte."""
    return f"/members/{body_id}/{member_id}.html"


def render_record(body: dict, data: dict, member: dict, today: str) -> str:
    """The #content markup, as member.html's script renders it."""
    mid = member["id"]
    label = data.get("body_label") or body["label"]
    noun = label.strip().split()[-1].lower()
    by_member = (data.get("stats") or {}).get("by_member") or {}
    s = {"total": 0, "yes": 0, "no": 0, "nay": 0, "abstain": 0, "excused": 0,
         "absent": 0, "yes_rate": 0, "by_tag": {}, **(by_member.get(mid) or {})}
    opposed = s["no"] + (s.get("nay") or 0)
    not_voting = s["abstain"] + s["excused"] + s["absent"]
    total = s["total"]

    def pct(x: int) -> str:
        return f"{x / total * 100:.2f}" if total else "0"

    agreement = (data.get("stats") or {}).get("agreement") or {}
    agreement_n = (data.get("stats") or {}).get("agreement_n") or {}
    ag = []
    for other in data.get("members") or []:
        if other.get("id") == mid:
            continue
        pair = "|".join(sorted([mid, other["id"]]))
        frac, n = agreement.get(pair), agreement_n.get(pair) or 0
        if frac is not None and n >= 10:
            ag.append((frac, n, other))
    ag.sort(key=lambda x: -x[0])

    tag_label = {t["id"]: t.get("label") or t["id"] for t in data.get("tags") or []}
    tag_rows = sorted((s.get("by_tag") or {}).items(), key=lambda kv: -kv[1])[:20]

    mine = [m for m in data.get("motions") or []
            if any(v.get("member_id") == mid for v in m.get("votes") or [])]
    mine.sort(key=lambda m: m.get("date") or "", reverse=True)

    current = is_current(member, today)
    out: list[str] = []
    out.append('<nav class="v2-crumbs" aria-label="Breadcrumb"><a href="/members.html">'
               f'Members</a><span aria-hidden="true">›</span><span>{esc(member["name"])}'
               '</span></nav>')
    out.append(
        '<header class="mp-head">'
        f'<span class="rc-ava mp-ava" aria-hidden="true">{esc(initials(member["name"]))}</span>'
        '<div class="mp-id">'
        f'<p class="v2-label">Eagle Mountain &middot; {esc(label)}'
        f'{"" if current else " &middot; former member"}</p>'
        f'<h1>{esc(member["name"])}</h1>{term_lines(member)}</div></header>')

    rec = ['<section class="mb-section" aria-labelledby="rec-h">',
           '<h2 class="mb-h2" id="rec-h">Their roll-call record</h2>',
           '<ul class="rc-keys">',
           f'<li class="rc-key"><span class="rc-keyfig">{total:,}</span>'
           '<span class="rc-keylab">roll calls on file</span></li>',
           f'<li class="rc-key"><span class="rc-keyfig mp-yes">{s["yes"]:,}</span>'
           '<span class="rc-keylab">voted yes</span></li>',
           f'<li class="rc-key"><span class="rc-keyfig mp-no">{opposed:,}</span>'
           '<span class="rc-keylab">voted no</span></li>',
           f'<li class="rc-key"><span class="rc-keyfig">{not_voting:,}</span>'
           f'<span class="rc-keylab">didn&rsquo;t vote: {s["abstain"]} abstained, '
           f'{s["excused"]} excused, {s["absent"]} absent</span></li>',
           '</ul>']
    if total:
        rec.append(
            f'<span class="rc-spine" role="img" aria-label="{s["yes"]} yes, {opposed} no, '
            f'{not_voting} not voting"><span class="s-yes" style="width:{pct(s["yes"])}%">'
            f'</span><span class="s-no" style="width:{pct(opposed)}%"></span>'
            f'<span class="s-other" style="width:{pct(not_voting)}%"></span></span>')
        rec.append(
            '<p class="rc-note">When they voted one way or the other, they voted yes '
            f'<b>{s["yes_rate"] * 100:.0f}%</b> of the time. Most {esc(noun)} business '
            'passes unanimously, so a high rate is normal here. '
            '<a href="/members.html">The members page</a> compares how often each member '
            'ends up on the losing side of a vote that split.</p>')
    rec.append("</section>")
    out.append("".join(rec))

    sec = ['<section class="mb-section" aria-labelledby="ag-h">',
           '<h2 class="mb-h2" id="ag-h">Who they vote with</h2>']
    if ag:
        sec.append('<p class="rc-note">How often they and each colleague voted the same '
                   'way, yes or no, on the roll calls where both were recorded. Pairs with '
                   'fewer than 10 shared votes are left out.</p><ul class="mp-ag">')
        for frac, n, other in ag:
            share = f"{frac * 100:.0f}"
            sec.append(
                f'<li><a class="mp-ag-who" href="{member_href(body["id"], other["id"])}">'
                f'<span class="pair-avatar" aria-hidden="true">{esc(initials(other["name"]))}</span>'
                f'<span><span class="pair-name">{esc(other["name"])}</span>'
                f'<span class="mp-ag-role">{esc(other.get("role") or "Member")}'
                f'{"" if is_current(other, today) else ", former"}</span></span></a>'
                f'<span class="pair-bar-wrap" title="Agreed on {share}% of {n} shared roll calls">'
                f'<span class="pair-bar" style="width:{share}%;background:var(--blue)"></span>'
                f'<span class="pair-meta"><span class="pair-pct">{share}%</span>'
                f'<span class="pair-n">of {n} votes</span></span></span></li>')
        sec.append("</ul>")
    else:
        sec.append('<p class="rc-note">Not enough overlapping votes to work out agreement yet.</p>')
    sec.append("</section>")
    out.append("".join(sec))

    sec = ['<section class="mb-section" aria-labelledby="tag-h">',
           '<h2 class="mb-h2" id="tag-h">What they voted on</h2>']
    if tag_rows:
        sec.append('<p class="rc-note">Their roll calls by topic. Each opens the votes list '
                   'filtered to that topic and to them.</p><ul class="mp-tags">')
        for tid, count in tag_rows:
            sec.append(f'<li><a href="/motions.html?member={esc(mid)}&amp;tag={esc(tid)}">'
                       f'{esc(tag_label.get(tid, tid))} <span class="cl-filter-n">{count}</span>'
                       '</a></li>')
        sec.append("</ul>")
    else:
        sec.append('<p class="rc-note">No tagged motions in their record yet.</p>')
    sec.append("</section>")
    out.append("".join(sec))

    rows = []
    for i, m in enumerate(mine[:50]):
        my = next((v for v in m.get("votes") or [] if v.get("member_id") == mid), {})
        how = (my.get("vote") or "").lower()
        tone = ("ok" if how in ("yes", "aye") else "bad" if how in ("no", "nay")
                else "cool")
        said = VOTED.get(how) or (f"Voted {how}" if my.get("vote") else "No vote recorded")
        title = m.get("headline") or "Motion as recorded"
        meeting = f'/meetings/{m.get("date")}-{body["id"]}-{m.get("meeting_id")}.html'
        link = (f'<p><a href="{esc(meeting)}">The meeting it was decided at</a></p>'
                if (DOCS / meeting.lstrip("/")).exists() else "")
        rows.append(
            f'<details class="mv-row" id="m{esc(m.get("id"))}"{" hidden" if i >= SHOW else ""}>'
            '<summary><span class="mv-meta">'
            f'<span>{fmt_date(m.get("date"))}</span>'
            f'<span class="v2-pill v2-tone-{tone}">{esc(said)}</span>'
            f'{outcome_pill(m.get("outcome"))}</span>'
            f'<span class="mv-title">{esc(title)}</span>'
            + (f'<span class="mv-sum">{esc(m["summary"])}</span>' if m.get("summary") else "")
            + f"</summary>{link}</details>")
    more = (f'<p><button type="button" class="v2-btn v2-btn--quiet" id="more-votes">'
            f'Show {min(50, len(mine)) - SHOW} more</button></p>' if len(mine) > SHOW else "")
    out.append(
        '<section class="mb-section" aria-labelledby="recent-h">'
        '<h2 class="mb-h2" id="recent-h">Their latest votes</h2>'
        '<p class="rc-note">Newest first. Open one for what it did and how everyone voted, or '
        f'<a href="/motions.html?member={esc(mid)}">search all {len(mine):,} of their votes</a>.</p>'
        f'<div id="recent-votes" class="mv-list">{"".join(rows)}</div>{more}</section>')

    return "\n        ".join(out)


def describe(data: dict, member: dict, label: str) -> str:
    s = ((data.get("stats") or {}).get("by_member") or {}).get(member["id"]) or {}
    total = s.get("total") or 0
    if not total:
        return (f"{member['name']}, {member.get('role') or 'member'} of the Eagle Mountain "
                f"{label}: their terms and the roll calls on file, from the city's minutes.")
    years = sorted(m.get("date", "")[:4] for m in data.get("motions") or []
                   if any(v.get("member_id") == member["id"] for v in m.get("votes") or []))
    since = f" since {years[0]}" if years and years[0] else ""
    return (f"How {member['name']} has voted on the Eagle Mountain {label}: "
            f"{total:,} roll calls on file{since}, yes {s.get('yes_rate', 0) * 100:.0f}% "
            "of the time, and which colleagues they vote with most.")


def schema(base: str, body: dict, member: dict, url: str, title: str, label: str,
           is_default: bool) -> str:
    org = ({"@id": f"{base}/#council"} if is_default else
           {"@type": "GovernmentOrganization", "name": f"Eagle Mountain {label}"})
    node = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "ProfilePage",
                "@id": f"{url}#webpage",
                "url": url,
                "name": title,
                "inLanguage": "en-US",
                "isPartOf": {"@id": f"{base}/#website"},
                "publisher": {"@id": f"{base}/#publisher"},
                "mainEntity": {"@id": f"{url}#person"},
                "breadcrumb": {
                    "@type": "BreadcrumbList",
                    "itemListElement": [
                        {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{base}/"},
                        {"@type": "ListItem", "position": 2, "name": "Members",
                         "item": f"{base}/members.html"},
                        {"@type": "ListItem", "position": 3, "name": member["name"], "item": url},
                    ],
                },
            },
            {
                "@type": "Person",
                "@id": f"{url}#person",
                "name": member["name"],
                "url": url,
                "jobTitle": member.get("role"),
                "memberOf": org,
            },
        ],
    }
    body_json = json.dumps(node, indent=2, ensure_ascii=False).replace("</", "<\\/")
    indented = "\n".join(f"    {line}" for line in body_json.splitlines())
    return f'    <script type="application/ld+json">\n{indented}\n    </script>\n'


# Relative URLs in the template, which sits at the site root; the generated
# pages are two directories down. Fragments, absolute URLs and the script's own
# ${…} interpolations are left alone.
RELATIVE = re.compile(r'\b(href|src)="(?![/#]|https?:|mailto:|\$\{)([^"]+)"')
CONTENT = re.compile(r'<div id="content" aria-busy="true">.*?</div>\n      </div>\n', re.S)


def meta_swap(page: str, attr: str, key: str, value: str) -> str:
    pattern = re.compile(rf'(<meta {attr}="{re.escape(key)}" content=")[^"]*(" />)')
    page, n = pattern.subn(lambda m: m.group(1) + esc(value) + m.group(2), page)
    if n != 1:
        raise SystemExit(f"member.html: expected one {key} meta tag, found {n}")
    return page


def render_page(template: str, base: str, body: dict, data: dict, member: dict,
                today: str, is_default: bool) -> str:
    label = data.get("body_label") or body["label"]
    url = member_url(base, body["id"], member["id"])
    title = f"{member['name']} — Voting Record, Eagle Mountain {label}"
    description = describe(data, member, label)

    page = RELATIVE.sub(lambda m: f'{m.group(1)}="/{m.group(2)}"', template)
    page = page.replace(
        '<html lang="en">',
        '<!-- Generated by ingest/build_member_pages.py from docs/member.html. '
        'Do not edit by hand. -->\n'
        f'<html lang="en" data-body="{esc(body["id"])}" data-member="{esc(member["id"])}">', 1)
    page = re.sub(r"<title>[^<]*</title>", f"<title>{esc(title)}</title>", page, count=1)
    page = meta_swap(page, "name", "description", description)
    page = meta_swap(page, "property", "og:title", title)
    page = meta_swap(page, "property", "og:description", description)
    page = meta_swap(page, "name", "twitter:title", title)
    page = meta_swap(page, "name", "twitter:description", description)

    head_comment = re.compile(r"    <!-- This file is two things\..*?-->\n", re.S)
    if not head_comment.search(page):
        raise SystemExit("member.html: the head comment this replaces has moved")
    page = head_comment.sub(lambda _: (
        f'    <link rel="canonical" href="{esc(url)}" data-fixed />\n'
        f'    <meta property="og:url" content="{esc(url)}" />\n'), page, count=1)
    page = page.replace('<meta name="robots" content="noindex, follow" />',
                        '<meta name="robots" content="index, follow, '
                        'max-image-preview:large, max-snippet:-1" />', 1)
    page = page.replace("  </head>",
                        schema(base, body, member, url, title, label, is_default)
                        + "  </head>", 1)

    record = render_record(body, data, member, today)
    page, n = CONTENT.subn(lambda _: (f'<div id="content">\n        {record}\n'
                                      '      </div>\n'), page, count=1)
    if n != 1:
        raise SystemExit("member.html: couldn't find the #content skeleton to replace")
    return page


def build() -> dict[str, str]:
    """Relative path under docs/members -> page."""
    template = TEMPLATE.read_text(encoding="utf-8")
    base = site_base()
    pages: dict[str, str] = {}
    for body in body_registry():
        data = load_json(DOCS / body["data_file"])
        if not data:
            print(f"  {body['id']}: no data file, skipped", file=sys.stderr)
            continue
        today = (data.get("generated_at") or "")[:10] or date.today().isoformat()
        for member in data.get("members") or []:
            if not member.get("id"):
                continue
            pages[f"{body['id']}/{member['id']}.html"] = render_page(
                template, base, body, data, member, today, body["default"])
    return pages


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--check", action="store_true",
                    help="don't write; exit 1 if any page would change")
    ap.add_argument("--prune", action="store_true",
                    help="delete pages in docs/members/ we no longer generate")
    args = ap.parse_args(argv)

    pages = build()
    if not pages:
        print("no member pages built; leaving docs/members alone", file=sys.stderr)
        return 1

    changed = 0
    for rel, content in sorted(pages.items()):
        path = OUT_DIR / rel
        if path.exists() and path.read_text(encoding="utf-8") == content:
            continue
        changed += 1
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    existing = ({str(p.relative_to(OUT_DIR)) for p in OUT_DIR.glob("*/*.html")}
                if OUT_DIR.is_dir() else set())
    orphans = sorted(existing - set(pages))

    if args.check:
        if changed or orphans:
            print(f"{changed} page(s) stale, {len(orphans)} orphaned", file=sys.stderr)
            return 1
        print(f"docs/members is current ({len(pages)} pages)")
        return 0

    for rel in orphans:
        if args.prune:
            (OUT_DIR / rel).unlink()
            print(f"  pruned {rel}")
        else:
            print(f"  orphan (use --prune to delete): {rel}", file=sys.stderr)

    print(f"wrote {changed} of {len(pages)} pages in docs/members")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
