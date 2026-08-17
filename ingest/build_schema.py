"""
Regenerate the JSON-LD blocks that have to stay in step with page content.

Most of the site's structured data is a fixed handful of lines and lives inline
in the page <head> where you can read it next to the <meta> tags it describes.
The glossary is the exception: definitions.html carries 83 entries and gains
one every few weeks, and a hand-maintained DefinedTermSet would be wrong within
a month. So this reads the terms back out of the rendered markup — the visible
page is the source of truth, not a parallel list — and rewrites the block
between the marker comments in place.

Terms are emitted as name + url only. The definitions themselves are already in
the HTML as text, and repeating all 83 of them in JSON-LD added 35KB to a 78KB
page to tell crawlers something the page already said out loud.

Run:  python -m ingest.build_schema
      python -m ingest.build_schema --check    # exit 1 if the block is stale
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DEFINITIONS = DOCS / "definitions.html"

CNAME = DOCS / "CNAME"
DEFAULT_HOST = "civicrollcall.com"

BEGIN = "    <!-- BEGIN generated:definitions-schema (ingest/build_schema.py) -->"
END = "    <!-- END generated:definitions-schema -->"

# <div class="def" id="slug"><dt>Term<a class="def-mark" …></a></dt> …
DEF_BLOCK = re.compile(r'<div class="def" id="([^"]+)">(.*?)</div>', re.S)
DT = re.compile(r"<dt>(.*?)</dt>", re.S)
ANCHOR = re.compile(r"<a\b.*?</a>", re.S)
TAG = re.compile(r"<[^>]+>")


def site_base() -> str:
    """Origin the absolute URLs hang off, no trailing slash."""
    host = DEFAULT_HOST
    if CNAME.exists():
        lines = CNAME.read_text(encoding="utf-8").strip().splitlines()
        if lines and lines[0].strip():
            host = lines[0].strip()
    return f"https://{host}"


def read_terms(source: str) -> list[tuple[str, str]]:
    """Every (slug, term) pair in the glossary, in page order."""
    terms: list[tuple[str, str]] = []
    for slug, body in DEF_BLOCK.findall(source):
        found = DT.search(body)
        if not found:
            continue
        # The pilcrow permalink lives inside the <dt>; it isn't part of the name.
        name = ANCHOR.sub("", found.group(1))
        name = html.unescape(TAG.sub("", name)).strip()
        if name:
            terms.append((slug, name))
    return terms


def render_block(terms: list[tuple[str, str]], base: str) -> str:
    doc = {
        "@context": "https://schema.org",
        "@type": "DefinedTermSet",
        "@id": f"{base}/definitions.html#glossary",
        "name": "Eagle Mountain City Terms Explained",
        "description": ("Plain-English definitions of the words Eagle Mountain "
                        "City uses in its agendas, notices and motions."),
        "url": f"{base}/definitions.html",
        "inLanguage": "en-US",
        "publisher": {"@id": f"{base}/#publisher"},
        "hasDefinedTerm": [
            {
                "@type": "DefinedTerm",
                "name": name,
                "url": f"{base}/definitions.html#{slug}",
                "inDefinedTermSet": f"{base}/definitions.html#glossary",
            }
            for slug, name in terms
        ],
    }
    body = json.dumps(doc, indent=2, ensure_ascii=False)
    indented = "\n".join(f"    {line}" for line in body.splitlines())
    return (f'{BEGIN}\n    <script type="application/ld+json">\n'
            f"{indented}\n    </script>\n{END}")


def splice(source: str, block: str) -> str:
    """Replace the marked region, or insert it just before </head>."""
    start = source.find(BEGIN)
    if start != -1:
        stop = source.find(END, start)
        if stop == -1:
            raise SystemExit("found the BEGIN marker but no END marker")
        return source[:start] + block + source[stop + len(END):]

    head_close = source.find("  </head>")
    if head_close == -1:
        raise SystemExit("definitions.html has no </head> to insert before")
    return source[:head_close] + block + "\n" + source[head_close:]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--check", action="store_true",
                    help="don't write; exit 1 if the file would change")
    args = ap.parse_args(argv)

    if not DEFINITIONS.exists():
        print("docs/definitions.html is missing", file=sys.stderr)
        return 1

    source = DEFINITIONS.read_text(encoding="utf-8")
    terms = read_terms(source)
    if not terms:
        print("no glossary terms parsed; leaving the file alone", file=sys.stderr)
        return 1

    updated = splice(source, render_block(terms, site_base()))
    if updated == source:
        print(f"definitions.html already current ({len(terms)} terms)")
        return 0
    if args.check:
        print(f"definitions.html schema is stale ({len(terms)} terms)",
              file=sys.stderr)
        return 1

    DEFINITIONS.write_text(updated, encoding="utf-8")
    print(f"wrote docs/definitions.html schema ({len(terms)} terms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
