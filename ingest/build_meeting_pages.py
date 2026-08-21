"""
Give every meeting a page of its own.

Two hundred meetings currently live behind one URL, meetings.html?id=<n>, which
the script fills in from data.json. That means there is exactly one page for the
whole archive, so there is nothing for a search engine to rank against a query
about one night's business — and the incumbent for "eagle mountain city council
meeting minutes" is a wall of untitled PDFs on utah.gov/pmn. A titled HTML page
per meeting beats those on the strength of having a title.

It also puts the transcripts in reach. There are 450,000-odd words of verbatim
recording in docs/transcripts/, fetched by script, linked from no href, listed in
no sitemap — the one thing on this site nobody else has, and search engines
cannot see a word of it. Meetings with a recording get a second page carrying the
transcript in full.

URLs
    /meetings/2026-07-21-city-council-727.html
    /meetings/2026-07-21-city-council-727-transcript.html

The event id is in there on purpose. Five dates in the archive already hold two
council meetings, and the alternative — numbering collisions in the order they
happen to appear — moves an existing page's URL the day a backfilled meeting
lands on the same date. A URL that changes is worse than a URL with a number in
it. The date and the body's name carry the search terms either way.

Relationship to meetings.html?id=
    The query-string URL keeps working: it is what the agent corpus emits, and
    what anyone who bookmarked a meeting has. It now declares this page as its
    canonical, so the two consolidate into one rather than competing.

Run:  python -m ingest.build_meeting_pages
      python -m ingest.build_meeting_pages --check    # exit 1 if any page stale
      python -m ingest.build_meeting_pages --prune    # delete orphaned pages
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date
from pathlib import Path

from . import bodies
from .nav import nav_links

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT_DIR = DOCS / "meetings"
PARSED = ROOT / "data" / "parsed"
TRANSCRIPTS = DOCS / "transcripts"
CSS_VERSION = "20260821c"

CNAME = DOCS / "CNAME"
DEFAULT_HOST = "civicrollcall.com"

MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")

# Only ever used for the page's own prose. item_title and motion are the OCR of
# the minutes PDF and 302 of them carry scanning damage; they appear on these
# pages as quoted source text, clearly attributed, but never as a title, a
# heading or a meta description. See ingest/build_prerender.py.
CLEAN_FIELDS = ("headline", "summary", "impact")


def esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def site_base() -> str:
    host = DEFAULT_HOST
    if CNAME.exists():
        lines = CNAME.read_text(encoding="utf-8").strip().splitlines()
        if lines and lines[0].strip():
            host = lines[0].strip()
    return f"https://{host}"


def fmt_date(iso: str) -> str:
    try:
        d = date.fromisoformat((iso or "")[:10])
    except ValueError:
        return esc(iso)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


# ---------------------------------------------------------------------------
# A very small Markdown subset, for the transcript summaries
# ---------------------------------------------------------------------------
# The summaries are machine-written to a fixed prompt and use six constructs and
# no others: h2, h3, pipe tables, bullet lists, paragraphs, and **bold** /
# *italic* inline. Surveyed across every summary on disk — no links, no code
# spans, no raw HTML, no ordered lists. A dependency to parse that would be a
# new install in the ingest requirements for the whole pipeline; this is the
# subset, written out, and build_meeting_pages --check exercises it on every
# real file. If a future summary grows a construct this doesn't know, it comes
# out as literal text rather than as broken markup.

INLINE = (
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), r"<em>\1</em>"),
)


def inline_md(text: str) -> str:
    out = esc(text)
    for pattern, repl in INLINE:
        out = pattern.sub(repl, out)
    return out


def render_markdown(src: str) -> str:
    lines = src.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    bullets: list[str] = []
    table: list[list[str]] = []

    def flush_para() -> None:
        if para:
            out.append(f"<p>{inline_md(' '.join(para).strip())}</p>")
            para.clear()

    def flush_bullets() -> None:
        if bullets:
            items = "".join(f"<li>{inline_md(b)}</li>" for b in bullets)
            out.append(f"<ul>{items}</ul>")
            bullets.clear()

    def flush_table() -> None:
        if not table:
            return
        head, *rest = table
        # A pipe table's second row is the alignment rule; it isn't data.
        if rest and all(set(c) <= set("-: ") for c in rest[0] if c):
            rest = rest[1:]
        ths = "".join(f"<th>{inline_md(c)}</th>" for c in head)
        trs = "".join(
            "<tr>" + "".join(f"<td>{inline_md(c)}</td>" for c in row) + "</tr>"
            for row in rest
        )
        # A bare <table>: .md-body already styles tables, including the
        # overflow-x that keeps a wide one from stretching the page.
        out.append(f"<table><thead><tr>{ths}</tr></thead>"
                   f"<tbody>{trs}</tbody></table>")
        table.clear()

    def flush_all() -> None:
        flush_para()
        flush_bullets()
        flush_table()

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            flush_all()
            continue

        if stripped.startswith("|"):
            flush_para()
            flush_bullets()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            table.append(cells)
            continue
        flush_table()

        heading = re.match(r"^(#{2,6})\s+(.*)$", stripped)
        if heading:
            flush_all()
            level = min(len(heading.group(1)) + 1, 6)  # h2 in source -> h3 here
            out.append(f"<h{level}>{inline_md(heading.group(2))}</h{level}>")
            continue

        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if bullet:
            flush_para()
            bullets.append(bullet.group(1))
            continue
        flush_bullets()

        para.append(stripped)

    flush_all()
    # .md-body is the site's existing stylesheet for rendered Markdown — the same
    # wrapper meetings.html puts around marked's output. Reusing it means these
    # pages inherit the headings, tables and blockquotes the site already has
    # rather than carrying a second, nearly-identical set of rules.
    return '<div class="md-body">\n' + "\n".join(out) + "\n</div>"


def first_sentence(text: str, limit: int = 300) -> str:
    """The 'In short:' line off the front of a summary, for a description."""
    flat = re.sub(r"\s+", " ", re.sub(r"[*#|]", "", text or "")).strip()
    flat = re.sub(r"^In short:\s*", "", flat)
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0]
    return cut + "…"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def body_registry() -> list[dict]:
    listed = []
    for body in bodies.all_bodies():
        try:
            data_file = Path(bodies.data_file(body)).name
        except (AttributeError, KeyError, TypeError):
            data_file = ("data.json" if body.get("default")
                         else f"data.{body['id']}.json")
        listed.append({
            "id": body["id"],
            "label": body.get("label", body["id"]),
            "data_file": data_file,
            "default": bool(body.get("default")),
        })
    return listed


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def transcript_index() -> dict[str, dict[int, dict]]:
    """body id -> meeting id -> manifest entry."""
    manifest = load_json(TRANSCRIPTS / "index.json") or {}
    out: dict[str, dict[int, dict]] = {}
    for body_id, entries in manifest.items():
        out[body_id] = {e["id"]: e for e in entries if "id" in e}
    return out


def agenda_only_index() -> dict[str, dict[int, dict]]:
    """body id -> meeting id -> entry, from ingest.build_agenda_only.

    Meetings the city held and noticed but never minuted or recorded. Empty (and
    the pages simply absent) until that build has run.
    """
    manifest = (load_json(DOCS / "data.agenda-only.json") or {}).get("bodies") or {}
    return {bid: {e["id"]: e for e in entries if "id" in e}
            for bid, entries in manifest.items()}


def read_text(rel: str | None) -> str:
    """A transcript file named by the manifest, relative to docs/."""
    if not rel:
        return ""
    path = DOCS / rel
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def detail_for(meeting: dict) -> dict:
    source = meeting.get("source_file") or ""
    if not source:
        return {}
    return load_json(PARSED / source.replace(".pdf", ".json")) or {}


# ---------------------------------------------------------------------------
# Page chrome
# ---------------------------------------------------------------------------
# Root-absolute asset paths throughout: these pages sit one directory down, and
# the rest of the site is flat, so a relative href would have to climb out of
# /meetings/ on every link. Same choice 404.html makes, for the same reason.

NAV = """    <header class="topbar">
      <a class="brand" href="/index.html">
        <span class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 512 512" focusable="false">
            <circle class="m-accent" cx="153" cy="153" r="33.5" />
            <rect class="m-fg" x="220" y="133" width="175" height="41" rx="20.5" />
            <circle class="m-fg" cx="153" cy="256" r="33.5" />
            <rect class="m-dim" x="220" y="235" width="144" height="42" rx="21" />
            <circle class="m-accent" cx="153" cy="358" r="33.5" />
            <rect class="m-dim" x="220" y="338" width="113" height="41" rx="20.5" />
          </svg>
        </span>
        <div><p class="brand-title">Roll Call</p><p class="city">Eagle Mountain, UT</p></div>
      </a>
      <nav class="nav" aria-label="Primary">
""" + nav_links(root=True, active="meetings.html") + """
      </nav>
    </header>
"""

FOOTER = ('    <footer class="footer"><span>Data from Eagle Mountain CivicClerk '
          'portal &middot; <a href="/about.html">How this site is built</a>'
          ' &middot; <a href="/definitions.html">Definitions</a>'
          "</span></footer>\n")


def head(title: str, description: str, canonical: str, extra: str = "") -> str:
    """The <head> these pages share. `canonical` is marked data-fixed so
    site.js leaves it alone — see setCanonical() for why that matters here."""
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <title>{esc(title)}</title>
    <meta name="description" content="{esc(description)}" />
    <link rel="canonical" href="{esc(canonical)}" data-fixed />
    <meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1" />
    <meta name="theme-color" content="#FFFDF8" media="(prefers-color-scheme: light)" />
    <meta name="theme-color" content="#1F231C" media="(prefers-color-scheme: dark)" />
    <meta property="og:type" content="article" />
    <meta property="og:site_name" content="Civic Roll Call" />
    <meta property="og:url" content="{esc(canonical)}" />
    <meta property="og:title" content="{esc(title)}" />
    <meta property="og:description" content="{esc(description)}" />
    <meta property="og:image" content="{site_base()}/icons/icon-512.png" />
    <meta name="twitter:card" content="summary" />
    <meta name="twitter:title" content="{esc(title)}" />
    <meta name="twitter:description" content="{esc(description)}" />
    <link rel="manifest" href="/manifest.webmanifest" />
    <link rel="icon" href="/icons/icon-192.png" />
    <link rel="apple-touch-icon" href="/icons/icon-192.png" />
    <link rel="preload" href="/fonts/instrument-sans-400-latin.woff2" as="font" type="font/woff2" crossorigin />
    <link rel="stylesheet" href="/fonts.css?v={CSS_VERSION}" />
    <link rel="stylesheet" href="/site.css?v={CSS_VERSION}" />
{extra}  </head>
  <body>
    <a href="#main" class="skip-link">Skip to main content</a>
{NAV}
"""


def tail() -> str:
    return f"""
{FOOTER}
    <script src="/site.js?v={CSS_VERSION}"></script>
  </body>
</html>
"""


# ---------------------------------------------------------------------------
# The meeting page
# ---------------------------------------------------------------------------

VOTE_CLASS = {"yes": "ok", "no": "danger", "nay": "danger"}


def vote_row(motion: dict, members: dict) -> str:
    votes = motion.get("votes") or []
    if not votes:
        return '<p class="muted">No roll call was recorded for this motion.</p>'
    chips = []
    for vote in votes:
        name = (members.get(vote.get("member_id")) or {}).get(
            "name", vote.get("member_id") or "Unknown")
        how = (vote.get("vote") or "").strip()
        tone = VOTE_CLASS.get(how.lower(), "muted")
        chips.append(
            f'<li class="mp-vote mp-{tone}"><span class="mp-who">{esc(name)}</span>'
            f'<span class="mp-how">{esc(how)}</span></li>'
        )
    return f'<ul class="mp-votes">{"".join(chips)}</ul>'


def render_meeting(body: dict, meeting: dict, motions: list[dict],
                   members: dict, detail: dict, transcript: dict | None,
                   base: str, slug: str) -> tuple[str, str, str]:
    """Returns (html, title, description)."""
    when = fmt_date(meeting.get("date", ""))
    label = body["label"]
    canonical = f"{base}/meetings/{slug}.html"

    title = f"{label}, {when} — Eagle Mountain"
    written = [m for m in motions if m.get("headline")]

    if transcript and transcript.get("summary_md"):
        description = first_sentence(transcript["summary_md"].split("\n\n")[0], 300)
    elif meeting.get("summary"):
        description = first_sentence(meeting["summary"], 300)
    elif written:
        description = first_sentence(
            f"{len(motions)} motions decided, including: "
            + "; ".join(m["headline"] for m in written[:3]), 300)
    else:
        description = (f"Motions and roll-call votes from the Eagle Mountain "
                       f"{label} meeting on {when}.")

    parts: list[str] = ['    <main id="main">']
    parts.append('      <nav class="crumbs" aria-label="Breadcrumb">'
                 '<a href="/meetings.html">All meetings</a>'
                 f'<span aria-hidden="true"> / </span><span>{esc(when)}</span></nav>')
    if meeting.get("type"):
        parts.append(f'      <p class="meeting-type">{esc(meeting["type"])}</p>')
    parts.append(f'      <h1 class="page-title">Eagle Mountain {esc(label)}, {esc(when)}</h1>')

    counts = (f'{len(motions)} motion{"" if len(motions) == 1 else "s"} · '
              f'{meeting.get("ord_count", 0)} ordinance lines · '
              f'{meeting.get("res_count", 0)} resolution lines')
    parts.append(f'      <p class="page-sub">{esc(counts)}</p>')

    if transcript and transcript.get("text"):
        tslug = f"{slug}-transcript"
        parts.append(
            '      <p class="mp-tx-link">🎙 This meeting was recorded. '
            f'<a href="/meetings/{esc(tslug)}.html">Read the full transcript</a>.</p>'
        )

    if meeting.get("transcript_only"):
        parts.append(
            '      <div class="mp-notice"><p><strong>The city has not published '
            'approved minutes for this meeting yet.</strong> What follows is drawn from '
            'the meeting recording, so there is no roll call to report here. The motions '
            'and votes will appear once the minutes are approved.</p></div>'
        )

    if meeting.get("summary"):
        parts.append('      <section class="section"><h2>Summary</h2>'
                     f'<p class="meeting-summary">{esc(meeting["summary"])}</p></section>')

    # The prose account of the night, written from the recording. It lives here
    # rather than on the transcript page so the canonical meeting URL owns the
    # readable version and the two pages don't duplicate each other.
    if transcript and transcript.get("summary_md"):
        parts.append('      <section class="section mp-tx-summary">')
        parts.append("        <h2>What happened</h2>")
        parts.append('        <p class="muted mp-tx-caveat">Written from the meeting '
                     'recording, not from the approved minutes.</p>')
        parts.append(render_markdown(transcript["summary_md"]))
        parts.append("      </section>")

    # Decisions.
    if meeting.get("transcript_only") and not motions:
        parts.append("    </main>")
        return "\n".join(parts), title, description

    parts.append('      <section class="section">')
    parts.append('        <h2>Motions and roll-call votes</h2>')
    if not motions:
        parts.append('        <p class="muted">No motions were parsed from these minutes.</p>')
    for motion in motions:
        heading = motion.get("headline")
        parts.append('        <article class="mp-motion">')
        if heading:
            parts.append(f'          <h3 class="mp-h3">{esc(heading)}</h3>')
        else:
            parts.append('          <h3 class="mp-h3 muted">Motion as recorded</h3>')
        if motion.get("summary"):
            parts.append(f'          <p>{esc(motion["summary"])}</p>')
        if motion.get("impact"):
            parts.append('          <p class="mp-impact"><strong>What it means:</strong> '
                         f'{esc(motion["impact"])}</p>')
        parts.append('          <p class="mp-outcome">'
                     f'{esc(motion.get("outcome") or "Outcome not recorded")}</p>')
        parts.append("          " + vote_row(motion, members))
        # The minutes' own wording, quoted and attributed. This is OCR of the
        # PDF, so it is offered as the source text rather than as our prose.
        if motion.get("motion"):
            parts.append('          <details class="mp-src"><summary>The motion as the '
                         "minutes record it</summary>"
                         f'<blockquote>{esc(motion["motion"])}</blockquote></details>')
        parts.append("        </article>")
    parts.append("      </section>")

    # Ordinance / resolution mentions, straight off the PDF.
    for key, heading in (("ordinances", "Ordinance mentions"),
                         ("resolutions", "Resolution mentions")):
        rows = detail.get(key) or []
        if not rows:
            continue
        items = "".join(
            f'<li><span class="mono muted">p.{esc(r.get("page"))}</span> '
            f'{esc(r.get("line_text"))}</li>' for r in rows
        )
        parts.append(
            f'      <section class="section"><h2>{heading} ({len(rows)})</h2>'
            '<details><summary class="muted">Show the lines as scanned</summary>'
            f'<ul class="mp-lines">{items}</ul></details></section>'
        )

    if meeting.get("source_file"):
        parts.append('      <section class="section"><h2>Source</h2>'
                     '<p class="muted">Parsed from the city&rsquo;s approved minutes, '
                     f'<span class="mono">{esc(meeting["source_file"])}</span>, published '
                     'through the Eagle Mountain CivicClerk portal.</p></section>')

    parts.append("    </main>")
    return "\n".join(parts), title, description


def render_agenda(entry: dict) -> list[str]:
    """The posted agenda, as the only account of a meeting that has no other.

    Prints the city's own numbering and wording. An item's plain-English line
    goes first when ingest.summarize_agenda has one cached, with the official
    title kept underneath it — the same order the upcoming-meeting card uses, so
    a reader who saw the meeting coming recognises it afterwards.
    """
    parts: list[str] = []
    sessions = {s["start"]: s for s in entry.get("sessions") or []}
    current: str | None = None

    parts.append('      <section class="section">')
    parts.append("        <h2>What was on the agenda</h2>")
    for head in entry.get("agenda") or []:
        session = head.get("session")
        if session and session != current and session in sessions:
            s = sessions[session]
            parts.append(f'        <h3 class="mp-session">{esc(s["label"])}'
                         f' &middot; {esc(s["start_label"])}</h3>')
            current = session
        title = head.get("title") or ""
        cls = "mp-ag-head muted" if head.get("procedural") else "mp-ag-head"
        parts.append(f'        <h3 class="{cls}">'
                     f'<span class="mono muted">{esc(head.get("number"))}.</span> '
                     f"{esc(title)}</h3>")
        if head.get("note"):
            parts.append(f'        <p class="muted">{esc(head["note"])}</p>')
        if not head.get("items"):
            continue
        parts.append('        <ul class="mp-ag-items">')
        for item in head["items"]:
            bits = [f'<span class="mono muted">{esc(item.get("number"))}</span>']
            if item.get("kind"):
                bits.append(f'<span class="ag-kind">{esc(item["kind"].title())}</span>')
            if item.get("plain"):
                bits.append(f'<span class="mp-ag-plain">{esc(item["plain"])}</span>'
                            f'<span class="muted mp-ag-official">'
                            f'{esc(item.get("title"))}</span>')
            else:
                bits.append(esc(item.get("title")))
            if item.get("time"):
                bits.append(f'<span class="muted">({esc(item["time"])})</span>')
            parts.append("          <li>" + " ".join(bits) + "</li>")
        parts.append("        </ul>")
    parts.append("      </section>")
    return parts


def render_agenda_only(body: dict, meeting: dict, entry: dict,
                       base: str, slug: str) -> tuple[str, str, str]:
    """A meeting whose only public record is the agenda. Returns (html, title,
    description)."""
    when = fmt_date(meeting.get("date", ""))
    label = body["label"]
    title = f"{label}, {when} — Eagle Mountain"
    heads = [h for h in (entry.get("agenda") or []) if not h.get("procedural")]
    topics = "; ".join(h["title"].title() for h in heads[:3] if h.get("title"))
    description = (
        f"The posted agenda for the Eagle Mountain {label} meeting on {when}"
        + (f": {topics}." if topics else ".")
        + " The city published no minutes for this meeting."
    )

    parts: list[str] = ['    <main id="main">']
    parts.append('      <nav class="crumbs" aria-label="Breadcrumb">'
                 '<a href="/meetings.html">All meetings</a>'
                 f'<span aria-hidden="true"> / </span><span>{esc(when)}</span></nav>')
    if entry.get("title"):
        parts.append(f'      <p class="meeting-type">{esc(entry["title"])}</p>')
    parts.append(f'      <h1 class="page-title">Eagle Mountain {esc(label)}, {esc(when)}</h1>')

    sub = " · ".join(p for p in (entry.get("start_label"), entry.get("location")) if p)
    if sub:
        parts.append(f'      <p class="page-sub">{esc(sub)}</p>')

    # Said plainly and said first: this page is the notice of a meeting, not a
    # record of one. Everything below it is what the council meant to take up.
    recording = entry.get("recording_url")
    parts.append(
        '      <div class="mp-notice"><p><strong>The city published no minutes '
        'for this meeting.</strong> What follows is the agenda it posted beforehand, '
        'so it says what the council intended to take up — not what was decided, and '
        'not who voted for it. If minutes are approved later, they will replace this '
        'page.</p>'
        + (f'<p>The city did post a recording of this meeting. It has not been '
           f'transcribed here, so nothing in it is searchable, but you can '
           f'<a href="{esc(recording)}">watch it on the city&rsquo;s portal</a>.</p>'
           if recording else "")
        + "</div>"
    )

    if entry.get("agenda"):
        parts.extend(render_agenda(entry))
    else:
        parts.append('      <section class="section"><p class="muted">The posted '
                     'agenda for this meeting doesn&rsquo;t follow the city&rsquo;s '
                     'usual template, so it isn&rsquo;t reproduced here. The PDF is '
                     'linked below.</p></section>')

    parts.append('      <section class="section"><h2>Source</h2><p class="muted">')
    links = []
    if entry.get("agenda_url"):
        links.append(f'<a href="{esc(entry["agenda_url"])}">the agenda PDF</a>')
    if entry.get("url"):
        links.append(f'<a href="{esc(entry["url"])}">the event on the city&rsquo;s portal</a>')
    parts.append("Read " + " and ".join(links) + "." if links else
                 "Published through the Eagle Mountain CivicClerk portal.")
    if entry.get("agenda_posted_on"):
        parts.append(f' The city recorder certifies the agenda was posted on '
                     f'{esc(fmt_date(entry["agenda_posted_on"]))}.')
    parts.append("</p></section>")

    parts.append("    </main>")
    return "\n".join(parts), title, description


def meeting_schema(body: dict, meeting: dict, motions: list[dict],
                   base: str, slug: str, title: str, description: str) -> str:
    canonical = f"{base}/meetings/{slug}.html"
    node = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "Event",
                "@id": f"{canonical}#event",
                "name": f"Eagle Mountain {body['label']} meeting, {fmt_date(meeting.get('date',''))}",
                "description": description,
                "startDate": meeting.get("date"),
                "url": canonical,
                "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
                "isAccessibleForFree": True,
                "inLanguage": "en-US",
                "organizer": {"@id": f"{base}/#council"},
                "location": {
                    "@type": "Place",
                    "name": "Eagle Mountain City Hall",
                    "address": {
                        "@type": "PostalAddress",
                        "addressLocality": "Eagle Mountain",
                        "addressRegion": "UT",
                        "addressCountry": "US",
                    },
                },
            },
            {
                "@type": "WebPage",
                "@id": f"{canonical}#webpage",
                "url": canonical,
                "name": title,
                "inLanguage": "en-US",
                "isPartOf": {"@id": f"{base}/#website"},
                "publisher": {"@id": f"{base}/#publisher"},
                "mainEntity": {"@id": f"{canonical}#event"},
                "breadcrumb": {
                    "@type": "BreadcrumbList",
                    "itemListElement": [
                        {"@type": "ListItem", "position": 1, "name": "Home",
                         "item": f"{base}/"},
                        {"@type": "ListItem", "position": 2, "name": "Meetings",
                         "item": f"{base}/meetings.html"},
                        {"@type": "ListItem", "position": 3,
                         "name": fmt_date(meeting.get("date", "")), "item": canonical},
                    ],
                },
            },
        ],
    }
    body_json = json.dumps(node, indent=2, ensure_ascii=False)
    indented = "\n".join(f"    {line}" for line in body_json.splitlines())
    return ('    <script type="application/ld+json">\n'
            f"{indented}\n    </script>\n")


# ---------------------------------------------------------------------------
# The transcript page
# ---------------------------------------------------------------------------

SPEAKER_LINE = re.compile(r"^(Speaker [A-Z]|[A-Z][\w.'-]*(?: [A-Z][\w.'-]*){0,3}):\s*(.*)$")

# Sentences per paragraph when a transcript has no speaker labels to break on.
SENTENCES_PER_PARA = 4
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def transcript_turns(text: str) -> list[tuple[str | None, str]]:
    """Reflow one-line-per-segment ASR output into readable turns.

    The .txt files carry one line per recogniser segment, which is a clause and
    not a sentence — rendered a paragraph each, a three-hour meeting comes out as
    two thousand orphaned fragments. Some files label the speaker on each segment
    and some don't, so there are two jobs: merge consecutive segments from the
    same speaker into one turn, and where nothing is labelled, fall back to
    breaking on sentences so the page is still readable prose.
    """
    turns: list[tuple[str | None, list[str]]] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        hit = SPEAKER_LINE.match(line)
        if hit:
            who, said = hit.group(1), hit.group(2).strip()
            if turns and turns[-1][0] == who:
                if said:
                    turns[-1][1].append(said)
            else:
                turns.append((who, [said] if said else []))
        elif turns:
            turns[-1][1].append(line)
        else:
            turns.append((None, [line]))

    out: list[tuple[str | None, str]] = []
    for who, chunks in turns:
        body = " ".join(c for c in chunks if c).strip()
        if not body:
            continue
        if who is not None:
            out.append((who, body))
            continue
        # Unlabelled: break the run into paragraphs a few sentences long.
        sentences = SENTENCE_END.split(body)
        for i in range(0, len(sentences), SENTENCES_PER_PARA):
            para = " ".join(sentences[i:i + SENTENCES_PER_PARA]).strip()
            if para:
                out.append((None, para))
    return out


def render_transcript(body: dict, meeting: dict, transcript: dict,
                      base: str, slug: str) -> tuple[str, str, str]:
    when = fmt_date(meeting.get("date", ""))
    label = body["label"]
    tslug = f"{slug}-transcript"
    canonical = f"{base}/meetings/{tslug}.html"
    title = f"Transcript: {label}, {when} — Eagle Mountain"

    summary_md = transcript.get("summary_md") or ""
    if summary_md:
        description = first_sentence(summary_md.split("\n\n")[0], 300)
    else:
        description = (f"Full transcript of the Eagle Mountain {label} meeting "
                       f"held on {when}.")

    parts: list[str] = ['    <main id="main" class="main-reading">']
    parts.append('      <nav class="crumbs" aria-label="Breadcrumb">'
                 '<a href="/meetings.html">All meetings</a>'
                 f'<span aria-hidden="true"> / </span>'
                 f'<a href="/meetings/{esc(slug)}.html">{esc(when)}</a>'
                 '<span aria-hidden="true"> / </span><span>Transcript</span></nav>')
    parts.append(f'      <h1 class="page-title">Transcript &mdash; Eagle Mountain '
                 f'{esc(label)}, {esc(when)}</h1>')
    parts.append(
        '      <p class="page-sub">Produced by speech recognition from the meeting '
        'recording, so it mis-hears names and technical terms. It is a searchable aid '
        'to the recording, not a record of equal standing to the '
        f'<a href="/meetings/{esc(slug)}.html">approved minutes</a>.</p>'
    )

    # No summary here: the prose account lives on the meeting page, which is the
    # canonical URL for this night. Repeating it would make these two pages near
    # duplicates of each other and split whatever they earn.

    # The speaker key, kept separate from the transcript body on purpose. The
    # names are inferred from what each voice says, not from the recording's own
    # labels, so attaching them to individual lines would put words in a named
    # person's mouth on the strength of a guess. Offered here as a key, with the
    # reasoning shown, and the body below keeps the labels the recording gave.
    speakers = transcript.get("speakers") or {}
    if speakers:
        rows = "".join(
            f"<tr><td class=\"mono\">Speaker {esc(k)}</td><td>{esc(v.get('name'))}</td>"
            f"<td class=\"muted\">{esc(v.get('basis'))}</td></tr>"
            for k, v in sorted(speakers.items())
        )
        parts.append(
            '      <section class="section"><h2>Who is speaking</h2>'
            '<p class="muted">Identified from what each voice says during the meeting, '
            'not from any label in the recording. Treat these as probable, not certain.</p>'
            '<div class="tablewrap"><table class="data"><thead><tr><th>Label</th>'
            "<th>Probably</th><th>On what basis</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div></section>"
        )

    text = transcript.get("text") or ""
    if text:
        parts.append('      <section class="section">')
        parts.append("        <h2>Full transcript</h2>")
        parts.append('        <div class="mp-tx">')
        for who, said in transcript_turns(text):
            if who:
                parts.append(
                    f'          <p class="mp-tx-line"><b class="mp-tx-who">'
                    f"{esc(who)}:</b> {esc(said)}</p>"
                )
            else:
                parts.append(f'          <p class="mp-tx-line">{esc(said)}</p>')
        parts.append("        </div>")
        parts.append("      </section>")

    parts.append("    </main>")
    return "\n".join(parts), title, description


def transcript_schema(body: dict, meeting: dict, base: str, slug: str,
                      title: str, description: str) -> str:
    canonical = f"{base}/meetings/{slug}-transcript.html"
    node = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "@id": f"{canonical}#webpage",
        "url": canonical,
        "name": title,
        "description": description,
        "inLanguage": "en-US",
        "isPartOf": {"@id": f"{base}/#website"},
        "publisher": {"@id": f"{base}/#publisher"},
        "about": {"@id": f"{base}/meetings/{slug}.html#event"},
        "breadcrumb": {
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{base}/"},
                {"@type": "ListItem", "position": 2, "name": "Meetings",
                 "item": f"{base}/meetings.html"},
                {"@type": "ListItem", "position": 3,
                 "name": fmt_date(meeting.get("date", "")),
                 "item": f"{base}/meetings/{slug}.html"},
                {"@type": "ListItem", "position": 4, "name": "Transcript",
                 "item": canonical},
            ],
        },
    }
    body_json = json.dumps(node, indent=2, ensure_ascii=False)
    indented = "\n".join(f"    {line}" for line in body_json.splitlines())
    return ('    <script type="application/ld+json">\n'
            f"{indented}\n    </script>\n")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def slug_for(body: dict, meeting: dict) -> str:
    """<date>-<body id>-<event id>.

    The body's *id* rather than a slug of its label, because site.js has to build
    this same string to link to these pages and it has the id to hand. The two
    happen to agree today ("City Council" slugifies to "city-council"), and
    hanging the URL scheme on that coincidence would break every link the day
    somebody retitles a body.
    """
    return f"{meeting.get('date')}-{body['id']}-{meeting.get('id')}"


def build() -> tuple[dict[str, str], list[dict]]:
    """Every page to write, plus the sitemap rows describing them."""
    base = site_base()
    pages: dict[str, str] = {}
    rows: list[dict] = []
    tx_index = transcript_index()
    ao_index = agenda_only_index()

    for body in body_registry():
        data = load_json(DOCS / body["data_file"])
        if not data:
            print(f"  {body['id']}: no data file, skipped", file=sys.stderr)
            continue

        members = {m["id"]: m for m in (data.get("members") or []) if m.get("id")}
        by_meeting: dict[int, list[dict]] = {}
        for motion in data.get("motions") or []:
            by_meeting.setdefault(motion.get("meeting_id"), []).append(motion)

        body_tx = tx_index.get(body["id"], {})

        # Meetings the city has recorded but not yet minuted. They aren't in the
        # dataset — build_dataset only sees approved minutes — so without this
        # they'd have no page at all, and they are the most recent and most
        # searched meetings on the site. The 6 August 2026 tax vote is one of them.
        known = {m.get("id") for m in (data.get("meetings") or [])}
        pending = [
            {
                "id": entry["id"],
                "date": entry.get("date"),
                "type": entry.get("title"),
                "source_file": None,
                "motion_count": 0,
                "ord_count": 0,
                "res_count": 0,
                "transcript_only": True,
            }
            for entry in body_tx.values()
            if entry.get("id") not in known and entry.get("date")
        ]

        # Meetings the city held, noticed, and then never minuted or recorded.
        # The agenda is the whole surviving record, so these pages say so and
        # print it — see ingest.build_agenda_only. Excluded there if minutes or
        # a transcript exist, so they can't shadow a fuller page.
        body_ao = {eid: e for eid, e in (ao_index.get(body["id"]) or {}).items()
                   if eid not in known and e.get("date")}
        agenda_only = [
            {
                "id": e["id"],
                "date": e["date"],
                "type": e.get("title"),
                "source_file": None,
                "motion_count": 0,
                "ord_count": 0,
                "res_count": 0,
                "agenda_only": True,
            }
            for e in body_ao.values()
        ]

        for meeting in (data.get("meetings") or []) + pending + agenda_only:
            mid = meeting.get("id")
            if mid is None or not meeting.get("date"):
                continue
            slug = slug_for(body, meeting)
            motions = sorted(by_meeting.get(mid) or [], key=lambda m: m.get("id") or 0)

            entry = body_tx.get(mid)
            transcript = None
            if entry:
                transcript = {
                    "summary_md": read_text(entry.get("summary_file")),
                    "text": read_text(entry.get("text_file")),
                    "speakers": load_json(DOCS / entry["speakers_file"])
                    if entry.get("speakers_file") else None,
                }
                if not (transcript["summary_md"] or transcript["text"]):
                    transcript = None

            if meeting.get("agenda_only"):
                page, title, description = render_agenda_only(
                    body, meeting, body_ao[mid], base, slug)
            else:
                page, title, description = render_meeting(
                    body, meeting, motions, members, detail_for(meeting),
                    transcript, base, slug)
            schema = meeting_schema(body, meeting, motions, base, slug,
                                    title, description)
            canonical = f"{base}/meetings/{slug}.html"
            pages[f"{slug}.html"] = (head(title, description, canonical, schema)
                                     + page + tail())
            rows.append({"loc": canonical, "date": meeting["date"]})

            if transcript and transcript.get("text"):
                tpage, ttitle, tdesc = render_transcript(
                    body, meeting, transcript, base, slug)
                tschema = transcript_schema(body, meeting, base, slug, ttitle, tdesc)
                tcanonical = f"{base}/meetings/{slug}-transcript.html"
                pages[f"{slug}-transcript.html"] = (
                    head(ttitle, tdesc, tcanonical, tschema) + tpage + tail())
                rows.append({"loc": tcanonical, "date": meeting["date"]})

    return pages, rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--check", action="store_true",
                    help="don't write; exit 1 if any page would change")
    ap.add_argument("--prune", action="store_true",
                    help="delete pages in docs/meetings/ we no longer generate")
    args = ap.parse_args(argv)

    pages, rows = build()
    if not pages:
        print("no meeting pages built; leaving docs/meetings alone", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    changed = 0
    for name, content in sorted(pages.items()):
        path = OUT_DIR / name
        if path.exists() and path.read_text(encoding="utf-8") == content:
            continue
        changed += 1
        if not args.check:
            path.write_text(content, encoding="utf-8")

    existing = {p.name for p in OUT_DIR.glob("*.html")}
    orphans = sorted(existing - set(pages))

    if args.check:
        if changed or orphans:
            print(f"{changed} page(s) stale, {len(orphans)} orphaned", file=sys.stderr)
            return 1
        print(f"docs/meetings is current ({len(pages)} pages)")
        return 0

    for name in orphans:
        if args.prune:
            (OUT_DIR / name).unlink()
            print(f"  pruned {name}")
        else:
            print(f"  orphan (use --prune to delete): {name}", file=sys.stderr)

    transcripts = sum(1 for n in pages if n.endswith("-transcript.html"))
    total = sum(len(c) for c in pages.values())
    print(f"wrote {changed} of {len(pages)} pages in docs/meetings "
          f"({transcripts} transcripts, {total:,} bytes total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
