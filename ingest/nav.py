"""
The site's destination list, written down once.

Before this file the same list existed in four hand-maintained copies — the
`<nav>` block pasted into twelve pages, the `NAV` constant in
build_meeting_pages.py, the TABBAR arrays in site.js, and the "where to go
instead" list in 404.html — and no two of them agreed. Each had been edited at
a different time for a different reason, so the site gave four different answers
to "what is on here".

So the list lives here, and ingest/build_nav.py writes it into all four.
Nothing below is rendered at runtime: every consumer gets static markup, because
the nav has to survive a load where no JavaScript runs and no JSON arrives.

The shape of the list is the argument the site is making. Meetings, Votes and
Members are the record — republished, traceable to a document the city posted.
Finances is analysis built on top of that record, and projections.html is, in
about.html's own words, "the one page on this site that is not a republication
of anything". Keeping the two apart in the nav is the same distinction about.html
draws in prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Item:
    """One top-level destination.

    `alias` names the other pages that belong to this section — the pages that
    should light this item up without claiming aria-current for themselves.
    site.js reads it from data-nav-alias; see highlightActiveNav().

    `body_scoped` marks a section that only the City Council has. The Planning
    Commission levies no tax and adopts no budget, so carrying Finances into
    that view would be six links to figures that aren't theirs.
    """

    label: str
    href: str
    alias: tuple[str, ...] = ()
    mobile: bool = True
    body_scoped: bool = False
    blurb: str = ""

    @property
    def nav(self) -> str:
        """The filename highlightActiveNav() matches location against."""
        return self.href


# Order is the reading order: home, then the record, then what was built on it,
# then how it was built.
NAV: tuple[Item, ...] = (
    Item(
        label="Home",
        href="index.html",
        blurb="the front page — what changed most recently, and what is coming up.",
    ),
    Item(
        label="Meetings",
        href="meetings.html",
        blurb="every council meeting on file, the motions decided each night, and the transcripts.",
    ),
    # motions.html keeps its filename. It is the target of the homepage search
    # form and of every tag chip on the site, and it has been indexed under that
    # name for months — renaming the file to match the label would break all of
    # it to no reader's benefit. "Votes" is what the page is; "motions" is what
    # the minutes call it, which is why definitions.html has an entry for it.
    Item(
        label="Votes",
        href="motions.html",
        blurb="search the whole roll-call record by topic, member, outcome or year.",
    ),
    Item(
        label="Members",
        href="members.html",
        alias=("member.html",),
        blurb="roll-call totals for each member, and how often each pair voted together.",
    ),
    Item(
        label="Finances",
        href="finances.html",
        alias=("tax.html", "projections.html", "budget.html", "staffing.html"),
        body_scoped=True,
        blurb="the tax rate, the budget, the projections and the payroll behind them.",
    ),
    # Reference, not a destination anyone arrives looking for. It stays off the
    # mobile bar — five is the most a bottom bar can hold before the labels stop
    # being readable — and every page already carries it in the footer.
    Item(
        label="About",
        href="about.html",
        alias=("definitions.html",),
        mobile=False,
        blurb="where the record comes from, and the words the city uses in plain English.",
    ),
)

# The mobile bottom bar. Five items, no overflow sheet: everything on the bar is
# a top-level section, and the one item that isn't on it lives in the footer.
TABBAR: tuple[Item, ...] = tuple(i for i in NAV if i.mobile)

MOBILE_MAX = 5

BEGIN = "BEGIN generated:nav (ingest/build_nav.py)"
END = "END generated:nav"


def _href(item: Item, root: bool) -> str:
    """Root-absolute for pages that can be served from any depth.

    docs/meetings/*.html and 404.html both need this: the rest of the site is
    flat, so a relative href from inside /meetings/ would have to climb out on
    every link, and 404.html is served for whatever path was missed.
    """
    return f"/{item.href}" if root else item.href


def nav_links(root: bool = False, active: str | None = None,
              indent: str = "        ") -> str:
    """The <a> elements inside <nav class="nav">.

    `active` is a filename to mark as the current page. The generated meeting
    pages pass "meetings.html" so the highlight is painted before site.js runs;
    every other page leaves it to highlightActiveNav().
    """
    out = []
    for item in NAV:
        attrs = [f'href="{_href(item, root)}"', f'data-nav="{item.nav}"']
        if item.alias:
            attrs.append(f'data-nav-alias="{" ".join(item.alias)}"')
        if item.body_scoped:
            attrs.append('data-nav-body="city-council"')
        if active and item.nav == active:
            attrs.append('class="active"')
            attrs.append('aria-current="page"')
        out.append(f"{indent}<a {' '.join(attrs)}>{item.label}</a>")
    return "\n".join(out)


def tabbar_js(indent: str = "  ") -> str:
    """The TABBAR array site.js mounts the bottom bar from.

    Hrefs are root-absolute here for the same reason the meeting pages' are: the
    bar is injected into docs/meetings/*.html too, and a relative "meetings.html"
    resolved from there points at /meetings/meetings.html, which has never
    existed. That was a live bug until this list became one list.
    """
    rows = []
    for item in TABBAR:
        parts = [f'href: "/{item.href}"', f'label: "{item.label}"']
        if item.body_scoped:
            parts.append('body: "city-council"')
        rows.append(f"{indent}  {{ {', '.join(parts)} }},")
    return "\n".join(rows)


def fallback_list(indent: str = "          ") -> str:
    """404.html's "where to go instead". Home is not offered — the reader is
    being sent somewhere useful, and the brand already goes home."""
    out = []
    for item in NAV:
        if item.href == "index.html":
            continue
        out.append(
            f'{indent}<li><a href="/{item.href}">{item.label}</a> '
            f"&mdash; {item.blurb}</li>"
        )
    return "\n".join(out)
