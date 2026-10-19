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
  members.html   the sitting roster as cards, linked to each member page.
  motions.html   the most recent MOTION_LIMIT motions. There are over a thousand
                 and they're addressed by fragment on this one page, so listing
                 all of them would add page weight without adding a single URL.
  claims.html    every fact check's claim, finding and ruling, each linked to the
                 page build_claim_pages writes for it. Before this the list was
                 the only route to those pages and it existed only in script.
  index.html     the latest votes, the latest fact checks, who sits on the
                 council, the tax and next-meeting cards, and the doors that
                 name the last meeting. The cards used to appear when the data
                 landed and push the page down under the reader (a CLS of 0.13
                 on a phone); written here, the script only confirms them.

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

# The index and the page it links to have to print a meeting's counts the same
# way, so the string is built in one place and imported rather than written out
# again here.
from .build_meeting_pages import counts_line
from .build_claim_pages import TONE as VERDICT_TONE, pill as verdict_pill

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
                         key=lambda m: 0 if m.get("lead") else
                         1 if m.get("significance") == "notable" else 2)
        heads = [m["headline"] for m in motions
                 if m.get("headline")][:HEADLINES_PER_MEETING]

        counts = counts_line(meeting.get("motion_count", 0),
                             meeting.get("ord_count", 0),
                             meeting.get("res_count", 0))
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


CHAIR = re.compile(r"^(mayor|chair|chairman|chairwoman|president)", re.I)


def split_record(data: dict) -> tuple[int, dict[str, dict[str, int]]]:
    """Divided roll calls, and each member's minority side of them.

    Mirrors splitRecord() in docs/members.html. A roll call is divided when at
    least one member voted yes and at least one voted no; abstentions and
    excusals are not sides, so they neither divide a vote nor lose one.
    """
    per: dict[str, dict[str, int]] = {}
    divided = 0
    for motion in data.get("motions") or []:
        votes = [v for v in (motion.get("votes") or [])
                 if str(v.get("vote", "")).lower() in ("yes", "aye", "no", "nay")]
        yes = [v for v in votes if str(v.get("vote", "")).lower() in ("yes", "aye")]
        no = [v for v in votes if v not in yes]
        if not yes or not no:
            continue
        divided += 1
        losing = None if len(yes) == len(no) else ("yes" if len(yes) < len(no) else "no")
        for vote in votes:
            rec = per.setdefault(vote.get("member_id"), {"seen": 0, "minority": 0})
            rec["seen"] += 1
            side = "yes" if vote in yes else "no"
            if losing and side == losing:
                rec["minority"] += 1
    return divided, per


def first_seated(member: dict) -> str:
    starts = sorted(t["start"] for t in (member.get("terms") or []) if t.get("start"))
    return starts[0] if starts else "9999"


def term_years(term: dict) -> str:
    start = (term.get("start") or "")[:4]
    end = (term.get("end") or "")[:4] or "present"
    if not start:
        return end
    return start if start == end else f"{start}\u2013{end}"


def ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def term_line(member: dict) -> str:
    """Mirrors termLine() in docs/members.html."""
    terms = member.get("terms") or []
    role = esc(member.get("role") or "Member")
    now = terms[0] if terms else None
    prior = terms[1:]
    if not now or not (now.get("start") or now.get("end")):
        return role

    run = 1
    for term in prior:
        if term.get("role") == now.get("role"):
            run += 1
        else:
            break
    head = f"{role} &middot; {ordinal(run)} term, {esc(term_years(now))}"

    before = [t for t in prior if t.get("start") or t.get("end")]
    if not before:
        return head
    roles = list(dict.fromkeys(t.get("role") or "" for t in before))
    if len(roles) == 1 and roles[0] and roles[0] != now.get("role"):
        listed = f'{esc(roles[0])} {" and ".join(esc(term_years(t)) for t in before)}'
    else:
        listed = ", ".join(
            esc(term_years(t)) if t.get("role") == now.get("role")
            else f'{esc(t.get("role") or "member")} {esc(term_years(t))}'
            for t in before)
    return f'{head}<span class="rc-prior">Previously {listed}</span>'


def initials(name: str) -> str:
    parts = [p for p in str(name or "").split() if p]
    first = parts[0][0] if parts else ""
    last = parts[-1][0] if parts else ""
    return (first + last or "?").upper()


def split_meter(split: dict[str, int]) -> str:
    """Mirrors splitMeter() in docs/members.html."""
    if not split["seen"]:
        return ('<span class="rc-meter"><span class="rc-mlab">On divided votes</span>'
                '<span class="rc-none">No roll call they have sat for has split yet.</span>'
                "</span>")
    share = split["minority"] / split["seen"] * 100
    caption = ("never on the losing side of one" if not split["minority"]
               else f'on the losing side of <b>{share:.0f}%</b> of them')
    return (
        '<span class="rc-meter">\n'
        f'                  <span class="rc-mlab">On divided votes '
        f'<span class="rc-fig">{split["minority"]} of {split["seen"]}</span></span>\n'
        f'                  <span class="rc-split" role="img" aria-label="On the minority side of '
        f'{split["minority"]} of {split["seen"]} divided roll calls">'
        f'<span style="width:{share:.1f}%"></span></span>\n'
        f'                  <span class="rc-splitline">{caption}</span>\n'
        "                </span>"
    )


def render_members(data: dict, today: str) -> str:
    """The roster, as the cards docs/members.html renders from the live data.

    The two have to agree: this copy is what a crawler and the first paint see,
    and the scripted one replaces it wholesale a moment later. card() and
    splitMeter() in that page are the other half of this function — change one,
    change the other.
    """
    members = [m for m in (data.get("members") or []) if is_current_member(m, today)]
    if not members:
        return ""

    by_member = (data.get("stats") or {}).get("by_member") or {}
    # Chair first, then longest-serving, then by how much record they carry.
    members.sort(key=lambda m: (
        0 if CHAIR.match(m.get("role") or "") else 1,
        first_seated(m),
        -(by_member.get(m.get("id"), {}).get("total") or 0),
    ))

    _, splits = split_record(data)
    cards = []
    for member in members:
        s = by_member.get(member.get("id")) or {}
        yes = s.get("yes", 0)
        no = s.get("no", 0) + s.get("nay", 0)
        other = s.get("abstain", 0) + s.get("excused", 0) + s.get("absent", 0)
        total = s.get("total") or (yes + no + other)
        pct = (lambda x: f"{x / total * 100:.2f}" if total else "0")
        # The member's own page, which ingest/build_member_pages.py writes.
        href = f'/members/city-council/{esc(member.get("id"))}.html'
        chair = bool(CHAIR.match(member.get("role") or ""))
        split = splits.get(member.get("id")) or {"seen": 0, "minority": 0}
        tag = (f'<span class="rc-tag">{esc(member.get("role"))}</span>' if chair else "")
        cards.append(
            f'          <li class="rc-card{" is-chair" if chair else ""}" data-href="{href}">\n'
            f'            <span class="rc-who">\n'
            f'              <span class="rc-ava" aria-hidden="true">{esc(initials(member.get("name")))}</span>\n'
            f'              <span class="rc-id">\n'
            f'                <span class="rc-nameline">\n'
            f'                  <a class="rc-name" href="{href}">{esc(member.get("name"))}</a>{tag}\n'
            f'                </span>\n'
            f'                <span class="rc-term">{term_line(member)}</span>\n'
            f'              </span>\n'
            f'            </span>\n'
            f'            <span class="rc-meters">\n'
            f'              <span class="rc-meter">\n'
            f'                <span class="rc-mlab">Roll-call record '
            f'<span class="rc-fig">{total:,} votes</span></span>\n'
            f'                <span class="rc-spine" role="img" aria-label="{yes} yes, {no} no, '
            f'{other} not voting">'
            f'<span class="s-yes" data-n="{yes}" style="width:{pct(yes)}%"></span>'
            f'<span class="s-no" data-n="{no}" style="width:{pct(no)}%"></span>'
            f'<span class="s-other" data-n="{other}" style="width:{pct(other)}%"></span></span>\n'
            f'                <span class="rc-legend">'
            f'<span class="l-yes"><i></i><b>{yes:,}</b> yes</span>'
            f'<span class="l-no"><i></i><b>{no}</b> no</span>'
            f'<span class="l-other"><i></i><b>{other}</b> not voting</span></span>\n'
            f'              </span>\n'
            f'              {split_meter(split)}\n'
            f'            </span>\n'
            f'            <a class="rc-go" href="{href}">Full voting record</a>\n'
            f"          </li>"
        )
    return "\n".join(cards)


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


MONTHS_SHORT = tuple(m[:3] for m in MONTHS)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def us_date(iso: str) -> str:
    """CT.fmtDate as an en-US browser prints it: "Oct 8, 2026"."""
    try:
        d = date.fromisoformat((iso or "")[:10])
    except ValueError:
        return esc(iso)
    return f"{MONTHS_SHORT[d.month - 1]} {d.day}, {d.year}"


def load_optional(name: str) -> dict | None:
    path = DOCS / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def render_claims(d: dict) -> str:
    """The fact checks, as the cards render() in docs/claims.html draws them,
    minus the folded evidence and the filters, which are the script's."""
    claims = d.get("claims") or []
    if not claims:
        return ""
    verdicts = {v["key"]: v for v in d.get("verdicts") or []}
    cards = []
    for c in claims:
        v = verdicts.get(c.get("verdict"), {"label": c.get("verdict")})
        tone = VERDICT_TONE.get(c.get("verdict"), "cool")
        cid = esc(c["id"])
        updated = f" · updated {us_date(c['updated'])}" if c.get("updated") else ""
        quote = (f'<blockquote class="cl-quote">&ldquo;{esc(c["claim"])}&rdquo;</blockquote>'
                 if c.get("summary") else "")
        cards.append(
            f'          <article class="cl-card" id="{cid}" aria-labelledby="{cid}-h">'
            f'<div class="cl-card-top">{verdict_pill(c.get("verdict"), v.get("label"))}'
            f'<span class="cl-checked">Checked {us_date(c.get("checked"))}{updated}</span></div>'
            f'<h3 class="cl-claim" id="{cid}-h"><a href="/claims/{cid}.html">'
            f'{esc(c.get("summary") or c["claim"])}</a></h3>{quote}'
            f'<p class="cl-ruling v2-tone-{tone}">{esc(c.get("ruling"))}</p>'
            f'<p class="cl-acts"><a class="v2-btn v2-btn--primary" href="/claims/{cid}.html">'
            'Read the full check <span aria-hidden="true">→</span></a></p></article>')
    n = len(claims)
    return (
        '      <div class="cl-page">\n'
        '        <header class="cl-masthead">\n'
        f'          <p class="v2-label">Eagle Mountain &middot; {esc(d.get("eyebrow") or "Fact checks")}</p>\n'
        f'          <h1>{esc(d.get("title"))}</h1>\n'
        f'          <p class="cl-sub">{esc(d.get("sub"))}</p>\n'
        f'          <p class="cl-standing"><b>{n} claim{"" if n == 1 else "s"} checked</b>'
        f'<span>Last reviewed {us_date(d.get("updated"))}</span></p>\n'
        '        </header>\n'
        '        <section class="cl-list" aria-labelledby="cl-list-h">\n'
        '          <h2 class="cl-h2" id="cl-list-h">Each check, with its sources</h2>\n'
        + "\n".join(cards) + "\n        </section>\n      </div>")


# ---------------------------------------------------------------------------
# The home page. Each function mirrors the render function of the same name in
# docs/index.html; change one, change the other.
# ---------------------------------------------------------------------------

def vote_split(motion: dict) -> tuple[int, int]:
    yes = no = 0
    for v in motion.get("votes") or []:
        x = str(v.get("vote") or "").lower()
        if x == "yes":
            yes += 1
        elif x in ("no", "nay"):
            no += 1
    return yes, no


def classify_outcome(outcome: str) -> str:
    o = (outcome or "").lower()
    if re.search(r"passed|approved|carried", o):
        return "passed"
    if re.search(r"fail|denied|not pass", o):
        return "failed"
    return "other"


def render_votes(data: dict, body_id: str = "city-council") -> str:
    motions = sorted(data.get("motions") or [],
                     key=lambda m: (m.get("date") or "", m.get("id") or 0), reverse=True)
    notable = [m for m in motions if m.get("significance") == "notable"]
    headed = [m for m in motions if m.get("headline") and m.get("significance") != "notable"]
    rest = [m for m in motions if not m.get("headline")]
    rows = []
    for m in (notable + headed + rest)[:6]:
        cls = classify_outcome(m.get("outcome"))
        yes, no = vote_split(m)
        word = {"passed": "Passed", "failed": "Failed"}.get(cls, "Recorded")
        count = f" {yes}–{no}" if yes + no else ""
        tone = {"passed": "ok", "failed": "bad"}.get(cls, "cool")
        href = f'/meetings/{m.get("date")}-{body_id}-{m.get("meeting_id")}.html#m{m.get("id")}'
        summary = f'<span class="v2-vote-sum">{esc(m["summary"])}</span>' if m.get("summary") else ""
        rows.append(
            f'            <li><a href="{esc(href)}"><span class="v2-vote-meta">'
            f'<span>{us_date(m.get("date"))}</span>'
            f'<span class="v2-vote-out v2-tone-{tone}">{word}{count}</span></span>'
            f'<span class="v2-vote-title">{esc(m.get("headline") or "Motion as recorded")}</span>'
            f"{summary}</a></li>")
    return "\n".join(rows)


def render_council(data: dict, today: str, body_id: str = "city-council") -> str:
    current = [m for m in data.get("members") or [] if is_current_member(m, today)]
    chair = next((m for m in current if re.search(r"mayor|chair", m.get("role") or "", re.I)), None)
    ordered = ([chair] if chair else []) + [m for m in current if m is not chair]
    return "\n".join(
        f'          <li><a href="/members/{body_id}/{esc(m["id"])}.html">'
        f'<span class="v2-avatar" aria-hidden="true">{esc(initials(m.get("name")))}</span>'
        f'<span><span class="v2-person-name">{esc(m.get("name"))}</span>'
        f'<span class="v2-person-role">{esc(m.get("role") or "Member")}</span></span></a></li>'
        for m in ordered)


def render_checks(d: dict | None) -> str:
    claims = (d or {}).get("claims") or []
    verdicts = {v["key"]: v for v in (d or {}).get("verdicts") or []}
    recent = sorted(claims, key=lambda c: c.get("checked") or "", reverse=True)[:4]
    return "\n".join(
        f'              <li><a href="/claims/{esc(c["id"])}.html">'
        f'<span class="v2-pill v2-tone-{VERDICT_TONE.get(c.get("verdict"), "cool")}">'
        f'{esc(verdicts.get(c.get("verdict"), {}).get("label") or c.get("verdict"))}</span>'
        f'<span class="v2-list-title">{esc(c.get("summary") or c.get("claim"))}</span></a></li>'
        for c in recent)


def money(n: float) -> str:
    return f"${n:,.2f}"


def render_tax(tax: dict | None) -> str:
    rates = (tax or {}).get("rates") or {}
    rate = rates.get("adopted") or rates.get("proposed")
    home = (rate or {}).get("home_example") or {}
    if not rate or not home.get("increase"):
        return ""
    adopted = bool(rates.get("adopted"))
    when = f" {us_date(rate['date'])}" if rate.get("date") else ""
    value = money(home["market_value"]).removesuffix(".00")
    return (
        f'<span class="v2-now-lab">Property tax &middot; {"adopted" if adopted else "proposed"}{when}</span>'
        f'<span class="v2-now-head">The council {"raised" if adopted else "has proposed raising"} '
        'the city&rsquo;s property tax rate</span>'
        f'<span class="v2-now-fig num">+{esc(money(home["increase"]))} <small>a year</small></span>'
        f'<span class="v2-now-note">on the city&rsquo;s own example home, a {esc(value)} '
        'primary residence</span>'
        '<span class="v2-now-cta">Work out your own home <span aria-hidden="true">→</span></span>')


def render_meet(feed: dict | None, today: str, body_id: str = "city-council") -> str:
    """The next meeting, without the "in N days", which only the script can
    know on the day it is read."""
    events = ((feed or {}).get("bodies") or {}).get(body_id) or []
    ahead = sorted((e for e in events if (e.get("date") or "") >= today),
                   key=lambda e: (e["date"], e.get("start") or ""))
    if not ahead:
        return ""
    ev = ahead[0]
    d = date.fromisoformat(ev["date"])
    comment = next((s for s in ev.get("sessions") or [] if s.get("public_comment")), None)
    when = f'{WEEKDAYS[d.weekday()]}, {MONTHS[d.month - 1]} {d.day}'
    return (
        f'<span class="v2-cal" aria-hidden="true"><span>{MONTHS_SHORT[d.month - 1]}</span>'
        f'<b>{d.day}</b></span><span class="v2-meet-text">'
        '<span class="v2-now-lab">Next meeting</span>'
        f'<span class="v2-meet-when">{esc(when)}'
        f'{", " + esc(ev["start_label"]) if ev.get("start_label") else ""}</span>'
        + (f'<span class="v2-meet-place">Public comment at {esc(comment["start_label"])}</span>'
           if comment and comment.get("start_label") else "")
        + (f'<span class="v2-meet-place">{esc(ev["location"])}</span>' if ev.get("location") else "")
        + f'<span class="v2-now-cta">{"See the agenda" if ev.get("agenda_posted") else "Meeting details"}'
        ' <span aria-hidden="true">→</span></span></span>')


def set_hidden(source: str, element_id: str, hidden: bool) -> str:
    """Add or drop the `hidden` attribute on the one tag carrying this id."""
    pattern = re.compile(rf'<[a-z]+\b[^>]*\bid="{re.escape(element_id)}"[^>]*>')
    match = pattern.search(source)
    if not match:
        raise SystemExit(f"index.html: no element with id={element_id}")
    tag = re.sub(r"\s+hidden(?=[\s>])", "", match.group(0))
    if hidden:
        tag = tag[:-1] + " hidden>"
    return source[:match.start()] + tag + source[match.end():]


def swap(source: str, pattern: str, replacement: str) -> str:
    updated, n = re.subn(pattern, lambda _: replacement, source, count=1, flags=re.S)
    if n != 1:
        raise SystemExit(f"index.html: couldn't find {pattern}")
    return updated


def prerender_home(source: str, data: dict, today: str) -> str:
    tax = load_optional("data.tax.json")
    claims = load_optional("data.claims.json")
    feed = load_optional("data.upcoming.json")

    tax_html = render_tax(tax)
    meet_html = render_meet(feed, today)
    checks_html = render_checks(claims)
    council_html = render_council(data, today)

    for name, container, body, indent in (
        ("votes", r'(<ol class="v2-votes" id="votes">)(.*?)(</ol>)', render_votes(data), "            "),
        ("council", r'(<ul class="v2-people" id="council">)(.*?)(</ul>)', council_html, "          "),
        ("checks", r'(<ul class="v2-list" id="checks">)(.*?)(</ul>)', checks_html, "              "),
        ("tax", r'(<a class="v2-home-tax" id="now-tax"[^>]*>)(.*?)(</a>)', tax_html, "                "),
        ("meet", r'(<a class="v2-home-meet" id="now-meet"[^>]*>)(.*?)(</a>)', meet_html, "                "),
    ):
        if body:
            source = splice(source, re.compile(container, re.S), body, indent)

    source = set_hidden(source, "now-tax", not tax_html)
    source = set_hidden(source, "now-meet", not meet_html)
    source = set_hidden(source, "now", not (tax_html or meet_html))
    source = set_hidden(source, "checks-section", not checks_html)
    source = set_hidden(source, "council-section", not council_html)

    if claims and claims.get("claims"):
        source = swap(source, r'(<span class="v2-door-where" id="door-claims">)[^<]*(</span>)',
                      f'<span class="v2-door-where" id="door-claims">Fact Checks: '
                      f'{len(claims["claims"])} checked so far</span>')
    meetings = sorted(data.get("meetings") or [], key=lambda m: m.get("date") or "")
    if meetings:
        last = meetings[-1]
        source = swap(source, r'<a href="[^"]*" id="door-meeting">',
                      f'<a href="/meetings/{last["date"]}-city-council-{last["id"]}.html" '
                      'id="door-meeting">')
        source = swap(source, r'(<span class="v2-door-where" id="door-meeting-where">)[^<]*(</span>)',
                      '<span class="v2-door-where" id="door-meeting-where">Latest minutes: '
                      f'{us_date(last["date"])}</span>')
    if data.get("generated_at"):
        source = swap(source, r'<span id="generated-at">[^<]*</span>',
                      f'<span id="generated-at">{us_date(data["generated_at"][:10])}</span>')
    return source


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
    # The roster's cards are built from <span>s, not <div>s, precisely so this
    # non-greedy close lands on the container and not on the first card.
    "members.html": re.compile(r'(<ul id="roster" class="rc-roster">)(.*?)(</ul>)', re.S),
    "motions.html": re.compile(r'(<div id="rows"[^>]*>)(.*?)(</div>)', re.S),
    # Empty in the source but for the generated block, which is all <div>s and
    # <section>s, so this closes on the </div> right before </main>.
    "claims.html": re.compile(r'(<div id="content" aria-busy="true">)(.*?)(</div>)(?=\s*</main>)', re.S),
}

INDENTS = {"meetings.html": "      ", "members.html": "        ",
           "motions.html": "      ", "claims.html": "      "}


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
        "claims.html": render_claims(load_optional("data.claims.json") or {}),
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

    # The home page isn't one container but several, plus a few attributes.
    path = DOCS / "index.html"
    source = path.read_text(encoding="utf-8")
    updated = prerender_home(source, data, today)
    if updated == source:
        print("index.html already current")
    elif args.check:
        print("index.html is stale", file=sys.stderr)
        stale = True
    else:
        path.write_text(updated, encoding="utf-8")
        print(f"wrote index.html ({len(updated) - len(source):+,d} bytes)")

    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
