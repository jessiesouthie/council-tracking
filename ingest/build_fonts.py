"""
Pull the site's typefaces off Google Fonts and serve them ourselves.

Every page used to open its <head> with two preconnects and a stylesheet on
fonts.googleapis.com. That is two DNS lookups, two TLS handshakes and a
render-blocking round trip to another origin before the first glyph can be
asked for — and then a second hop to fonts.gstatic.com for the file itself.
Self-hosting collapses all of it into a request to an origin the browser has
already connected to, which is the single cheapest thing this site can do for
its Largest Contentful Paint. It also stops handing every reader's IP and
referrer to a third party on every page view, which for a site about local
government is worth something on its own.

What it does:

  1. Fetch each css2 URL the pages ask for, with a browser User-Agent — send
     anything else and Google serves TrueType instead of woff2.
  2. Keep the latin and latin-ext subsets and drop the rest. The site is in
     English; the Cyrillic, Greek and Vietnamese cuts are dead weight, and the
     unicode-range on each @font-face means a browser would never fetch them
     anyway — they'd just sit in the repository.
  3. Download every remaining woff2 into docs/fonts/ and write docs/fonts.css
     with the same @font-face rules pointed at the local copies.

The families are declared in one file even though no page uses all of them.
@font-face is a declaration, not a fetch: a browser downloads only the faces its
CSS actually renders with, so budget.html still pulls Fraunces and a page with
no book matter on it never pulls Newsreader.

Run:  python -m ingest.build_fonts
      python -m ingest.build_fonts --check    # verify every file is present
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
FONT_DIR = DOCS / "fonts"
OUT_CSS = DOCS / "fonts.css"

# Google serves woff2 only to browsers it recognizes. A urllib default UA gets
# TrueType, which is roughly twice the size and unnecessary everywhere we ship.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# The exact css2 requests the pages make today. Keep in step with the <head> of
# each page — build_fonts --check won't catch a family that was added there and
# never added here, it will only catch a file that has gone missing.
SOURCES = [
    # definitions, claims, the agenda cards — Newsreader for book matter
    "https://fonts.googleapis.com/css2?"
    "family=Newsreader:ital,opsz,wght@0,6..72,300..600;1,6..72,300..500&display=swap",
    # every page — the site pair: text in Instrument Sans, figures in Plex Mono
    "https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700"
    "&family=IBM+Plex+Mono:wght@400;500;600&display=swap",
    # index, budget, finances — Fraunces pinned to one instance
    "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght,SOFT,WONK@9..144,700,0,1"
    "&display=swap",
]

KEEP_SUBSETS = {"latin", "latin-ext"}

# Each @font-face is preceded by a /* subset */ comment in Google's output.
FACE = re.compile(r"/\*\s*([\w\-\[\]]+)\s*\*/\s*(@font-face\s*\{.*?\})", re.S)
SRC_URL = re.compile(r"url\((https://fonts\.gstatic\.com/[^)]+)\)")
PROP = re.compile(r"([\w-]+)\s*:\s*([^;]+);")


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def face_key(block: str) -> tuple:
    """Identity of a @font-face, for dropping the copies shared between files."""
    props = dict(PROP.findall(block))
    return (props.get("font-family", "").strip(),
            props.get("font-style", "").strip(),
            props.get("font-weight", "").strip(),
            props.get("unicode-range", "").strip())


def local_name(family: str, style: str, weight: str, subset: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", family.strip().strip("'\"").lower()).strip("-")
    weight = weight.replace(" ", "-")
    italic = "-italic" if style == "italic" else ""
    return f"{slug}{italic}-{weight}-{subset}.woff2"


def collect() -> list[dict]:
    """Every distinct face we intend to ship, in the order first seen.

    Faces are deduplicated twice over, on two different keys. First on the face
    itself, so a family named by two of the stylesheets yields one rule and not
    two. Then on the remote URL, because Google
    answers Instrument Sans at four weights with four @font-face rules that all
    point at the same variable file — four rules is correct and they all stay,
    but downloading that file four times under four names would put 120KB of
    identical bytes in the repository.
    """
    seen: set[tuple] = set()
    by_remote: dict[str, str] = {}
    faces: list[dict] = []
    for url in SOURCES:
        css = fetch(url).decode("utf-8")
        for subset, block in FACE.findall(css):
            if subset not in KEEP_SUBSETS:
                continue
            key = face_key(block)
            if key in seen:
                continue
            remote = SRC_URL.search(block)
            if not remote:
                print(f"  no woff2 url in a {key[0]} face, skipped", file=sys.stderr)
                continue
            seen.add(key)
            family, style, weight, _ = key
            href = remote.group(1)
            if href not in by_remote:
                by_remote[href] = local_name(family, style, weight, subset)
            faces.append({
                "block": block,
                "subset": subset,
                "remote": href,
                "file": by_remote[href],
            })
    return faces


def render_css(faces: list[dict]) -> str:
    head = [
        "/* Generated by ingest/build_fonts.py — do not edit by hand.",
        " *",
        " * Self-hosted copies of the faces the pages used to fetch from Google.",
        " * Linked from each <head> ahead of site.css, for the same reason the",
        " * Google stylesheet was: the font request must not wait on 340KB of CSS",
        " * to parse first.",
        " *",
        " * Only latin and latin-ext are shipped. font-display: swap is Google's",
        " * own setting, kept — text paints immediately in the fallback and",
        " * reflows once, rather than hiding until the webfont lands.",
        " */",
        "",
    ]
    body = []
    for face in faces:
        block = SRC_URL.sub(f"url(fonts/{face['file']})", face["block"])
        block = re.sub(r"\n\s*", "\n  ", block.strip())
        block = re.sub(r"\n\s*\}$", "\n}", block)
        body.append(f"/* {face['subset']} */\n{block}")
    return "\n".join(head) + "\n\n".join(body) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--check", action="store_true",
                    help="don't download; verify docs/fonts.css and its files")
    args = ap.parse_args(argv)

    if args.check:
        if not OUT_CSS.exists():
            print("docs/fonts.css is missing; run without --check", file=sys.stderr)
            return 1
        wanted = re.findall(r"url\(fonts/([^)]+)\)", OUT_CSS.read_text(encoding="utf-8"))
        missing = [name for name in wanted if not (FONT_DIR / name).exists()]
        if missing:
            for name in missing:
                print(f"missing docs/fonts/{name}", file=sys.stderr)
            return 1
        print(f"fonts.css OK ({len(wanted)} faces, all files present)")
        return 0

    try:
        faces = collect()
    except (urllib.error.URLError, OSError) as exc:
        print(f"couldn't reach Google Fonts: {exc}", file=sys.stderr)
        return 1
    if not faces:
        print("no faces collected; leaving the existing files alone", file=sys.stderr)
        return 1

    FONT_DIR.mkdir(parents=True, exist_ok=True)
    # Several faces share a file, so count bytes per file rather than per face.
    written: dict[str, int] = {}
    for face in faces:
        name = face["file"]
        if name in written:
            continue
        target = FONT_DIR / name
        if target.exists():
            written[name] = target.stat().st_size
            continue
        try:
            blob = fetch(face["remote"])
        except (urllib.error.URLError, OSError) as exc:
            print(f"  {name}: download failed ({exc})", file=sys.stderr)
            return 1
        target.write_bytes(blob)
        written[name] = len(blob)
        print(f"  {name} ({len(blob):,} bytes)")

    OUT_CSS.write_text(render_css(faces), encoding="utf-8")
    print(f"wrote docs/fonts.css ({len(faces)} faces over "
          f"{len(written)} files, {sum(written.values()):,} bytes of woff2)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
