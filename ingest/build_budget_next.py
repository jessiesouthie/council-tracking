"""Build docs/data.budget-next.json — the FY2026–27 budget, from ClearGov line items.

The Budget page's main charts are built from the FY2025 *budget book* (see
docs/data.budget.json). No budget book exists for FY2026 or FY2027 — the city
stopped publishing them after FY2025 — but ClearGov still holds every line item
for every year behind its statistics API, and that is what this reads.

Method, and why it can be trusted: the same rollup run against FY2025 version
1917953 reproduces $172,508,305, the exact total the published FY2025 book
carries. That tie-out is asserted below and the script fails if it ever breaks.

Two version lineages are involved:

  1917953  the adopted-budget lineage. Holds FY2019–FY2026. FY2025 in this
           lineage is the published book; FY2026 is the budget adopted 17 Jun
           2025 (meeting 494).
  1937425  holds FY2027. This is where the FY2026–27 budget lives — set on
           5 May 2026 when the council accepted the tentative budget (meeting
           712), touched again 10 Jun and 16 Jul 2026 around the interim
           adoption (meeting 726).

FY2027 not sharing the adopted lineage is a real caveat, surfaced in the JSON
rather than smoothed over: the FY2026–27 budget is *interim*, adopted 16 Jun
2026 pending a Truth-in-Taxation hearing on 6 Aug and final adoption on 18 Aug
2026, so it has no adopted-lineage row to sit in yet.

Usage:  python -m ingest.build_budget_next
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

MUNICIPALITY_ID = 315552
API = "https://cleargov.com/api"

DOCS = Path(__file__).resolve().parent.parent / "docs"
OUT = DOCS / "data.budget-next.json"

# (year, native_version_id) for each budget this file compares.
FY2027 = (2027, 1937425)  # interim FY2026-27
FY2026 = (2026, 1917953)  # adopted FY2025-26
FY2025 = (2025, 1917953)  # adopted FY2024-25 — the published book

# The published FY2025 book's own bottom line. The rollup must reproduce it.
FY2025_BOOK_TOTAL = 172_508_305

# ClearGov's object taxonomy splits revenue from expense by client_id prefix.
REVENUE_PREFIX = "7.8.996.4"
EXPENSE_PREFIX = "7.8.996.5"

# ---------------------------------------------------------------------------
# The Sankey groupings, reverse-engineered from the shipped FY2025 file.
#
# The charts do not plot raw taxonomy names. Fund groups and functions are the
# depth-5 parents in ClearGov's own hierarchy, but a handful carry friendlier
# labels, and the General Fund's 20-odd departments collapse into nine service
# areas a resident would recognise. None of that is derivable from the API — it
# was authored by hand for the FY2025 page. Rather than invent a second, subtly
# different grouping for FY2027, the maps below were recovered by regenerating
# FY2025 from raw line items until it reproduced docs/data.budget.json, and
# verify_grouping() re-runs that check on every build.
# ---------------------------------------------------------------------------
FUND_GROUP_RENAME = {
    "Enterprise Fund": "Enterprise · Utilities",
    "Internal Service Fund": "Internal Service",
}
OBJECT_RENAME = {
    "Materials Supplies Services": "Materials, Supplies & Services",
    "Fund Balance Appropriation": "Use of Reserves",
}
REVENUE_RENAME = {
    "Other Sources of Funding": "Other funding & reserves",
    "Non-Business Licenses & Permits": "Licenses & Permits",
    "Interfund Transfers": "Transfers in",
    "State": "State & intergovernmental",
    "Fees": "Fees & charges",
}
# One account has to be corrected by hand. ClearGov maps revenue objects from
# the account's last segment, and account 31111 is "Sweetwater CRA Revenues" in
# the RDA fund — so the General Fund's own 10-00-31111-0000, which the city uses
# for the restricted property-tax increase, inherits the object "RDA". It carried
# $0 in FY2025 and FY2026 so it never mattered; at $6.96M it would render as the
# General Fund's second-largest revenue source, labelled "RDA". It is property
# tax, and it is not collected yet, so it gets a node that says both. Kept short
# because the Sankey's left label gutter clips anything much longer.
REVENUE_ACCOUNT_OVERRIDE = {"10-00-31111-0000": "Proposed property tax"}
SERVICE_AREA = {
    "Public Safety": "Public Safety",
    "Streets & Roads": "Streets & Roads",
    "Economic Development": "Economic Development",
    "Planning & Zoning": "Planning & Zoning",
    "Engineering": "Engineering",
    "Library": "Library",
    "Parks": "Parks, Recreation & Events",
    "Recreation": "Parks, Recreation & Events",
    "Special Events": "Parks, Recreation & Events",
    "Open Space": "Parks, Recreation & Events",
    "Building Inspections": "Building & Neighborhood",
    "Neighborhood Improvement": "Building & Neighborhood",
    "Executive": "General government",
    "Finance": "General government",
    "Information Technology": "General government",
    "Operations & Facilities": "General government",
    "City Recorder's Office": "General government",
    "Human Resources": "General government",
    "Legislative": "General government",
    "Attorney": "General government",
    "Communications": "General government",
    # New departments in the FY2027 books, all on fresh account numbers under the
    # same 10-11 block as the rest of city hall. Bucketed with it so the chart
    # doesn't read a bookkeeping change as new spending.
    "Accounting": "General government",
    "Risk Management": "General government",
    "Legislative & Strategic Svcs": "General government",
    "Customer Services": "General government",
    # Not a department: money leaving the fund. ClearGov files these under a
    # category that happens to share a department's name — see DISAMBIGUATE.
    "Transfers out": "Transfers out",
}

# ClearGov reuses department names across functions: there is a "Planning &
# Zoning" under Community Development (the actual department) and another under
# Interfund Transfers (money the General Fund sends elsewhere). Fourteen names
# collide this way. Keying on the name alone silently adds them together — and
# in FY2027 it is not a rounding issue: the General Fund's entire "Planning &
# Zoning" figure is transfers, because the real department moved to the new
# Community Development fund. So categories are keyed on (function, name) and
# the transfer ones are relabelled for what they are.
DISAMBIGUATE = {"Interfund Transfers": "Transfers out"}

# The General Fund revenue accounts that move to the Community Development fund
# in FY2027 — building permits, plan check and development fees. Listed by
# account because the move is what makes the department comparison misleading,
# and the page states the amount.
PERMIT_ACCOUNTS = {
    "10-00-32311-0000",  # Bldg Permits - Building
    "10-00-34512-0000",  # Dev Fee - Plan Check
    "10-00-34513-0000",  # Plan Check Solar/Commercial
    "10-00-34515-0000",  # Dev Fee - Plat Fees
    "10-00-32330-0000",  # Bldg Permit - Temp Power Inspection
    "10-00-32220-0000",  # Permits - Conditional Use
    "10-00-34516-0000",  # Dev Fee - Zoning and Subdivision
    "10-00-32214-0000",  # Permits - Sign
    "10-00-32320-0000",  # Bldg Permits - Surcharge
}

# The FY2025 file was built from an earlier ClearGov snapshot. Three line-item
# labels have since been corrected upstream, so an exact string comparison would
# fail on text the city fixed. Verification normalises these and nothing else.
UPSTREAM_TEXT_FIXES = {"treatement": "treatment"}


def fetch(path: str):
    req = urllib.request.Request(f"{API}/{path}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)


def load():
    cats = {c["id"]: c for c in fetch(f"categories/listByMunicipalityId?municipalityId={MUNICIPALITY_ID}")}
    items = fetch(f"statistics/municipalities/{MUNICIPALITY_ID}/lineItems")
    return cats, items


class Book:
    """One (year, version) budget, indexed the way the page needs to read it."""

    def __init__(self, items, cats, year, version):
        self.cats = cats
        self.rows = [r for r in items if r["year"] == year and r["native_version_id"] == version]
        self.year = year
        self.version = version

    def name(self, cat_id):
        c = self.cats.get(cat_id)
        return c["name"] if c else None

    def parent(self, cat_id):
        """The depth-5 group a fund or department sits under — the chart's columns."""
        c = self.cats.get(cat_id)
        if not c or c.get("parent_id") not in self.cats:
            return None
        return self.cats[c["parent_id"]]["name"]

    def department(self, cat_id, disambiguate=True):
        """A department name that means one thing.

        ClearGov reuses names across functions, so the raw name is ambiguous;
        this resolves the ones that would otherwise be summed together. Passing
        disambiguate=False reproduces the older, conflated behaviour, which is
        how verify_grouping proves the fix changes nothing else.
        """
        if disambiguate:
            fn = self.parent(cat_id)
            if fn in DISAMBIGUATE:
                return DISAMBIGUATE[fn]
        return self.name(cat_id)

    def side(self, row):
        c = self.cats.get(row.get("native_object_id"))
        if not c:
            return None
        if c["client_id"].startswith(REVENUE_PREFIX):
            return "rev"
        if c["client_id"].startswith(EXPENSE_PREFIX):
            return "exp"
        return None

    def lines(self, side, fund=None):
        for r in self.rows:
            if self.side(r) != side:
                continue
            if fund is not None and self.name(r.get("native_fund_id")) != fund:
                continue
            yield r

    def total(self, side="exp", fund=None):
        return sum(r["total"] or 0 for r in self.lines(side, fund))

    def by(self, dim, side="exp", fund=None):
        """Sum a side by fund / category (department) / object (type of spending).

        Departments go through department() rather than name() so the
        year-over-year table can't sum a department together with the transfers
        category that shares its name.
        """
        key = {"fund": "native_fund_id", "category": "native_category_id", "object": "native_object_id"}[dim]
        out: dict[str, float] = {}
        for r in self.lines(side, fund):
            cid = r.get(key)
            k = (self.department(cid) if dim == "category" else self.name(cid)) or "Unassigned"
            out[k] = out.get(k, 0) + (r["total"] or 0)
        return out


def dept_line(book: Book, department: str):
    """One department's General Fund expense, transfers category excluded."""
    return sum(
        r["total"] or 0 for r in book.lines("exp", fund="General Fund")
        if book.department(r.get("native_category_id")) == department
    )


def _sorted_totals(counts: dict):
    return [{"name": k, "total": round(v)} for k, v in sorted(counts.items(), key=lambda kv: -kv[1]) if v]


def _drill(rows, top=40):
    """Line items for one focused node: top N by value, then an "Other" rollup.

    Grouped by (line name, specific fund) — the same salary line in two
    departments is one row, as the FY2025 page shows it. `c` is the co-ordinate
    on the other axis; where a group spans several of them the largest
    contributor names it. The Other row keeps the list summing to the node.
    """
    agg, tag = {}, {}
    for nm, c, f, v in rows:
        if not v:
            continue
        agg[(nm, f)] = agg.get((nm, f), 0) + v
        # Compared on magnitude and seeded on first sight, so a group made only
        # of credit lines still gets tagged instead of raising.
        if (nm, f) not in tag or abs(v) > abs(tag[(nm, f)][0]):
            tag[(nm, f)] = (v, c)
    # Ranked by size, not by signed value: some General Fund lines are credits
    # (administrative cost recharged to the utility funds, booked as negative
    # expense). Ranking on the signed value would push a -$1.4M line to the
    # bottom of the list and bury it inside "Other", which then reads negative.
    ordered = sorted(agg.items(), key=lambda kv: -abs(kv[1]))
    out = [{"name": k[0], "value": round(v), "c": tag[k][1], "f": k[1]} for k, v in ordered[:top]]
    rest = ordered[top:]
    if rest:
        out.append({
            "name": f"Other ({len(rest)} smaller lines)",
            "value": round(sum(v for _, v in rest)),
            "c": None,
            "f": None,
        })
    return out


def chart_shape(book: Book, disambiguate=True):
    """The all_funds + general_fund blocks the Sankeys read, for one budget."""
    exp = [r for r in book.lines("exp")]

    def fund_group(r):
        g = book.parent(r.get("native_fund_id"))
        return FUND_GROUP_RENAME.get(g, g)

    def function(r):
        return book.parent(r.get("native_category_id"))

    def obj(r):
        o = book.name(r.get("native_object_id"))
        return OBJECT_RENAME.get(o, o)

    funds, functions, objects = {}, {}, {}
    lff, lfo = {}, {}
    for r in exp:
        v = r["total"] or 0
        g, fn, ob = fund_group(r), function(r), obj(r)
        funds[g] = funds.get(g, 0) + v
        functions[fn] = functions.get(fn, 0) + v
        objects[ob] = objects.get(ob, 0) + v
        lff[(g, fn)] = lff.get((g, fn), 0) + v
        lfo[(g, ob)] = lfo.get((g, ob), 0) + v

    all_funds = {
        "funds": _sorted_totals(funds),
        "functions": _sorted_totals(functions),
        "objects": _sorted_totals(objects),
        "links_fund_function": [{"source": a, "target": b, "value": round(v)}
                                for (a, b), v in sorted(lff.items(), key=lambda kv: -kv[1]) if v],
        "links_fund_object": [{"source": a, "target": b, "value": round(v)}
                              for (a, b), v in sorted(lfo.items(), key=lambda kv: -kv[1]) if v],
        "drill": {
            "byFund": {g: _drill([(r["name"], function(r), book.name(r.get("native_fund_id")), r["total"] or 0)
                                  for r in exp if fund_group(r) == g])
                       for g, v in funds.items() if v},
            "byFunction": {g: _drill([(r["name"], fund_group(r), book.name(r.get("native_fund_id")), r["total"] or 0)
                                      for r in exp if function(r) == g])
                           for g, v in functions.items() if v},
            "byObject": {g: _drill([(r["name"], fund_group(r), book.name(r.get("native_fund_id")), r["total"] or 0)
                                    for r in exp if obj(r) == g])
                         for g, v in objects.items() if v},
        },
    }

    rev = {}
    for r in book.lines("rev", fund="General Fund"):
        n = REVENUE_ACCOUNT_OVERRIDE.get(r["sub_account_number"])
        if n is None:
            n = book.name(r.get("native_object_id"))
            n = REVENUE_RENAME.get(n, n)
        rev[n] = rev.get(n, 0) + (r["total"] or 0)
    ranked = [(k, v) for k, v in sorted(rev.items(), key=lambda kv: -kv[1]) if v]
    # The chart shows the eight largest sources plus one "Other revenue" node;
    # the table under it expands that node into its parts.
    keep, tail = ranked[:8], ranked[8:]
    revenue = [{"name": k, "total": round(v)} for k, v in keep]
    if tail:
        revenue.append({"name": "Other revenue", "total": round(sum(v for _, v in tail))})

    dept = {}
    for r in book.lines("exp", fund="General Fund"):
        d = book.department(r.get("native_category_id"), disambiguate)
        dept[d] = dept.get(d, 0) + (r["total"] or 0)
    unmapped = sorted(d for d, v in dept.items() if v and d not in SERVICE_AREA)
    if unmapped:
        raise SystemExit(
            "General Fund departments with no service area: " + ", ".join(unmapped)
            + f"\nAdd them to SERVICE_AREA in {__file__} — leaving them unmapped would "
              "silently add a bucket to the General Fund chart."
        )
    area = {}
    for d, v in dept.items():
        a = SERVICE_AREA.get(d, d)
        area[a] = area.get(a, 0) + v

    general_fund = {
        "total": round(sum(dept.values())),
        "revenue": revenue,
        "revenue_detail": [{"name": k, "total": round(v)} for k, v in keep + tail],
        "departments": _sorted_totals(area),
        "departments_detail": _sorted_totals(dept),
    }
    return all_funds, general_fund


def verify_grouping(fy25: Book, published: Path):
    """Regenerate FY2025's chart blocks and check them against the shipped file.

    The groupings are hand-authored, so this is the only thing standing between
    a quiet mapping drift and two charts on one page that count differently.
    """
    if not published.exists():
        print("! data.budget.json absent — grouping not verified")
        return
    want = json.loads(published.read_text(encoding="utf-8"))
    af, gf = chart_shape(fy25)

    for block in ("funds", "functions", "objects", "links_fund_function", "links_fund_object"):
        if af[block] != want["all_funds"][block]:
            raise SystemExit(f"grouping drift: all_funds.{block} no longer reproduces the FY2025 file")
    # The department fix deliberately splits transfers out of the department they
    # were filed under, so `gf` cannot match the published file. Rebuilding with
    # the fix off must match it exactly — that is what proves the fix is the only
    # difference, rather than a mapping that has quietly drifted.
    _, gf_conflated = chart_shape(fy25, disambiguate=False)
    if gf_conflated != want["general_fund"]:
        raise SystemExit("grouping drift: general_fund no longer reproduces the FY2025 file")
    if round(sum(d["total"] for d in gf["departments"])) != want["general_fund"]["total"]:
        raise SystemExit("the department fix changed the General Fund total; it must only re-label")

    def norm(s):
        s = " ".join(s.lower().split())
        for a, b in UPSTREAM_TEXT_FIXES.items():
            s = s.replace(a, b)
        return s

    for dim, groups in want["all_funds"]["drill"].items():
        if set(af["drill"][dim]) != set(groups):
            raise SystemExit(f"grouping drift: drill.{dim} has different groups than the FY2025 file")
        for g, items in groups.items():
            got = [(norm(e["name"]), e["f"], e["value"]) for e in af["drill"][dim][g]]
            exp = [(norm(e["name"]), e["f"], e["value"]) for e in items]
            if got != exp:
                raise SystemExit(f"grouping drift: drill.{dim}[{g}] no longer reproduces the FY2025 file")
    print("grouping verified against the published FY2025 charts")


def pair(now: dict, prior: dict, *, drop_zero=True):
    """Merge two {name: total} maps into a sorted comparison list."""
    rows = []
    for k in set(now) | set(prior):
        a, b = prior.get(k, 0), now.get(k, 0)
        if drop_zero and not a and not b:
            continue
        rows.append({"name": k, "prior": round(a), "total": round(b), "change": round(b - a)})
    rows.sort(key=lambda r: -r["total"])
    return rows


def build():
    cats, items = load()
    fy27 = Book(items, cats, *FY2027)
    fy26 = Book(items, cats, *FY2026)
    fy25 = Book(items, cats, *FY2025)

    got = round(fy25.total("exp"))
    if got != FY2025_BOOK_TOTAL:
        raise SystemExit(
            f"FY2025 rollup is {got:,}, not the published {FY2025_BOOK_TOTAL:,}. "
            "ClearGov's taxonomy or version lineage moved — re-verify before publishing."
        )
    verify_grouping(fy25, DOCS / "data.budget.json")
    af27, gf27 = chart_shape(fy27)

    fba27 = fy27.by("object")["Fund Balance Appropriation"]
    fba26 = fy26.by("object").get("Fund Balance Appropriation", 0)
    fba25 = fy25.by("object").get("Fund Balance Appropriation", 0)

    gross27, gross26, gross25 = fy27.total(), fy26.total(), fy25.total()

    borrowing = sorted(
        (
            {
                "name": fy27.name(r.get("native_fund_id")),
                "total": round(r["total"]),
            }
            for r in fy27.lines("rev")
            if fy27.name(r.get("native_object_id")) == "Proceeds from Borrowing" and (r["total"] or 0) > 0
        ),
        key=lambda r: -r["total"],
    )

    capital = sorted(
        (
            {
                "name": r["name"],
                "fund": fy27.name(r.get("native_fund_id")),
                "account": r["sub_account_number"],
                "total": round(r["total"]),
            }
            for r in fy27.lines("exp")
            if fy27.name(r.get("native_object_id")) == "Capital Outlay" and (r["total"] or 0) > 0
        ),
        key=lambda r: -r["total"],
    )

    def line(book, account):
        """One line item by its account number — for claims that name a specific line."""
        hits = [r["total"] or 0 for r in book.rows if r["sub_account_number"] == account]
        if not hits:
            raise SystemExit(f"account {account} is missing from FY{book.year} v{book.version}")
        return round(sum(hits))

    def gf_revenue(book):
        return {
            r["name"]: r["total"] or 0
            for r in book.lines("rev", fund="General Fund")
            if (r["total"] or 0) > 0
        }

    data = {
        "_comment": (
            "Eagle Mountain's FY2026-27 budget as adopted on an interim basis 16 June 2026, "
            "compared against the FY2025-26 and FY2024-25 adopted budgets. Rolled up from the "
            "city's own ClearGov line items via ingest/build_budget_next.py. Budgeted, not "
            "actual. This budget is not final: a Truth-in-Taxation hearing is set for 6 August "
            "2026 and final adoption for 18 August 2026, and figures can move at either. The "
            "same rollup reproduces the published FY2025 book total to the dollar, which is how "
            "the method is verified."
        ),
        "fiscal_year": "2026-27",
        "fiscal_year_label": "FY2026–27",
        "status": "interim",
        "status_note": (
            "Passed 16 June 2026 by a 4–1 vote as an interim budget — meaning it is in force but "
            "not final. Utah law requires that step when a city proposes a property-tax increase, "
            "because the increase has to go to a public hearing first. The final vote on this "
            "budget is scheduled for 18 August 2026, and the figures can change before then."
        ),
        "as_of": "2026-07-16",
        "source": {
            "title": "Eagle Mountain City line items",
            "publisher": "ClearGov",
            "url": "https://cleargov.com/api/statistics/municipalities/315552/lineItems",
            "note": (
                "The city has not published a budget book since FY2025. These figures come from "
                "the raw line-item feed that sits behind that book — the same data, one step "
                "earlier: version 1937425 for FY2026-27, and 1917953 for the FY2025 and FY2026 "
                "adopted budgets."
            ),
            "caveat": (
                "The FY2026-27 figures come from a different version of that feed than the "
                "finished budgets they are compared against, because an interim budget does not "
                "get a finished-budget entry until it is adopted. Everything is added up exactly "
                "the same way, and the method reproduces the city's published FY2025 total to the "
                "dollar."
            ),
        },
        "calendar": [
            {
                "date": "2026-05-05",
                "meeting_id": 712,
                "label": "Tentative budget accepted",
                "note": "Council also acknowledged the budget officer's proposed property-tax increase.",
                "done": True,
            },
            {
                "date": "2026-05-26",
                "meeting_id": 748,
                "label": "Budget work session",
                "note": "Line-by-line review of new positions; no votes taken.",
                "done": True,
            },
            {
                "date": "2026-06-16",
                "meeting_id": 726,
                "label": "Interim budget adopted, 4–1",
                "note": "Councilmember Huish voted no.",
                "done": True,
            },
            {
                "date": "2026-08-06",
                "meeting_id": 747,
                "label": "Tax increase adopted, 5–0",
                "note": "Roughly sixty people spoke at the Truth-in-Taxation hearing; the Council then set the rate at 0.001500, not the 0.001700 it noticed.",
                "done": True,
            },
            {
                "date": "2026-08-18",
                "meeting_id": None,
                "label": "Final budget adoption",
                "note": "Set by the 5 May resolution accepting the tentative budget.",
                "done": False,
            },
        ],
        "totals": {
            "fy2027": {"gross": round(gross27), "reserves": round(fba27), "net": round(gross27 - fba27)},
            "fy2026": {"gross": round(gross26), "reserves": round(fba26), "net": round(gross26 - fba26)},
            "fy2025": {"gross": round(gross25), "reserves": round(fba25), "net": round(gross25 - fba25)},
        },
        "borrowing": {
            "total": round(sum(b["total"] for b in borrowing)),
            "by_fund": borrowing,
            "note": (
                "Budgeted bond proceeds. Nearly all of it is the sewer bond that pays for the new "
                "wastewater treatment plant; the budget books the borrowing in FY2027 even though "
                "most of the construction happens in later years."
            ),
        },
        # The Sankeys read these two, in exactly the shape data.budget.json uses,
        # so the FY2025 charts and these are one implementation with two inputs.
        "all_funds": af27,
        "general_fund_chart": gf27,
        "funds": pair(fy27.by("fund"), fy26.by("fund")),
        "objects": pair(fy27.by("object"), fy26.by("object")),
        "capital": capital,
        "capital_total": round(sum(c["total"] for c in capital)),
        "general_fund": {
            "total": round(fy27.total(fund="General Fund")),
            "prior": round(fy26.total(fund="General Fund")),
            "revenue": pair(gf_revenue(fy27), gf_revenue(fy26)),
            "departments": pair(
                fy27.by("category", fund="General Fund"), fy26.by("category", fund="General Fund")
            ),
            # The biggest structural change in the General Fund, and the reason a
            # naive department comparison misleads: planning and building left it.
            "moved_out": {
                "fund": "Community Development",
                "fund_total": round(fy27.total("exp", fund="Community Development")),
                "departments": [
                    {"name": "Building Inspections", "prior": round(dept_line(fy26, "Building Inspections"))},
                    {"name": "Planning & Zoning", "prior": round(dept_line(fy26, "Planning & Zoning"))},
                ],
                "revenue_moved": round(sum(
                    r["total"] or 0 for r in fy26.lines("rev", fund="General Fund")
                    if r["sub_account_number"] in PERMIT_ACCOUNTS
                )),
                "note": (
                    "FY2027 moves planning, building inspection and permitting out of the General "
                    "Fund into a new Community Development fund that pays for itself out of permit "
                    "and plan-check fees. Both the cost and the fee revenue leave the General Fund "
                    "together, so a department-by-department comparison against FY2026 is not "
                    "like for like."
                ),
            },
            "reorg_note": (
                "Four departments show up for the first time this year — Accounting, Risk "
                "Management, Legislative & Strategic Services and Customer Services. They are new "
                "lines in the same city-hall part of the budget; the budget does not say whether "
                "that is new work or existing work being split out and given its own name. And "
                "\u201cTransfers out\u201d is not a department at all: it is money the General "
                "Fund sends to the city's other funds, which the source data happens to file "
                "under the Planning & Zoning name."
            ),
        },
        "tax_line": {
            "interim_revenue": line(fy27, "10-21-42100-9498"),
            "base_revenue": gf_revenue(fy26)["Property Taxes"],
            "revenue_account": "Property Taxes Interim Budget",
            "expense_account": "Restricted Interim Budget",
            "department": "Public Safety",
            "note": (
                "The proposed property-tax increase appears in the budget as its own revenue line "
                "and, for the identical amount, as a restricted expense line inside Public Safety. "
                "The Council approved an increase on 6 August, but at 0.001500 rather than the "
                "0.001700 this budget was built on, so the figure is trued up at the 18 August "
                "adoption."
            ),
        },
        # The single clearest thing the line items show: the Sheriff contract is
        # being moved off sales tax and onto property tax. Contract Services falls
        # to exactly today's property-tax collections, and the rest of the contract
        # is held in the restricted line until the hearing. The two together are the
        # proposed property-tax revenue, to the dollar.
        "public_safety": {
            "contract_prior": line(fy26, "10-21-42100-4520"),
            "contract": line(fy27, "10-21-42100-4520"),
            "restricted": line(fy27, "10-21-42100-9498"),
            "dispatch_prior": line(fy26, "10-21-42100-4525"),
            "dispatch": line(fy27, "10-21-42100-4525"),
            "note": (
                "In FY2025-26 the Sheriff contract line carries the whole cost and is met largely "
                "out of sales tax. In FY2026-27 it is cut to the exact amount of the city's current "
                "property-tax collections, with the balance moved to the restricted line. Added "
                "together they are the proposed property-tax revenue exactly — the budget's own "
                "statement that public safety is being re-based onto property tax."
            ),
        },
    }
    ps = data["public_safety"]
    ps["combined"] = ps["contract"] + ps["restricted"]

    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} — FY2027 gross {gross27:,.0f}, net of reserves {gross27 - fba27:,.0f}")


if __name__ == "__main__":
    build()
