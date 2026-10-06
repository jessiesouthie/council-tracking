"""
The site's destination list, written down once.

Before this file the same list existed in four hand-maintained copies, and no
two of them agreed. Now it lives here. ingest/chrome_v2.py draws the header,
the phone tab bar and the footer from it, ingest/build_nav.py splices that
chrome into every page plus 404.html's "where to go instead", and
build_meeting_pages.py imports chrome_v2 directly. Nothing is rendered at
runtime, because the nav has to survive a load where no JavaScript runs.

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
class Child:
    """A page inside a section, as it is named in that section's menu."""

    label: str
    href: str


@dataclass(frozen=True)
class Item:
    """One top-level destination.

    `children` are the pages inside the section. The section's own sub-nav
    strip lists them in this order, and docs/nav.test.mjs holds each page's
    strip to it.

    `alias` names other pages that belong to this section but are not menu
    entries — member.html is a per-member detail page, not somewhere to send
    someone from the bar. Menu children are aliases too; `aliases` merges both.
    Aliases light this item up without claiming aria-current for themselves;
    see chrome_v2._attrs().

    `body_scoped` marks a section that only the City Council has. The Planning
    Commission levies no tax and adopts no budget, so carrying Finances into
    that view would be six links to figures that aren't theirs.
    """

    label: str
    href: str
    children: tuple[Child, ...] = ()
    alias: tuple[str, ...] = ()
    mobile: bool = True
    # False keeps it out of the header bar; the footer still lists it.
    header: bool = True
    body_scoped: bool = False
    blurb: str = ""
    # The tab bar's name for it. Five tabs share a 390px phone, about 78px
    # each, so "Meetings & Votes" goes on the bar as "Meetings". Empty means
    # the label already fits.
    short: str = ""

    @property
    def tab_label(self) -> str:
        return self.short or self.label

    @property
    def aliases(self) -> tuple[str, ...]:
        """Every page that should light this item up, menu children included.

        The section's own href is excluded — it is the item, not an alias of
        itself, and listing it there would hand aria-current to two elements.
        """
        seen, out = {self.href}, []
        for href in [c.href for c in self.children] + list(self.alias):
            if href not in seen:
                seen.add(href)
                out.append(href)
        return tuple(out)


# Order is the reading order: home, then the record, then what was built on it,
# then how it was built.
NAV: tuple[Item, ...] = (
    Item(
        label="Home",
        href="index.html",
        blurb="the front page: what changed most recently, and what is coming up.",
    ),
    # Votes is a tab of this section rather than a section of its own. Both
    # pages read the same record from the other end: meetings.html is the
    # archive by night, motions.html is the same motions cut loose from their
    # night and searchable by topic, member, outcome or year. A reader who has
    # found the meeting and a reader who has found the subject are after the
    # same thing, so the two sit one tab apart instead of one bar apart.
    #
    # motions.html keeps its filename. It is the target of the homepage search
    # form and of every tag chip on the site, and it has been indexed under that
    # name for months — renaming the file to match the label would break all of
    # it to no reader's benefit. "Votes" is what the page is; "motions" is what
    # the minutes call it, which is why definitions.html has an entry for it.
    Item(
        label="Meetings & Votes",
        short="Meetings",
        href="meetings.html",
        children=(
            Child("Meetings", "meetings.html"),
            Child("Votes", "motions.html"),
        ),
        blurb="every council meeting on file, the motions decided each night, "
              "the transcripts, and the whole roll-call record searchable by topic.",
    ),
    Item(
        label="Members",
        href="members.html",
        alias=("member.html",),
        blurb="roll-call totals for each member, and how often each pair voted together.",
    ),
    # Claims cut across every section — tax, staffing, the petition — so it is
    # not a child of any of them.
    #
    # It was off the mobile bar while Votes held a slot, which meant the page
    # was reachable on a phone only from its card on the front page: a reader
    # who arrived on a claim from a link someone sent had no way back to the
    # rest of them. Folding Votes into Meetings freed the slot, and this is what
    # it is for. Most people meet this site on a phone, and a claim is the thing
    # they are most often sent.
    # Body-scoped for the same reason the front page only shows its Claims card
    # to the council, and for the reason claims.html itself gives when it is
    # opened under another body: every claim on file is about something the City
    # Council did. Offering it from the Planning Commission's bar would spend a
    # slot on a page that opens saying "not this body".
    # Named for what a reader is checking, not for what the page holds: "Claims"
    # read as the site making them.
    Item(
        label="Fact Checks",
        short="Fact checks",
        href="claims.html",
        body_scoped=True,
        blurb="what is going around about the city, checked against the recordings and the notices.",
    ),
    Item(
        label="Taxes & Budget",
        short="Taxes",
        href="finances.html",
        # In the order the money moves, which is the order the section page
        # walks them through: what is charged, what it is spent on, what a
        # different rate would bring in, and the biggest thing it buys.
        children=(
            Child("Overview", "finances.html"),
            Child("Tax", "tax.html"),
            Child("Projections", "projections.html"),
            Child("Budget", "budget.html"),
            Child("Staffing", "staffing.html"),
        ),
        body_scoped=True,
        blurb="the tax rate, the budget, the projections and the payroll behind them.",
    ),
    # Subjects that run across years of meetings and every body on the site:
    # one place for what a reader would otherwise piece together from forty
    # motions. Not body-scoped, because the Planning Commission and the RDA
    # board acted on these too. Off the mobile bar, which is full; the front
    # page carries a card for it instead.
    Item(
        label="Topics",
        href="data-centers.html",
        children=(
            Child("Data centers", "data-centers.html"),
            Child("Tax referendum", "referendum.html"),
        ),
        mobile=False,
        blurb="subjects that run across years of meetings: the data centers and the tax referendum.",
    ),
    # Reference, not a destination anyone arrives looking for. It stays off the
    # mobile bar — five is the most a bottom bar can hold before the labels stop
    # being readable — and off the header bar too: every page carries it in the
    # footer, and the strip's "What is this?" link goes to it.
    Item(
        label="About",
        href="about.html",
        children=(
            Child("How this site is built", "about.html"),
            Child("Definitions", "definitions.html"),
        ),
        mobile=False,
        header=False,
        blurb="where the record comes from, and the words the city uses in plain English.",
    ),
)

# The mobile bottom bar. No overflow sheet: everything on the bar is a top-level
# section, and the sections that aren't on it live in the footer or on the front
# page. Five is the ceiling, not the target — see MOBILE_MAX.
TABBAR: tuple[Item, ...] = tuple(i for i in NAV if i.mobile)

# The desktop and tablet header bar.
HEADER: tuple[Item, ...] = tuple(i for i in NAV if i.header)

MOBILE_MAX = 5

BEGIN = "BEGIN generated:nav (ingest/build_nav.py)"
END = "END generated:nav"


def fallback_list(indent: str = "          ") -> str:
    """404.html's "where to go instead". Home is not offered — the reader is
    being sent somewhere useful, and the brand already goes home."""
    out = []
    for item in NAV:
        if item.href == "index.html":
            continue
        out.append(
            f'{indent}<li><a href="/{item.href}">{item.label}</a>: '
            f"{item.blurb}</li>"
        )
    return "\n".join(out)
