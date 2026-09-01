"""
Email a finished meeting summary as soon as it exists.

The last step of scripts/transcribe_meeting.sh: once a meeting has been
transcribed (diarized, in --cloud mode), summarized and published, this mails a
copy — the summary rendered into the message body, with the .summary.md and the
full .txt transcript attached — to whoever is on the recipient list.

Run:
  python -m ingest.mail_summary <EVENT_ID> [--body city-council]
                                [--to a@b.com,c@d.com] [--dry-run]

Nothing is configured in the repo. Settings come from the environment first,
then from a keyfile of KEY=VALUE lines (blank lines and #comments ignored):

  ~/.config/council-tracking/mail.env      (override with COUNCIL_MAIL_KEYFILE)

  COUNCIL_MAIL_TO      comma-separated recipients. NO RECIPIENT = NO MAIL:
                       with this unset the whole step is skipped silently, so
                       the pipeline behaves exactly as it did before.
  COUNCIL_SMTP_USER    SMTP login (e.g. civicrollcall@gmail.com)
  COUNCIL_SMTP_PASS    SMTP password. For Gmail this must be a 16-character
                       App Password (myaccount.google.com → Security → 2-Step
                       Verification → App passwords), NOT the account password.
  COUNCIL_MAIL_FROM    optional From: header; defaults to COUNCIL_SMTP_USER
  COUNCIL_SMTP_HOST    optional; default smtp.gmail.com
  COUNCIL_SMTP_PORT    optional; default 587 (STARTTLS). 465 uses implicit TLS.

Keep the keyfile out of the repo and chmod 600 — it holds a live credential.
In GitHub Actions the same names arrive as repository secrets.

Exit status: 0 when the mail is sent, and 0 when no recipient is configured
(deliberately off). 1 only when sending was wanted and something went wrong, so
a caller can warn without ever failing a meeting over an email.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html import escape
from pathlib import Path

from . import bodies

ROOT = Path(__file__).resolve().parent.parent
SITE = "https://civicrollcall.com"
PORTAL_MEDIA = "https://eaglemountainut.portal.civicclerk.com/event/{id}/media"
DEFAULT_KEYFILE = Path.home() / ".config" / "council-tracking" / "mail.env"

# Gmail rejects anything over 25 MB; stay well under and drop the transcript
# rather than the whole message if a marathon meeting ever gets close.
MAX_ATTACH_BYTES = 20 * 1024 * 1024


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
def _read_keyfile(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        # Tolerate `export FOO="bar"` — that is how people write these by hand.
        k = k.strip().removeprefix("export ").strip()
        out[k] = v.strip().strip("'\"")
    return out


def load_config() -> dict[str, str]:
    """Environment wins over the keyfile; the keyfile fills the gaps."""
    keyfile = Path(os.environ.get("COUNCIL_MAIL_KEYFILE") or DEFAULT_KEYFILE)
    cfg = _read_keyfile(keyfile)
    for key in (
        "COUNCIL_MAIL_TO",
        "COUNCIL_MAIL_FROM",
        "COUNCIL_SMTP_USER",
        "COUNCIL_SMTP_PASS",
        "COUNCIL_SMTP_HOST",
        "COUNCIL_SMTP_PORT",
    ):
        val = os.environ.get(key)
        if val:
            cfg[key] = val

    # Google shows an App Password as four spaced groups ("abcd efgh ijkl mnop")
    # and most people paste it that way; SMTP wants the bare 16 characters.
    # Only that exact shape is collapsed, so a real password containing spaces
    # is left alone.
    pw = cfg.get("COUNCIL_SMTP_PASS", "")
    if re.fullmatch(r"(?:[a-z]{4} ){3}[a-z]{4}", pw, re.I):
        cfg["COUNCIL_SMTP_PASS"] = pw.replace(" ", "")
    return cfg


def _recipients(raw: str) -> list[str]:
    return [a.strip() for a in re.split(r"[,;]", raw or "") if a.strip()]


# --------------------------------------------------------------------------
# locating the meeting's files
# --------------------------------------------------------------------------
def find_meeting(event_id: str, body_id: str) -> dict:
    """Resolve the transcript stem for an event id, or raise."""
    body = bodies.get_body(body_id)
    src = ROOT / "data" / "transcripts" / body_id
    matches = sorted(src.glob(f"*__{event_id}.summary.md"))
    if not matches:
        raise FileNotFoundError(
            f"no summary for event {event_id} in {src} — nothing to email"
        )
    summary = matches[-1]
    stem = summary.name[: -len(".summary.md")]
    date = stem.split("__", 1)[0]
    url = f"{SITE}/meetings.html?id={event_id}"
    if body_id != bodies.default_body()["id"]:
        url += f"&body={body_id}"
    return {
        "body": body,
        "stem": stem,
        "date": date,
        "event_id": event_id,
        "summary": summary,
        "transcript": src / f"{stem}.txt",
        "page_url": url,
        "media_url": PORTAL_MEDIA.format(id=event_id),
    }


# --------------------------------------------------------------------------
# markdown → HTML
# --------------------------------------------------------------------------
# Deliberately small: it only has to render the shapes the summary prompt asks
# for — headings, bold lead-ins, bullet lists, and the Decisions / Meeting map
# tables. Anything it doesn't recognize falls through as an escaped paragraph,
# and the plain-text alternative carries the original Markdown regardless.
_INLINE = (
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"<strong>\1</strong>"),
    (re.compile(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.S), r"<em>\1</em>"),
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)"), r'<a href="\2">\1</a>'),
)


def _inline(text: str) -> str:
    out = escape(text)
    for pattern, repl in _INLINE:
        out = pattern.sub(repl, out)
    return out


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def markdown_to_html(md: str) -> str:
    lines = md.splitlines()
    html: list[str] = []
    para: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if para:
            html.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()
        if bullets:
            items = "".join(f"<li>{_inline(b)}</li>" for b in bullets)
            html.append(f"<ul>{items}</ul>")
            bullets.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush()
            i += 1
            continue

        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            flush()
            # The summary never emits an H1 (the page supplies the title, and so
            # does this message), so levels map straight through; a stray "#"
            # is demoted rather than left to compete with the subject line.
            level = min(max(len(heading.group(1)), 2), 4)
            html.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue

        # A table: a header row, a |---|---| separator, then body rows.
        if (
            stripped.startswith("|")
            and i + 1 < len(lines)
            and re.fullmatch(r"\|[\s:|-]+\|", lines[i + 1].strip() or "")
        ):
            flush()
            head = _split_row(stripped)
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            th = "".join(f"<th>{_inline(c)}</th>" for c in head)
            body = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                for r in rows
            )
            html.append(
                f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"
            )
            continue

        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if bullet:
            if para:
                flush()
            bullets.append(bullet.group(1))
            i += 1
            continue

        if bullets:
            flush()
        para.append(stripped)
        i += 1

    flush()
    return "\n".join(html)


STYLE = """
body { margin:0; padding:24px; background:#f6f7f9;
       font:15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
       color:#1c1f23; }
.wrap { max-width:760px; margin:0 auto; background:#fff; border:1px solid #e3e6ea;
        border-radius:10px; padding:28px 32px; }
h1 { font-size:21px; margin:0 0 4px; }
.meta { color:#5b6570; font-size:13px; margin:0 0 18px; }
.meta a { color:#1a5fb4; }
h2 { font-size:17px; margin:26px 0 8px; padding-top:14px; border-top:1px solid #eceff2; }
h3 { font-size:15px; margin:20px 0 6px; }
p { margin:0 0 12px; }
ul { margin:0 0 14px 20px; padding:0; }
li { margin:0 0 6px; }
table { border-collapse:collapse; width:100%; margin:0 0 16px; font-size:13px; }
th, td { border:1px solid #dde1e6; padding:6px 9px; text-align:left; vertical-align:top; }
th { background:#f1f3f6; font-weight:600; }
code { background:#f1f3f6; padding:1px 4px; border-radius:3px; font-size:13px; }
.foot { margin-top:26px; padding-top:14px; border-top:1px solid #eceff2;
        color:#7a838d; font-size:12px; }
"""


def build_message(meeting: dict, sender: str, to: list[str]) -> EmailMessage:
    label = meeting["body"]["label"]
    md = meeting["summary"].read_text(encoding="utf-8")
    words = len(meeting["transcript"].read_text(encoding="utf-8").split()) \
        if meeting["transcript"].is_file() else 0

    title = f"{label} — {meeting['date']}"
    links = (
        f"Page:      {meeting['page_url']}\n"
        f"Recording: {meeting['media_url']}\n"
    )

    msg = EmailMessage()
    msg["Subject"] = f"[Civic Roll Call] {title} — summary published"
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="civicrollcall.com")

    plain = (
        f"{title}\n{'=' * len(title)}\n\n"
        f"{links}"
        f"Transcript: {words:,} words, attached in full.\n\n"
        "--------------------------------------------------------------\n\n"
        f"{md}\n"
    )
    msg.set_content(plain)

    body_html = markdown_to_html(md)
    msg.add_alternative(
        "<style>" + STYLE + "</style>"
        '<div class="wrap">'
        f"<h1>{escape(title)}</h1>"
        f'<p class="meta">'
        f'<a href="{escape(meeting["page_url"])}">Read on the site</a> · '
        f'<a href="{escape(meeting["media_url"])}">Watch the recording</a>'
        + (f" · transcript {words:,} words, attached" if words else "")
        + "</p>"
        f"{body_html}"
        '<p class="foot">Sent automatically when the transcript and summary '
        "finished publishing. The summary is machine-written from the recording "
        "and is not the official minutes.</p>"
        "</div>",
        subtype="html",
    )

    total = 0
    for path in (meeting["summary"], meeting["transcript"]):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if total + len(data) > MAX_ATTACH_BYTES:
            print(f"  (skipping attachment {path.name}: message would be too large)")
            continue
        total += len(data)
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (ctype or "text/plain").partition("/")
        msg.add_attachment(
            data, maintype=maintype, subtype=subtype or "plain", filename=path.name
        )
    return msg


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------
def send(msg: EmailMessage, cfg: dict[str, str]) -> None:
    host = cfg.get("COUNCIL_SMTP_HOST") or "smtp.gmail.com"
    port = int(cfg.get("COUNCIL_SMTP_PORT") or 587)
    user = cfg["COUNCIL_SMTP_USER"]
    password = cfg["COUNCIL_SMTP_PASS"]

    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=120)
    else:
        server = smtplib.SMTP(host, port, timeout=120)
    with server:
        if port != 465:
            server.starttls()
        server.login(user, password)
        server.send_message(msg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Email a published meeting summary.")
    ap.add_argument("event_id", help="CivicClerk event id")
    ap.add_argument("--body", default="city-council", help="body id")
    ap.add_argument("--to", default="", help="override the recipient list")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="build the message and print who it would go to, but don't send",
    )
    args = ap.parse_args(argv)

    cfg = load_config()
    to = _recipients(args.to or cfg.get("COUNCIL_MAIL_TO", ""))
    if not to:
        print("  no COUNCIL_MAIL_TO configured — skipping the email step.")
        return 0

    meeting = find_meeting(args.event_id, args.body)
    sender = cfg.get("COUNCIL_MAIL_FROM") or cfg.get("COUNCIL_SMTP_USER") or ""
    if args.dry_run:
        msg = build_message(meeting, sender or "unset@example.com", to)
        print(f"  would send '{msg['Subject']}'")
        print(f"    from: {sender or '(unset)'}")
        print(f"    to:   {', '.join(to)}")
        print(f"    attachments: {[p.get_filename() for p in msg.iter_attachments()]}")
        return 0

    missing = [k for k in ("COUNCIL_SMTP_USER", "COUNCIL_SMTP_PASS") if not cfg.get(k)]
    if missing:
        print(
            f"  recipients are configured but {' and '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not — no email sent.",
            file=sys.stderr,
        )
        return 1

    msg = build_message(meeting, sender, to)
    try:
        send(msg, cfg)
    except smtplib.SMTPAuthenticationError as e:
        print(
            f"  SMTP login refused ({e.smtp_code}). For Gmail, COUNCIL_SMTP_PASS "
            "must be an App Password, not the account password.",
            file=sys.stderr,
        )
        return 1
    except Exception as e:  # noqa: BLE001 — an email must never sink a meeting
        print(f"  email failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"  emailed {meeting['stem']} to {', '.join(to)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
