"""
Publish the meetings that haven't happened yet.

Everything else in this repo looks backwards — minutes, votes, transcripts, all
of it after the fact. This is the one forward-looking file: the city's own
calendar, so the site can tell a resident when the council next sits and what
time to turn up.

Source is the same CivicClerk portal the crawl reads. Writes
docs/data.upcoming.json:

  {
    "generated_at": ISO timestamp,
    "source": "Eagle Mountain CivicClerk portal",
    "timezone": "America/Denver",          # what the naive times below mean
    "bodies": {
      "<body id>": [
        {"id", "title", "date": "YYYY-MM-DD",
         "start": "HH:MM", "start_label": "4:00 PM",   # local wall clock
         "location", "url",
         "agenda_posted": bool,
         "agenda_url": direct PDF link,   # when an agenda is posted
         # sessions and items come from the agenda PDF, when one is posted
         "sessions": [{"start", "start_label", "label", "public_comment"}],
         "agenda": [
           {"number": "13", "title": "TRUTH-IN-TAXATION",
            "note": "",                  # the agenda's own blurb under a heading
            "session": "19:00",          # keys into sessions[].start
            "group": "Scheduled Items",  # Consent Agenda / Scheduled Items / null
            "procedural": false,         # the gavel and the pledge, not the business
            "items": [
              {"number": "13.C", "kind": "RESOLUTION/PUBLIC HEARING",
               "title": "...",           # the city's own wording, legalese and all
               "plain": "...",           # one sentence a resident can read
               "time": "", "background": "..."}
            ]}
         ]}
      ]
    }
  }

The "plain" line comes from ingest.summarize_agenda, an LLM step run by hand and
cached in data/meta; this build merges whatever is cached and leaves the rest
empty, so it never needs the model itself.

Times are Eagle Mountain wall-clock with no offset attached, matching how the
API reports them and how the agenda prints them — see civicclerk.LOCAL_TZ.

Run:  python -m ingest.build_upcoming                  # every CivicClerk body
      python -m ingest.build_upcoming --body city-council
      python -m ingest.build_upcoming --horizon 6      # keep 6 meetings ahead
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pdfplumber
import requests

from . import agenda_summaries, bodies, civicclerk

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data.upcoming.json"
AGENDA_CACHE = ROOT / "data" / "agendas"

PORTAL_EVENT = "https://eaglemountainut.portal.civicclerk.com/event/{id}/overview"

# How many scheduled meetings to publish. The calendar is booked a year out, and
# the site only ever shows the front of it, but keeping a handful means the file
# stays useful for a few weeks if an ingest run is missed.
DEFAULT_HORIZON = 6

# Only the next couple of meetings are worth opening an agenda PDF for: agendas
# are posted days, not months, ahead, so anything further out has none.
AGENDA_LOOKAHEAD = 3

# "4:00 PM WORK SESSION - CITY COUNCIL CHAMBERS" — the agenda's own session
# headings. Eagle Mountain runs a work session first and the voting (policy)
# session hours later, and which one a resident wants is the whole question.
# CONFERENCE is in there for the strategic planning conferences, which print the
# same banner ("8:00 AM STRATEGIC PLANNING CONFERENCE") under another name.
SESSION_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*([AP])\.?M\.?\s+([A-Z][A-Z' \-]*(?:SESSION|CONFERENCE))\b",
    re.MULTILINE,
)

# The agenda's own heading for the open-mic slot, numbered as an agenda item
# ("7. PUBLIC COMMENTS"). Anchored to the line start so a passing mention in the
# body text of some other item doesn't count as a comment period.
PUBLIC_COMMENT_RE = re.compile(
    r"^\s*(?:\d+\.\s*)?PUBLIC COMMENT", re.MULTILINE | re.IGNORECASE
)

# --- the agenda's own numbering ------------------------------------------
# Headings are numbered ("13. TRUTH-IN-TAXATION") and the business under them
# is lettered ("13.C. RESOLUTION/PUBLIC HEARING - ...").
HEADING_RE = re.compile(r"^(\d{1,2})\.\s+(\S.*)$")
# The dot after the letter is optional: most agendas print "2.A." but the
# special-session template drops it ("2.A DISCUSSION - Items of Mutual
# Interest"). No heading can be mistaken for an item either way — a heading puts
# a space after its number, and this needs a capital hard against the dot.
ITEM_RE = re.compile(r"^(\d{1,2})\.([A-Z])\.?\s+(\S.*)$")

# An unnumbered all-caps banner that divides the night ("CONSENT AGENDA",
# "SCHEDULED ITEMS"). Two words or more, no trailing punctuation, and — the part
# that actually settles it — a numbered heading on the next line. Without that
# last test a wrapped fragment in a title ("…Terra Smith's Eagle Mountain," /
# "LLC.") reads as a banner and orphans the items under it.
GROUP_RE = re.compile(r"^[A-Z][A-Z0-9 &'/\-]*[A-Z] [A-Z0-9 &'/\-]*[A-Z]$")

# "RESOLUTION/PUBLIC HEARING - A Resolution of…" — the classifier the agenda
# puts in front of an item, which is what says whether the council will vote on
# it, hold a hearing on it, or only talk about it.
KIND_RE = re.compile(r"^([A-Z][A-Z/ ]{2,40}?)\s*[-–]\s*(.*)$")

# "(anticipated time 15 minutes)" — only ever on work-session discussion items.
TIME_RE = re.compile(r"\(anticipated time ([^)]+)\)", re.IGNORECASE)

# The staff write-up under an item.
BACKGROUND_RE = re.compile(r"^BACKGROUND:?\s*", re.IGNORECASE)

# The recorder's certificate at the foot of every agenda: "…was posted on this 13
# day of August, 2026, on the Eagle Mountain City bulletin boards…". This is the
# city attesting when it gave notice, and it is the one date on the page that
# says the meeting was noticed lawfully — worth carrying through.
# Whitespace is \s+ throughout: the certificate wraps mid-sentence in the PDF
# ("was posted on\nthis 13 day of August, 2026"), so a literal space would miss it.
POSTED_ON_RE = re.compile(
    r"posted\s+on\s+this\s+(\d{1,2})\s*(?:st|nd|rd|th)?\s+day\s+of\s+"
    r"([A-Za-z]+),?\s+(\d{4})", re.IGNORECASE
)
MONTHS = ["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"]

# Where the agenda stops being the agenda: the ADA notice, the posting
# certificate and the recorder's signature.
BOILERPLATE_RE = re.compile(
    r"^(THE PUBLIC IS INVITED|CERTIFICATE OF POSTING"
    r"|In accordance with the Americans)", re.IGNORECASE
)

# The machinery of running a meeting rather than the business of it: the gavel,
# the pledge, the manager's standing slot, the vote to go behind closed doors,
# the minutes of meetings this site already carries, the adjournment. Every one
# of these appears on every agenda and none of them is why a resident would come.
# Flagged rather than dropped — the file stays a faithful copy of the agenda, and
# the site decides what to print. PUBLIC COMMENT is deliberately not here: it is
# the one procedural slot that belongs to residents.
PROCEDURAL_RE = re.compile(
    r"^(?:"
    r"call to order"
    r"|pledge of allegiance"
    r"|invocation|moment of silence"
    r"|roll call"
    r"|(?:approval of )?(?:the )?agenda(?: review| approval)?$"
    r"|agenda review"
    r"|city manager(?:'s)?(?: information| report)"
    r"|city council/mayor items"
    r"|city council/mayor'?s business"
    r"|communication items"
    r"|adjourn"
    r"|closed session|executive session"
    r"|minutes$"
    r")", re.IGNORECASE
)


def local_today() -> str:
    """Today in Eagle Mountain, as YYYY-MM-DD."""
    return datetime.now(ZoneInfo(civicclerk.LOCAL_TZ)).strftime("%Y-%m-%d")


def split_event_time(event_date: str) -> tuple[str, str]:
    """('2026-08-18T16:00:00Z') -> ('2026-08-18', '16:00'), no timezone maths.

    The trailing Z is a lie the API tells (civicclerk.LOCAL_TZ); the digits in
    front of it are already Eagle Mountain wall-clock.
    """
    day, _, rest = (event_date or "").partition("T")
    return day[:10], rest[:5]


def label_time(hhmm: str) -> str:
    """'16:00' -> '4:00 PM'. Empty in, empty out."""
    if not re.fullmatch(r"\d{2}:\d{2}", hhmm or ""):
        return ""
    h, m = int(hhmm[:2]), hhmm[3:]
    suffix = "AM" if h < 12 else "PM"
    return f"{h % 12 or 12}:{m} {suffix}"


def format_location(loc: dict | None) -> str:
    """Flatten the API's address record into one line, skipping blanks."""
    if not loc:
        return ""
    parts = [loc.get("address1"), loc.get("address2")]
    return ", ".join(p.strip() for p in parts if (p or "").strip())


def agenda_pdf(body: dict, event: dict, file_entry: dict,
               session: requests.Session) -> Path | None:
    """Fetch (and cache) the agenda PDF for an event. None if it won't download.

    Cached under the file id: CivicClerk issues a new one when an agenda is
    amended, so a re-published agenda is a cache miss rather than a stale hit.
    """
    dest = AGENDA_CACHE / body["id"] / f"{event['id']}__{file_entry['fileId']}.pdf"
    if dest.exists():
        return dest
    try:
        civicclerk.download_file(file_entry["fileId"], dest, session=session)
    except Exception as exc:
        print(f"  ! agenda download failed for event {event['id']}: {exc}")
        return None
    return dest


def agenda_text(pdf_path: Path) -> str:
    """The agenda PDF as plain text, or "" if it won't open.

    Never raises: an unreadable or differently-shaped agenda costs the page its
    session split and its item list, but the meeting's own date, time and place
    still carry it.
    """
    try:
        data = pdf_path.read_bytes()
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as exc:
        print(f"  ! could not read {pdf_path.name}: {exc}")
        return ""


def read_sessions(text: str) -> list[dict]:
    """Pull the session headings ("4:00 PM WORK SESSION") out of an agenda.

    Each session is also checked for a public-comment slot, because that — not
    the meeting's nominal start — is the hour a resident who wants to be heard
    needs to show up for. The flag is read off this agenda rather than assumed
    from how the council usually runs its night.
    """
    matches = list(SESSION_RE.finditer(text))
    out: list[dict] = []
    seen: set[str] = set()
    for i, m in enumerate(matches):
        hh, mm, ampm, name = m.groups()
        h = int(hh) % 12 + (12 if ampm == "P" else 0)
        start = f"{h:02d}:{mm}"
        if start in seen:
            continue
        seen.add(start)
        # Everything up to the next session heading belongs to this one.
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append({
            "start": start,
            "start_label": label_time(start),
            "label": name.title().replace("'S", "'s"),
            "public_comment": bool(PUBLIC_COMMENT_RE.search(text[m.end():end])),
        })
    out.sort(key=lambda s: s["start"])
    return out


def read_posted_on(text: str) -> str:
    """The day the recorder certifies the agenda was posted, as YYYY-MM-DD.

    "" when the certificate is missing or worded differently — the site prints
    nothing rather than guessing at a legal notice date.
    """
    m = POSTED_ON_RE.search(text)
    if not m:
        return ""
    day, month, year = m.group(1), m.group(2).lower(), m.group(3)
    if month not in MONTHS:
        return ""
    return f"{year}-{MONTHS.index(month) + 1:02d}-{int(day):02d}"


def _wrap_join(left: str, right: str) -> str:
    """Rejoin a line the PDF wrapped. "Mid-" + "Valley" is one word; everything
    else gets its space back."""
    if left.endswith("-") and len(left) > 1 and left[-2].isalnum():
        return left + right
    return f"{left} {right}" if left else right


def _split_kind(title: str) -> tuple[str, str]:
    """"RESOLUTION - A Resolution of…" -> ("RESOLUTION", "A Resolution of…")."""
    m = KIND_RE.match(title)
    return (m.group(1).strip(), m.group(2).strip()) if m else ("", title)


def read_agenda(text: str) -> list[dict]:
    """Structure the posted agenda: the numbered headings, and the lettered
    business under each one.

    The agenda is the one document that says what the council is *about* to do,
    and the portal only publishes it as a PDF. This walks that PDF's own
    numbering rather than guessing at the prose: a line is a heading, an item, a
    session banner, a group banner, or the continuation of whichever of those
    came last.

    Returns [] for an agenda that doesn't follow the template — the page falls
    back to linking the PDF, which is where residents were going anyway.
    """
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]

    headings: list[dict] = []
    session: str | None = None   # keys into read_sessions()[].start
    group: str | None = None
    heading: dict | None = None
    item: dict | None = None
    # Everything above the first session banner or numbered heading is the
    # letterhead — the city's name, the date, the address of the chambers — all
    # of which the page already prints for itself.
    #
    # A numbered heading opens the agenda as readily as a session banner does.
    # Requiring the banner cost the special sessions and joint work meetings
    # their whole item list: they run one undivided sitting, so they print no
    # banner to start on, and the parser walked past "1. MAYOR'S WELCOME" and
    # everything under it. Nothing in the letterhead is numbered that way — the
    # street address ("1650 E. Stagecoach Run") has no space after its dot — so
    # starting on a heading doesn't drag the masthead in behind it.
    started = False

    for i, ln in enumerate(lines):
        if BOILERPLATE_RE.match(ln):
            break

        m = SESSION_RE.match(ln)
        if m:
            hh, mm, ampm, _ = m.groups()
            session = f"{int(hh) % 12 + (12 if ampm == 'P' else 0):02d}:{mm}"
            group, heading, item = None, None, None
            started = True
            continue

        m = ITEM_RE.match(ln)
        if m and started:
            number, letter, rest = m.groups()
            item = {"number": f"{number}.{letter}", "kind": "", "title": rest,
                    "time": "", "background": ""}
            # An item whose heading we never saw (a page break landing badly)
            # still gets one, so it can't fall out of the list.
            if heading is None or heading["number"] != number:
                heading = {"number": number, "title": "", "note": "",
                           "session": session, "group": group, "items": []}
                headings.append(heading)
            heading["items"].append(item)
            continue

        m = HEADING_RE.match(ln)
        if m:
            started = True
            heading = {"number": m.group(1), "title": m.group(2), "note": "",
                       "session": session, "group": group, "items": []}
            headings.append(heading)
            item = None
            continue

        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if started and GROUP_RE.match(ln) and HEADING_RE.match(nxt):
            group = ln.title()
            heading, item = None, None
            continue

        if not started:
            continue

        # A continuation line, belonging to whatever was last opened.
        if item is not None:
            if item["background"]:
                item["background"] = _wrap_join(item["background"], ln)
            elif BACKGROUND_RE.match(ln):
                item["background"] = BACKGROUND_RE.sub("", ln)
            else:
                item["title"] = _wrap_join(item["title"], ln)
        elif heading is not None:
            heading["note"] = _wrap_join(heading["note"], ln)

    # Now that every title is whole, pull the classifier and the timebox off it.
    for h in headings:
        h["procedural"] = bool(PROCEDURAL_RE.match(h["title"]))
        for it in h["items"]:
            title = it["title"]
            tm = TIME_RE.search(title)
            if tm:
                it["time"] = tm.group(1).strip()
                title = TIME_RE.sub("", title).strip()
            it["kind"], it["title"] = _split_kind(title)
            # Filled in later by ingest.summarize_agenda, which reads this file.
            it["plain"] = ""
    return [h for h in headings if h["title"] or h["items"]]


def merge_plain(entries: list[dict]) -> int:
    """Fill in each item's plain-English line from the summary cache.

    The cache is written by ingest.summarize_agenda (an LLM step that runs
    locally, never in CI — same arrangement as the motion summaries). Keeping the
    merge here means the weekly rebuild re-attaches every sentence it already has
    instead of blanking them, and an item nobody has summarized yet simply shows
    the city's own wording.
    """
    cache = agenda_summaries.load_cache()
    if not cache:
        return 0
    n = 0
    for entry in entries:
        for head in entry.get("agenda") or []:
            for item in head["items"]:
                got = cache.get(agenda_summaries.item_key(item))
                if got and got.get("plain"):
                    item["plain"] = got["plain"]
                    n += 1
    return n


def build_body(body: dict, *, horizon: int, since: str,
               session: requests.Session) -> list[dict]:
    events = civicclerk.list_upcoming_events(
        since, session=session, category=body["category"], limit=horizon * 3
    )

    out: list[dict] = []
    for ev in events:
        date, start = split_event_time(ev.get("eventDate") or "")
        if not date:
            continue
        entry = {
            "id": ev["id"],
            "title": (ev.get("eventName") or f"{body['label']} Meeting").strip(),
            "date": date,
            "start": start,
            "start_label": label_time(start),
            "location": format_location(ev.get("eventLocation")),
            "url": PORTAL_EVENT.format(id=ev["id"]),
            "agenda_posted": False,
            "agenda_posted_on": "",
            "agenda_url": "",
            "sessions": [],
            "agenda": [],
        }

        agenda = civicclerk.find_agenda(ev)
        if agenda and agenda.get("fileId"):
            entry["agenda_posted"] = True
            entry["agenda_url"] = civicclerk.file_url(agenda["fileId"])
            if len(out) < AGENDA_LOOKAHEAD:
                pdf = agenda_pdf(body, ev, agenda, session)
                if pdf:
                    text = agenda_text(pdf)
                    entry["sessions"] = read_sessions(text)
                    entry["agenda"] = read_agenda(text)
                    entry["agenda_posted_on"] = read_posted_on(text)

        out.append(entry)
        if len(out) >= horizon:
            break

    plain = merge_plain(out)
    for e in out:
        extra = " · ".join(f"{s['label']} {s['start_label']}" for s in e["sessions"])
        n = sum(len(h["items"]) for h in e["agenda"])
        print(f"  {e['date']} {e['start_label']:>8}  {e['title']}"
              + (f"   [{extra}]" if extra else "")
              + (f"   {n} agenda items" if e["agenda"] else ""))
    if plain:
        print(f"  ({plain} items carried a plain-English line over from the cache)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--body", help="single body id (default: every CivicClerk body)")
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON,
                    help=f"meetings to publish per body (default {DEFAULT_HORIZON})")
    ap.add_argument("--since", help="keep meetings on/after this day (default: today)")
    args = ap.parse_args()

    since = args.since or local_today()
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
            entries = build_body(body, horizon=args.horizon, since=since,
                                 session=session)
        except Exception as exc:
            print(f"  ! calendar fetch failed: {exc}", file=sys.stderr)
            continue
        if entries:
            manifest[body["id"]] = entries
        else:
            print("  (nothing scheduled)")

    if not manifest:
        print("no upcoming meetings found; leaving the existing file alone",
              file=sys.stderr)
        return 1

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
    print(f"\nwrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
