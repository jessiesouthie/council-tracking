"""
Give every fact check a page of its own, and a picture for the link preview.

A claim is the thing on this site people are most often sent, and until now
each one lived at claims.html#<id>. Facebook, iMessage and every other link
unfurler drop the fragment before they fetch, so a shared check previewed as
the generic Claims page with the square app icon, and the reader who tapped it
landed at the top of a page about sixty phone screens long. This writes:

    /claims/<id>.html          the check on its own, readable with no script
    /claims/og/<id>.png        1200x630: the claim and the verdict, which is
                               what shows in the feed before anyone taps

Both come from docs/data.claims.json, the same file claims.html renders, so
the list page and the single page cannot say different things. The pages use
the redesign's chrome and stylesheet (ingest/chrome_v2.py, docs/v2.css).

claims.html keeps working as the index. Its rows link here, and an old
claims.html#<id> link forwards to the page for that id.

Run:  python -m ingest.build_claim_pages
      python -m ingest.build_claim_pages --check    # exit 1 if any page is stale
      python -m ingest.build_claim_pages --prune    # delete pages for retired ids
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

from . import chrome_v2 as chrome
from .chrome_v2 import esc

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DATA = DOCS / "data.claims.json"
OUT_DIR = DOCS / "claims"
OG_DIR = OUT_DIR / "og"
MEETINGS = DOCS / "meetings"
FONTS = Path(__file__).resolve().parent / "assets" / "fonts"
CSS_VERSION = "20261003b"
# The "Ask about this site" widget. These pages don't load site.js, which is
# what mounts it everywhere else, so they link it themselves. Kept equal to
# the tag in site.js mountAgent() by docs/claims.test.mjs.
AGENT_VERSION = "20261003a"
BASE = "https://civicrollcall.com"

MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")

# The four tones a verdict can take. Same grouping claims.html uses (its TONE
# table), so a verdict is the same colour on the list and on its own page.
TONE = {
    "accurate": "ok",
    "incomplete": "warn",
    "misleading": "bad",
    "unsupported": "bad",
    "inaccurate": "bad",
    "out-of-date": "cool",
    "unresolved": "cool",
}

# Each verdict carries a shape as well as a colour, so it reads in greyscale
# and to a reader who can't separate the hues. Paths in a 24px box.
GLYPH = {
    "accurate": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "incomplete": '<path d="M12 6v8M12 18h0"/>',
    "misleading": '<path d="M7 7l10 10M17 7L7 17"/>',
    "unsupported": '<path d="M7 7l10 10M17 7L7 17"/>',
    "inaccurate": '<path d="M7 7l10 10M17 7L7 17"/>',
    "out-of-date": '<circle cx="12" cy="12" r="7"/><path d="M12 8v4.5l3 2"/>',
    "unresolved": '<path d="M9 9.5a3 3 0 1 1 4.2 2.7c-.8.4-1.2 1-1.2 1.8v.5M12 18h0"/>',
}

# The same rating claims.html gives each verdict in its ClaimReview.
RATING = {"accurate": 5, "incomplete": 4, "unresolved": 3, "out-of-date": 2,
          "misleading": 2, "unsupported": 2, "inaccurate": 1}

# What the second half of a check is called depends on the verdict: "where it
# goes wrong" is the wrong heading over the caveat on a claim found accurate.
WRONG_HEADING = {
    "accurate": "Worth knowing",
    "incomplete": "What it leaves out",
    "unresolved": "What isn't settled",
    "out-of-date": "What changed",
}
WRONG_DEFAULT = "Where it goes wrong"

RELATED_MAX = 4

# Bump when render_og() draws differently, so every og:image URL changes and
# Facebook drops the pictures it has cached.
OG_LAYOUT = 1


def fmt_date(iso: str) -> str:
    try:
        d = date.fromisoformat((iso or "")[:10])
    except ValueError:
        return esc(iso)
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


def glyph(verdict: str, stroke: str = "#fff", width: str = "2.6") -> str:
    return (f'<svg viewBox="0 0 24 24" fill="none" stroke="{stroke}" stroke-width="{width}" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            f'{GLYPH.get(verdict, GLYPH["unresolved"])}</svg>')


def pill(verdict: str, label: str) -> str:
    tone = TONE.get(verdict, "cool")
    return (f'<span class="v2-pill v2-tone-{tone}">{glyph(verdict, "currentColor", "2.8")}'
            f'{esc(label)}</span>')


def meeting_pages() -> dict[int, tuple[str, bool]]:
    """Event id -> (page filename, has a transcript page)."""
    out: dict[int, tuple[str, bool]] = {}
    if not MEETINGS.is_dir():
        return out
    names = {p.name for p in MEETINGS.glob("*.html")}
    for name in names:
        hit = re.match(r"^\d{4}-\d{2}-\d{2}-.+-(\d+)\.html$", name)
        if hit and not name.endswith("-transcript.html"):
            out[int(hit.group(1))] = (name, name[:-5] + "-transcript.html" in names)
    return out


def site_href(href: str) -> str:
    """read_more links are written relative to docs/; these pages sit a level
    down, so they need the leading slash."""
    if re.match(r"^[a-z]+:", href) or href.startswith("/"):
        return href
    return "/" + href


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def source_block(s: dict, meetings: dict[int, tuple[str, bool]]) -> str:
    links = []
    when = fmt_date(s["date"]) if s.get("date") else ""
    if s.get("motion") is not None:
        links.append(f'<a class="v2-chip" href="/motions.html?id={esc(s["motion"])}">'
                     f'The vote{", " + when if when else ""} <span aria-hidden="true">→</span></a>')
    elif s.get("meeting") is not None:
        page = meetings.get(int(s["meeting"]))
        if page:
            name, has_transcript = page
            links.append(f'<a class="v2-chip" href="/meetings/{esc(name)}">'
                         f'The meeting{", " + when if when else ""} <span aria-hidden="true">→</span></a>')
            if has_transcript:
                links.append(f'<a class="v2-chip" href="/meetings/{esc(name[:-5])}-transcript.html">'
                             'Read the transcript <span aria-hidden="true">→</span></a>')
        else:
            links.append(f'<a class="v2-chip" href="/meetings.html?id={esc(s["meeting"])}">'
                         f'The meeting{", " + when if when else ""} <span aria-hidden="true">→</span></a>')
    if s.get("doc"):
        links.append(f'<a class="v2-chip" href="{esc(s["doc"]["url"])}" rel="nofollow noopener">'
                     f'{esc(s["doc"]["title"])} <span aria-hidden="true">↗</span></a>')

    who = esc(s.get("speaker") or "")
    if when and not (s.get("motion") is not None or s.get("meeting") is not None):
        who = f"{who}, {when}" if who else when
    parts = ['<figure class="v2-source">']
    if s.get("quote"):
        parts.append(f'<blockquote>&ldquo;{esc(s["quote"])}&rdquo;</blockquote>')
    parts.append("<figcaption>")
    if who:
        parts.append(f'<span class="v2-source-who">{who}</span>')
    if s.get("note"):
        parts.append(f'<span class="v2-source-note">{esc(s["note"])}</span>')
    if links:
        parts.append(f'<span class="v2-source-links">{"".join(links)}</span>')
    parts.append("</figcaption></figure>")
    return "".join(parts)


def related(c: dict, claims: list[dict]) -> list[dict]:
    """Other checks a reader of this one is most likely after: the ones filed
    under the same first topic, then any that share a topic at all."""
    first = (c.get("topics") or [None])[0]
    mine = set(c.get("topics") or [])
    same = [o for o in claims if o is not c and (o.get("topics") or [None])[0] == first]
    near = [o for o in claims if o is not c and o not in same and mine & set(o.get("topics") or [])]
    return (same + near)[:RELATED_MAX]


def og_version(c: dict, verdict_label: str, topic_label: str) -> str:
    """A short hash of exactly what the preview image draws. It rides on the
    og:image URL, so when a verdict changes Facebook fetches the new picture
    instead of the one it cached."""
    key = json.dumps([OG_LAYOUT, c.get("summary") or c.get("claim"), verdict_label,
                      topic_label, c.get("verdict")], ensure_ascii=False)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def render_page(c: dict, data: dict, meetings: dict[int, tuple[str, bool]]) -> str:
    verdicts = {v["key"]: v for v in data.get("verdicts", [])}
    cats = {k["key"]: k for k in data.get("categories", [])}
    claims = data.get("claims", [])
    v = verdicts.get(c["verdict"], {"label": c["verdict"], "note": ""})
    tone = TONE.get(c["verdict"], "cool")
    first = (c.get("topics") or [None])[0]
    topic = cats.get(first, {}).get("label", "")
    summary = c.get("summary") or c["claim"]
    url = f"{BASE}/claims/{c['id']}.html"
    og = f"{BASE}/claims/og/{c['id']}.png?v={og_version(c, v['label'], topic)}"

    title = f"Fact check: {summary}"
    desc = f"{v['label']}. {c['ruling']}"
    if len(desc) > 300:
        desc = desc[:297].rsplit(" ", 1)[0] + "…"

    figs = "".join(
        f'<div class="v2-fig"><span class="v2-fig-text"><span class="v2-fig-label">{esc(f["label"])}</span>'
        + (f'<span class="v2-fig-note">{esc(f["note"])}</span>' if f.get("note") else "")
        + f'</span><span class="v2-fig-value">{esc(f["value"])}</span></div>'
        for f in c.get("figures") or [])

    sections = []
    if figs:
        sections.append(f'<section class="v2-section" aria-labelledby="h-figs"><h2 id="h-figs">The numbers</h2>'
                        f'<div class="v2-figs">{figs}</div></section>')
    if c.get("true_part"):
        sections.append('<section class="v2-section v2-tone-ok" aria-labelledby="h-true">'
                        '<h2 class="v2-dot-h" id="h-true">What&rsquo;s true</h2>'
                        f'<p>{esc(c["true_part"])}</p></section>')
    if c.get("wrong_part"):
        wtone = "warn" if c["verdict"] in ("accurate", "incomplete", "unresolved", "out-of-date") else "bad"
        heading = WRONG_HEADING.get(c["verdict"], WRONG_DEFAULT)
        sections.append(f'<section class="v2-section v2-tone-{wtone}" aria-labelledby="h-wrong">'
                        f'<h2 class="v2-dot-h" id="h-wrong">{esc(heading)}</h2>'
                        f'<p>{esc(c["wrong_part"])}</p></section>')
    if c.get("detail"):
        paras = "".join(f"<p>{esc(p)}</p>" for p in c["detail"])
        sections.append(f'<section class="v2-section" aria-labelledby="h-detail"><h2 id="h-detail">More detail</h2>{paras}</section>')
    if c.get("provisional"):
        sections.append(f'<p class="v2-note"><strong>Still provisional.</strong> {esc(c["provisional"])}</p>')
    if c.get("sources"):
        srcs = "".join(source_block(s, meetings) for s in c["sources"])
        sections.append('<section class="v2-section" aria-labelledby="h-src"><h2 id="h-src">Where this comes from</h2>'
                        '<p class="v2-check-seen">Every finding here is drawn from an open-meeting recording or a published document. Check it yourself.</p>'
                        f'<div class="v2-sources">{srcs}</div></section>')

    share = ('<section class="v2-share" aria-label="Share this check">'
             f'<a class="v2-btn v2-btn--primary" href="{esc(url)}" data-share data-title="{esc(title)}">'
             '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
             'stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12M7 8l5-5 5 5M5 14v5h14v-5"/></svg>'
             'Share this check</a>'
             f'<button type="button" class="v2-btn v2-btn--quiet" data-copy="{esc(url)}">Copy link</button>'
             '</section>')

    if c.get("read_more"):
        items = "".join(f'<li><a href="{esc(site_href(m["href"]))}">{esc(m["label"])} <span aria-hidden="true">→</span></a></li>'
                        for m in c["read_more"])
        sections_more = (f'<section class="v2-section" aria-labelledby="h-read"><h2 id="h-read">Read more on this site</h2>'
                         f'<ul class="v2-links">{items}</ul></section>')
    else:
        sections_more = ""

    rel = related(c, claims)
    rel_html = ""
    if rel:
        rows = "".join(
            f'<li><a href="/claims/{esc(o["id"])}.html">'
            f'{pill(o["verdict"], verdicts.get(o["verdict"], {"label": o["verdict"]})["label"])}'
            f'<span class="v2-list-title">{esc(o.get("summary") or o["claim"])}</span></a></li>'
            for o in rel)
        heading = f"More checks on {topic.lower()}" if topic else "More fact checks"
        rel_html = (f'<section class="v2-section" aria-labelledby="h-rel"><h2 id="h-rel">{esc(heading)}</h2>'
                    f'<ul class="v2-list">{rows}</ul>'
                    f'<a class="v2-more" href="/claims.html">See all {len(claims)} fact checks →</a></section>')

    quote = ""
    if c.get("summary") and c["summary"] != c["claim"]:
        quote = f'<blockquote class="v2-check-quote">&ldquo;{esc(c["claim"])}&rdquo;</blockquote>'
    seen_bits = [esc(c["claim_note"])] if c.get("claim_note") else []
    if c.get("seen"):
        seen_bits.append(f"Seen: {esc(c['seen'])}.")
    seen = f'<p class="v2-check-seen">{" ".join(seen_bits)}</p>' if seen_bits else ""

    crumb_topic = (f'<span aria-hidden="true">›</span><a href="/claims.html?topic={esc(first)}">{esc(topic)}</a>'
                   if topic else "")

    schema = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "ClaimReview",
                "@id": f"{url}#review",
                "url": url,
                "claimReviewed": summary,
                "datePublished": c.get("checked"),
                "author": {"@id": f"{BASE}/#publisher"},
                "itemReviewed": {
                    "@type": "Claim",
                    "name": c["claim"],
                    "appearance": {"@type": "CreativeWork", "name": c.get("seen") or "In circulation"},
                },
                "reviewRating": {
                    "@type": "Rating",
                    "ratingValue": RATING.get(c["verdict"], 3),
                    "bestRating": 5,
                    "worstRating": 1,
                    "alternateName": v["label"],
                },
            },
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{BASE}/"},
                    {"@type": "ListItem", "position": 2, "name": "Fact checks", "item": f"{BASE}/claims.html"},
                    {"@type": "ListItem", "position": 3, "name": summary, "item": url},
                ],
            },
        ],
    }
    schema_json = json.dumps(schema, ensure_ascii=False, indent=2).replace("</", "<\\/")

    return f"""<!doctype html>
<!-- Generated by ingest/build_claim_pages.py from docs/data.claims.json. Do not edit by hand. -->
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <title>{esc(title)} | Civic Roll Call</title>
    <meta name="description" content="{esc(desc)}" />
    <link rel="canonical" href="{esc(url)}" />
    <meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1" />
    <meta name="theme-color" content="#14202e" />
    <meta property="og:type" content="article" />
    <meta property="og:site_name" content="Civic Roll Call" />
    <meta property="og:url" content="{esc(url)}" />
    <meta property="og:title" content="{esc(title)}" />
    <meta property="og:description" content="{esc(desc)}" />
    <meta property="og:image" content="{esc(og)}" />
    <meta property="og:image:width" content="1200" />
    <meta property="og:image:height" content="630" />
    <meta property="og:image:alt" content="{esc(summary)}: {esc(v['label'])}" />
    <meta name="twitter:card" content="summary_large_image" />
    <meta name="twitter:title" content="{esc(title)}" />
    <meta name="twitter:description" content="{esc(desc)}" />
    <meta name="twitter:image" content="{esc(og)}" />
    <link rel="manifest" href="/manifest.webmanifest" />
    <link rel="icon" href="/icons/icon-192.png" />
    <link rel="apple-touch-icon" href="/icons/icon-192.png" />
    {chrome.head_assets(CSS_VERSION)}
    <script type="application/ld+json">
{schema_json}
    </script>
  </head>
  <body>
    <a class="v2-skip" href="#main">Skip to the fact check</a>
    {chrome.strip()}
    {chrome.header("claims.html")}
    <div class="v2-wrap v2-wrap--read">
      <nav class="v2-crumbs" aria-label="Breadcrumb"><a href="/claims.html">Fact checks</a>{crumb_topic}</nav>
      <main id="main" class="v2-check">
        <section class="v2-check-claim" aria-labelledby="h-claim">
          <p class="v2-label">The claim</p>
          <h1 id="h-claim">{esc(summary)}</h1>
          {quote}
          {seen}
        </section>
        <section class="v2-verdict v2-tone-{tone}" aria-label="Our finding">
          <div class="v2-verdict-head">
            <span class="v2-verdict-icon">{glyph(c["verdict"])}</span>
            <span><span class="v2-label" style="color: inherit">Our finding</span>
              <span class="v2-verdict-label" style="display: block">{esc(v["label"])}</span></span>
          </div>
          <p class="v2-verdict-meaning">{esc(v.get("note", ""))}</p>
          <p class="v2-verdict-ruling">{esc(c["ruling"])}</p>
        </section>
        {"".join(sections)}
        {share}
        {sections_more}
        {rel_html}
        <p class="v2-note">Checked {fmt_date(c.get("checked", ""))}. Nothing goes on the fact-check list unless an open-meeting recording or a published document can settle it: <a href="/claims.html#cl-rules-h">the rules</a>. Spot a mistake? <a href="mailto:{chrome.CONTACT}?subject={esc("Correction: " + summary)}">{chrome.CONTACT}</a></p>
      </main>
    </div>
    {chrome.footer()}
    {chrome.tabbar("claims.html")}
    <script>
      // Share sheet where the phone has one, the clipboard where it doesn't, and
      // a plain link to this page underneath both, so nothing here needs a script.
      document.addEventListener("click", function (e) {{
        var share = e.target.closest("[data-share]");
        if (share && navigator.share) {{
          e.preventDefault();
          navigator.share({{ title: share.dataset.title, url: share.href }}).catch(function () {{}});
          return;
        }}
        var copy = e.target.closest("[data-copy]") || (share && !navigator.share ? share : null);
        if (!copy) return;
        e.preventDefault();
        var url = copy.dataset.copy || copy.href;
        var done = function () {{
          var was = copy.lastChild.textContent;
          copy.lastChild.textContent = "Link copied";
          setTimeout(function () {{ copy.lastChild.textContent = was; }}, 2200);
        }};
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          navigator.clipboard.writeText(url).then(done, function () {{ location.href = url; }});
        }} else {{
          location.href = url;
        }}
      }});
    </script>
    <script src="/agent.js?v={AGENT_VERSION}" defer></script>
  </body>
</html>
"""


# ---------------------------------------------------------------------------
# The preview image
# ---------------------------------------------------------------------------

OG_TONE = {
    "ok":   {"fg": (30, 107, 63),  "bg": (227, 242, 232), "edge": (140, 199, 161), "solid": (30, 107, 63)},
    "warn": {"fg": (90, 59, 0),    "bg": (255, 246, 224), "edge": (226, 180, 74),  "solid": (138, 90, 0)},
    "bad":  {"fg": (143, 36, 24),  "bg": (251, 228, 225), "edge": (231, 154, 144), "solid": (163, 39, 27)},
    "cool": {"fg": (62, 74, 87),   "bg": (230, 234, 239), "edge": (185, 195, 206), "solid": (91, 102, 117)},
}
INK = (20, 32, 46)
MUTED = (74, 88, 104)
SURFACE = (244, 246, 248)


def _font(weight: int, size: int):
    from PIL import ImageFont
    return ImageFont.truetype(str(FONTS / f"PublicSans-{weight}.ttf"), size)


def _wrap(draw, text: str, font, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _draw_glyph(draw, verdict: str, cx: int, cy: int, r: int) -> None:
    """The verdict's shape, in white on its tone's disc, at radius r."""
    w = max(4, r // 5)
    s = r / 12  # the SVG glyphs are drawn in a 24px box centred on 12,12
    pt = lambda x, y: (cx + (x - 12) * s, cy + (y - 12) * s)
    white = (255, 255, 255)
    if verdict == "accurate":
        draw.line([pt(5, 12.5), pt(9.5, 17), pt(19, 7.5)], fill=white, width=w, joint="curve")
    elif verdict == "incomplete":
        draw.line([pt(12, 6), pt(12, 14)], fill=white, width=w)
        draw.ellipse([pt(10.6, 16.6), pt(13.4, 19.4)], fill=white)
    elif verdict in ("misleading", "unsupported", "inaccurate"):
        draw.line([pt(7, 7), pt(17, 17)], fill=white, width=w)
        draw.line([pt(17, 7), pt(7, 17)], fill=white, width=w)
    elif verdict == "out-of-date":
        draw.ellipse([pt(5, 5), pt(19, 19)], outline=white, width=w - 1)
        draw.line([pt(12, 8), pt(12, 12.5), pt(15, 14.5)], fill=white, width=w - 1, joint="curve")
    else:
        draw.arc([pt(9, 6.5), pt(15, 12.5)], start=180, end=90, fill=white, width=w)
        draw.line([pt(12, 12.5), pt(12, 14.5)], fill=white, width=w)
        draw.ellipse([pt(10.6, 16.6), pt(13.4, 19.4)], fill=white)


def render_og(c: dict, verdict_label: str, topic_label: str) -> bytes:
    import io
    from PIL import Image, ImageDraw

    W, H, PAD = 1200, 630, 72
    tone = OG_TONE[TONE.get(c["verdict"], "cool")]
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, W, 14], fill=INK)

    # Labelled as a claim under test, not a headline: a stranger scrolling past
    # "The tax was rushed through in secret" in 64px type must not read it as
    # this site saying so. The verdict sits right under it for the same reason.
    eyebrow = f"CLAIM WE CHECKED · {topic_label.upper()}" if topic_label else "CLAIM WE CHECKED"
    d.text((PAD, 62), eyebrow, font=_font(700, 26), fill=MUTED)

    # The claim, as large as fits in three lines.
    text = c.get("summary") or c["claim"]
    for size in (64, 58, 52, 46, 40):
        font = _font(800, size)
        lines = _wrap(d, text, font, W - 2 * PAD)
        if len(lines) <= 3:
            break
    y = 118
    for line in lines[:3]:
        d.text((PAD, y), line, font=font, fill=INK)
        y += int(size * 1.15)

    # The verdict pill.
    vfont = _font(800, 38)
    label_w = int(d.textlength(verdict_label, font=vfont))
    ph, r = 76, 25
    px, py = PAD, max(y + 30, 360)
    pw = 16 + 2 * r + 16 + label_w + 30
    d.rounded_rectangle([px, py, px + pw, py + ph], radius=ph // 2, fill=tone["bg"], outline=tone["edge"], width=3)
    cx, cy = px + 16 + r, py + ph // 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=tone["solid"])
    _draw_glyph(d, c["verdict"], cx, cy, r)
    d.text((cx + r + 16, cy), verdict_label, font=vfont, fill=tone["fg"], anchor="lm")

    # The footer band: whose site this is, which is the thing a stranger
    # scrolling past most needs to know.
    band = 96
    d.rectangle([0, H - band, W, H], fill=SURFACE)
    mx, my, ms = PAD, H - band // 2 - 22, 44
    d.rounded_rectangle([mx, my, mx + ms, my + ms], radius=10, fill=INK)
    for i, (x0, x1) in enumerate(((17, 37), (17, 33), (17, 29))):
        yy = my + 12 + i * 10
        d.ellipse([mx + 7, yy - 2.5, mx + 12, yy + 2.5], fill=(255, 255, 255))
        d.line([(mx + x0, yy), (mx + x1, yy)], fill=(255, 255, 255), width=4)
    d.text((mx + ms + 14, H - band // 2), "civicrollcall.com", font=_font(800, 28), fill=INK, anchor="lm")
    d.text((W - PAD, H - band // 2), "Eagle Mountain's city record, not run by the city",
           font=_font(400, 24), fill=(51, 65, 79), anchor="rm")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------

def build(check: bool = False, prune: bool = False) -> int:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    claims = data.get("claims", [])
    verdicts = {v["key"]: v for v in data.get("verdicts", [])}
    cats = {k["key"]: k for k in data.get("categories", [])}
    meetings = meeting_pages()

    ids = [c["id"] for c in claims]
    bad = [i for i in ids if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", i)]
    if bad:
        print(f"claim ids that can't be filenames: {bad}", file=sys.stderr)
        return 1
    if len(set(ids)) != len(ids):
        print("duplicate claim ids", file=sys.stderr)
        return 1

    stale, wrote = [], 0
    if not check:
        OG_DIR.mkdir(parents=True, exist_ok=True)
    for c in claims:
        page = OUT_DIR / f"{c['id']}.html"
        text = render_page(c, data, meetings)
        current = page.read_text(encoding="utf-8") if page.exists() else None
        if current != text:
            if check:
                stale.append(page.relative_to(ROOT))
            else:
                page.write_text(text, encoding="utf-8")
                wrote += 1

        png = OG_DIR / f"{c['id']}.png"
        if check:
            if not png.exists():
                stale.append(png.relative_to(ROOT))
            continue
        v = verdicts.get(c["verdict"], {"label": c["verdict"]})["label"]
        t = cats.get((c.get("topics") or [None])[0], {}).get("label", "")
        img = render_og(c, v, t)
        if not png.exists() or png.read_bytes() != img:
            png.write_bytes(img)
            wrote += 1

    keep = set(ids)
    orphans = [p for p in OUT_DIR.glob("*.html") if p.stem not in keep] + \
              [p for p in OG_DIR.glob("*.png") if p.stem not in keep]
    if orphans:
        if prune and not check:
            for p in orphans:
                p.unlink()
            print(f"pruned {len(orphans)} file(s) for retired claims")
        else:
            for p in orphans:
                print(f"  orphan: {p.relative_to(ROOT)} (run with --prune)", file=sys.stderr)
            if check:
                return 1

    if check:
        if stale:
            for p in stale:
                print(f"  stale: {p}", file=sys.stderr)
            print("run: python -m ingest.build_claim_pages", file=sys.stderr)
            return 1
        print(f"claim pages up to date ({len(claims)})")
        return 0
    print(f"wrote {wrote} file(s) for {len(claims)} claims in {OUT_DIR.relative_to(ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--check", action="store_true", help="exit 1 if any page is stale")
    ap.add_argument("--prune", action="store_true", help="delete pages for retired claim ids")
    args = ap.parse_args(argv)
    return build(check=args.check, prune=args.prune)


if __name__ == "__main__":
    raise SystemExit(main())
