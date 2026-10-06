"""
The redesign's page chrome: the "who runs this" strip, the header, the tab bar
and the footer, as static markup for pages styled by docs/v2.css.

Destinations still come from ingest/nav.py and nowhere else — this module only
decides how the redesign draws them. A page built here needs no site.js to get
its navigation: the bar and the menu are in the HTML, so they survive a load
where no script runs, which is also what a link-preview crawler sees.

The strip is the point of the module. Most readers arrive from a link someone
sent them, on a page in the middle of the site, and the first thing they need
to know is that this is not the city's own website.
"""

from __future__ import annotations

import html

from .nav import NAV, TABBAR, Item

CONTACT = "civicrollcall@gmail.com"
FONT_PRELOAD = "/fonts/public-sans-400-800-latin.woff2"


def esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


# Stroke icons for the tab bar, keyed by the section's href. 24px box, drawn in
# currentColor so the active colour comes from the link.
_ICON = {
    "index.html": '<path d="M4 11l8-6 8 6v8a1 1 0 0 1-1 1h-4v-6h-6v6H5a1 1 0 0 1-1-1z"/>',
    "meetings.html": '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>',
    "members.html": '<circle cx="9" cy="9" r="3.2"/><path d="M3.5 19c.8-3 3-4.5 5.5-4.5s4.7 1.5 5.5 4.5"/><circle cx="17" cy="8" r="2.5"/><path d="M16.5 13.5c2 .2 3.5 1.5 4 4"/>',
    "claims.html": '<circle cx="12" cy="12" r="8"/><path d="M8.5 12.3l2.4 2.4 4.6-5"/>',
    "finances.html": '<path d="M5 20V10M10 20V5M15 20v-8M20 20v-5"/>',
    "about.html": '<circle cx="12" cy="12" r="8"/><path d="M12 11v5M12 8h0"/>',
}
_DEFAULT_ICON = '<circle cx="12" cy="12" r="8"/>'

# A roll call: three names, each with its vote ticked off.
MARK = ('<span class="v2-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" '
        'stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M4 6.5l1.6 1.6L8.5 5M4 12.5l1.6 1.6 2.9-3.1M4 18.5l1.6 1.6 2.9-3.1'
        'M12 7h8M12 13h6M12 19h7"/></svg></span>')


def icon(href: str) -> str:
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            f'{_ICON.get(href, _DEFAULT_ICON)}</svg>')


def _section_of(page: str) -> Item | None:
    for item in NAV:
        if item.href == page or page in item.aliases:
            return item
    return None


def head_assets(css_version: str) -> str:
    """The font preload and the one stylesheet. Nothing from site.css."""
    return (f'<link rel="preload" href="{FONT_PRELOAD}" as="font" type="font/woff2" crossorigin />\n'
            f'    <link rel="stylesheet" href="/fonts.css?v={esc(css_version)}" />\n'
            f'    <link rel="stylesheet" href="/v2.css?v={esc(css_version)}" />')


def strip() -> str:
    return ('<div class="v2-strip"><div class="v2-wrap">'
            # The long sentence on wider screens; on a phone the strip keeps to
            # one line so the page itself starts higher up.
            '<span><span class="v2-strip-long">An independent record of Eagle Mountain city '
            'government, kept by a resident. </span><span class="v2-strip-short">Kept by a '
            'resident. </span><strong>Not run by the city.</strong></span>'
            '<a href="/about.html">What is this?</a>'
            '</div></div>')


def _attrs(item: Item, page: str, current: Item | None) -> str:
    """aria-current on the page itself, a class on its section otherwise (one
    "you are here" per list), and the body marker site.js reads to drop a
    council-only section for another board (applyBodyNav)."""
    out = []
    if item.href == page:
        out.append('aria-current="page" class="is-section"')
    elif current is not None and item.href == current.href:
        out.append('class="is-section"')
    if item.body_scoped:
        out.append('data-nav-body="city-council"')
    return (" " + " ".join(out)) if out else ""


def _dropdown(item: Item, top_link: str, page: str) -> str:
    """A section with pages inside it opens a menu of them on hover or keyboard
    focus, so a reader on a computer or tablet can go straight to Budget or
    Votes without stopping at the section page first. Pure CSS (v2.css), so it
    works where no script runs. The section link still goes to the section,
    which keeps a tap on a touch screen that has no hover doing what it did.

    The wrapper carries the body marker as well as the link, so site.js
    removing a council-only section takes its menu with it. The menu's own
    links are class="v2-nav-sub" and never take aria-current: the header has
    one "you are here", and it belongs to the section."""
    body = ' data-nav-body="city-council"' if item.body_scoped else ""
    subs = "".join(
        f'<li><a class="v2-nav-sub{" is-here" if c.href == page else ""}" href="/{c.href}">'
        f'{esc(c.label)}</a></li>'
        for c in item.children)
    return (f'<div class="v2-nav-item"{body}>{top_link}'
            f'<ul class="v2-nav-menu" aria-label="{esc(item.label)}">{subs}</ul></div>')


def header(section: str) -> str:
    """Brand plus the desktop menu. `section` is the page being built; a page
    inside a section (a fact check passes "claims.html") lights that section."""
    current = _section_of(section)
    links = []
    for item in NAV:
        top_link = f'<a href="/{item.href}"{_attrs(item, section, current)}>{esc(item.label)}</a>'
        links.append(_dropdown(item, top_link, section) if item.children else top_link)
    return ('<header class="v2-header"><div class="v2-wrap">'
            '<a class="v2-brand" href="/index.html">'
            f'{MARK}<span><span class="v2-brand-name">Civic Roll Call</span>'
            '<span class="v2-brand-place">Eagle Mountain, Utah</span></span></a>'
            f'<nav class="v2-nav" aria-label="Main">{"".join(links)}</nav>'
            '</div></header>')


def tabbar(section: str) -> str:
    current = _section_of(section)
    links = []
    for item in TABBAR:
        links.append(f'<a href="/{item.href}"{_attrs(item, section, current)}>{icon(item.href)}'
                     f'<span>{esc(item.tab_label)}</span></a>')
    return f'<nav class="v2-tabbar" aria-label="Main (mobile)">{"".join(links)}</nav>'


def top(page: str) -> str:
    """Everything above <main> on a redesigned page."""
    return f"{strip()}\n{header(page)}"


def bottom(page: str) -> str:
    """Everything after <main>: the footer, then the phone tab bar."""
    return f"{footer()}\n{tabbar(page)}"


def footer() -> str:
    links = "".join(f'<a href="/{item.href}">{esc(item.label)}</a>' for item in NAV)
    return ('<footer class="v2-footer"><div class="v2-wrap">'
            f'<nav aria-label="Everything on this site">{links}'
            '<a href="/definitions.html">Definitions</a></nav>'
            '<p>Civic Roll Call is published independently by an Eagle Mountain resident, '
            'with no affiliation to the city, any candidate or any campaign. '
            f'Corrections and tips: <a href="mailto:{CONTACT}">{CONTACT}</a></p>'
            '</div></footer>')
