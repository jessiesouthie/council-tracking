"""
Email the agenda for a meeting that hasn't happened yet.

The forward-looking twin of ingest.mail_summary. That one goes out after the
fact, with the summary and the transcript of a meeting already held. This one
goes out when the city posts an agenda, while there is still time to read it and
turn up: session times, which session takes public comment, and every item with
the plain-English line the site shows.

  python -m ingest.mail_agenda                       # next posted city council agenda
  python -m ingest.mail_agenda --body planning-commission
  python -m ingest.mail_agenda --event 729           # a particular meeting
  python -m ingest.mail_agenda --dry-run             # print the message, send nothing
  python -m ingest.mail_agenda --to a@b.com          # override the recipient list

Reads docs/data.upcoming.json, so run it after ingest.build_upcoming and
ingest.summarize_agenda: an item with no cached plain-English line is sent with
the city's own wording and nothing else, which reads worse than waiting.

Configuration, recipients and the SMTP credential all come from mail_summary,
which means one keyfile covers both:

  ~/.config/council-tracking/mail.env      (override with COUNCIL_MAIL_KEYFILE)

No recipient configured is not an error, same as the summary mail: the step
prints why it did nothing and exits 0.

Nothing on this agenda has happened. The message says "will consider" and never
"approved", and the procedural furniture (the gavel, the pledge, adjournment) is
counted at the end rather than listed, so what a resident might actually come
for is not buried in it.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html import escape
from pathlib import Path

from . import bodies, mail_summary

ROOT = Path(__file__).resolve().parent.parent
UPCOMING = ROOT / "docs" / "data.upcoming.json"
SITE = "https://civicrollcall.com"

# The agenda's own furniture. The parse already flags these; they are counted in
# the footer so the item numbers in the message still match the printed agenda.
# (see PROCEDURAL_RE in build_upcoming.py)


def _pretty_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}"


def find_meeting(body_id: str, event_id: str = "") -> dict:
    """The next meeting of a body whose agenda is posted, or a named one."""
    if not UPCOMING.is_file():
        raise FileNotFoundError(
            f"no {UPCOMING.relative_to(ROOT)} — run build_upcoming first"
        )
    data = json.loads(UPCOMING.read_text(encoding="utf-8"))
    events = data.get("bodies", {}).get(body_id) or []
    if event_id:
        for ev in events:
            if str(ev.get("id")) == str(event_id):
                return ev
        raise LookupError(f"event {event_id} is not on the {body_id} calendar")
    for ev in events:
        if ev.get("agenda_posted") and ev.get("agenda"):
            return ev
    raise LookupError(f"no posted agenda on the {body_id} calendar yet")


def _sessions(ev: dict) -> dict[str, dict]:
    return {s.get("start", ""): s for s in ev.get("sessions", [])}


def _session_label(sess: dict | None) -> str:
    if not sess:
        return ""
    label = sess.get("label") or "Session"
    out = f"{label}, {sess.get('start_label', '')}".strip().rstrip(",")
    if sess.get("public_comment"):
        out += " (public comment)"
    return out


def _ordered(ev: dict) -> list[tuple[dict | None, list[dict]]]:
    """Agenda headings grouped under the session they sit in, in printed order."""
    sessions = _sessions(ev)
    out: list[tuple[dict | None, list[dict]]] = []
    for heading in ev.get("agenda", []):
        if heading.get("procedural"):
            continue
        sess = sessions.get(heading.get("session", ""))
        if out and out[-1][0] is sess:
            out[-1][1].append(heading)
        else:
            out.append((sess, [heading]))
    return out


def _procedural_count(ev: dict) -> int:
    return sum(1 for h in ev.get("agenda", []) if h.get("procedural"))


def _page_url(body_id: str) -> str:
    url = f"{SITE}/meetings.html"
    if body_id != bodies.default_body()["id"]:
        url += f"?body={body_id}"
    return url


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def _heading_line(heading: dict) -> str:
    bits = [f"{heading.get('number', '')}. {heading.get('title', '')}".strip()]
    if heading.get("group"):
        bits.append(f"[{heading['group']}]")
    return " ".join(bits)


def render_plain(ev: dict, body: dict, page_url: str) -> str:
    title = f"{body['label']}, {_pretty_date(ev['date'])}"
    lines = [title, "=" * len(title), ""]
    for sess in ev.get("sessions", []):
        lines.append(_session_label(sess))
    if not ev.get("sessions"):
        lines.append(f"Starts {ev.get('start_label', '')}")
    lines += [
        ev.get("location", ""),
        "",
        f"Agenda posted {_pretty_date(ev.get('agenda_posted_on', ''))}.",
        "",
        f"On the site: {page_url}",
        f"The agenda itself: {ev.get('agenda_url') or ev.get('url', '')}",
        "",
        "-" * 62,
        "",
    ]

    for sess, headings in _ordered(ev):
        label = _session_label(sess)
        if label:
            lines += [label.upper(), ""]
        for heading in headings:
            lines.append(_heading_line(heading))
            if heading.get("note"):
                lines.append(f"  {heading['note']}")
            for item in heading.get("items", []):
                kind = item.get("kind", "")
                num = item.get("number", "")
                lines.append(f"  {num} {kind}".rstrip())
                if item.get("plain"):
                    lines.append(f"    {item['plain']}")
                lines.append(f"    Agenda wording: {item.get('title', '')}")
                if item.get("background"):
                    lines.append(f"    {item['background']}")
            lines.append("")

    procedural = _procedural_count(ev)
    if procedural:
        lines.append(
            f"{procedural} procedural headings (call to order, the pledge, "
            "reports, adjournment) are left out above and numbered on the "
            "printed agenda."
        )
    lines += [
        "",
        "Nothing here has been decided. The plain-English line under each item "
        "says what the body is being asked to do, and is machine-written from "
        "the agenda, not from the city.",
    ]
    return "\n".join(lines) + "\n"


EXTRA_STYLE = """
.sess { margin:26px 0 10px; padding-top:14px; border-top:1px solid #eceff2;
        font-size:15px; font-weight:600; color:#1c1f23; }
.sess .cmt { font-weight:400; color:#1a5fb4; }
.head { margin:16px 0 6px; font-size:14px; font-weight:600; }
.head .num { color:#7a838d; font-weight:400; }
.tag { display:inline-block; margin-left:6px; padding:1px 6px; border-radius:9px;
       background:#f1f3f6; color:#5b6570; font-size:11px; font-weight:600;
       vertical-align:1px; letter-spacing:.02em; }
.item { margin:0 0 12px 16px; padding-left:12px; border-left:2px solid #e3e6ea; }
.item .kind { color:#7a838d; font-size:11px; font-weight:600; letter-spacing:.03em; }
.item .plain { margin:1px 0 3px; }
.item .legal { color:#5b6570; font-size:12.5px; margin:0; }
.item .bg { color:#7a838d; font-size:12.5px; margin:4px 0 0; }
"""


def render_html(ev: dict, body: dict, page_url: str) -> str:
    title = f"{body['label']}, {_pretty_date(ev['date'])}"
    when = " · ".join(
        filter(None, [_session_label(s) for s in ev.get("sessions", [])])
    ) or ev.get("start_label", "")

    out = [
        "<style>" + mail_summary.STYLE + EXTRA_STYLE + "</style>",
        '<div class="wrap">',
        f"<h1>{escape(title)}</h1>",
        '<p class="meta">'
        f"{escape(when)}<br>{escape(ev.get('location', ''))}<br>"
        f"Agenda posted {escape(_pretty_date(ev.get('agenda_posted_on', '')))} · "
        f'<a href="{escape(page_url)}">see it on the site</a> · '
        f'<a href="{escape(ev.get("agenda_url") or ev.get("url", ""))}">'
        "the agenda itself</a></p>",
    ]

    for sess, headings in _ordered(ev):
        label = sess.get("label") if sess else ""
        if label:
            bits = f"{escape(label)}, {escape(sess.get('start_label', ''))}"
            if sess.get("public_comment"):
                bits += ' <span class="cmt">public comment</span>'
            out.append(f'<p class="sess">{bits}</p>')
        for heading in headings:
            tag = (
                f'<span class="tag">{escape(heading["group"])}</span>'
                if heading.get("group")
                else ""
            )
            out.append(
                f'<p class="head"><span class="num">{escape(heading.get("number", ""))}.'
                f"</span> {escape(heading.get('title', ''))}{tag}</p>"
            )
            if heading.get("note"):
                out.append(f'<p class="legal">{escape(heading["note"])}</p>')
            for item in heading.get("items", []):
                out.append('<div class="item">')
                head = " ".join(
                    filter(None, [item.get("number", ""), item.get("kind", "")])
                )
                out.append(f'<p class="kind">{escape(head)}</p>')
                if item.get("plain"):
                    out.append(f'<p class="plain">{escape(item["plain"])}</p>')
                out.append(f'<p class="legal">{escape(item.get("title", ""))}</p>')
                if item.get("background"):
                    out.append(f'<p class="bg">{escape(item["background"])}</p>')
                out.append("</div>")

    procedural = _procedural_count(ev)
    foot = (
        "Nothing here has been decided. The line under each item says what the "
        "body is being asked to do, and is machine-written from the agenda, not "
        "from the city."
    )
    if procedural:
        foot = (
            f"{procedural} procedural headings (call to order, the pledge, "
            f"reports, adjournment) are left out above, which is why the "
            f"numbering skips. {foot}"
        )
    out += [f'<p class="foot">{foot}</p>', "</div>"]
    return "\n".join(out)


def build_message(ev: dict, body: dict, sender: str, to: list[str]) -> EmailMessage:
    page_url = _page_url(body["id"])
    msg = EmailMessage()
    msg["Subject"] = (
        f"[Civic Roll Call] {body['label']}, {_pretty_date(ev['date'])}: agenda posted"
    )
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="civicrollcall.com")
    msg.set_content(render_plain(ev, body, page_url))
    msg.add_alternative(render_html(ev, body, page_url), subtype="html")
    return msg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Email a posted agenda.")
    ap.add_argument("--body", default="city-council", help="body id")
    ap.add_argument("--event", default="", help="CivicClerk event id")
    ap.add_argument("--to", default="", help="override the recipient list")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the message and who it would go to, but don't send",
    )
    args = ap.parse_args(argv)

    cfg = mail_summary.load_config()
    to = mail_summary._recipients(args.to or cfg.get("COUNCIL_MAIL_TO", ""))
    if not to:
        print("  no COUNCIL_MAIL_TO configured — skipping the email step.")
        return 0

    body = bodies.get_body(args.body)
    ev = find_meeting(args.body, args.event)
    sender = cfg.get("COUNCIL_MAIL_FROM") or cfg.get("COUNCIL_SMTP_USER") or ""

    if args.dry_run:
        msg = build_message(ev, body, sender or "unset@example.com", to)
        print(f"  would send '{msg['Subject']}'")
        print(f"    from: {sender or '(unset)'}")
        print(f"    to:   {', '.join(to)}")
        print()
        print(render_plain(ev, body, _page_url(body["id"])))
        return 0

    missing = [k for k in ("COUNCIL_SMTP_USER", "COUNCIL_SMTP_PASS") if not cfg.get(k)]
    if missing:
        print(
            f"  recipients are configured but {' and '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not — no email sent.",
            file=sys.stderr,
        )
        return 1

    msg = build_message(ev, body, sender, to)
    try:
        mail_summary.send(msg, cfg)
    except Exception as e:  # noqa: BLE001 — an email must never sink an ingest
        print(f"  email failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"  emailed the {ev['date']} {body['label']} agenda to {', '.join(to)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
