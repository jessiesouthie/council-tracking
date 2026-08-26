"""Build docs/data.budget-book.json — the FY2025–26 adopted budget, line by line.

The city published a full budget book for FY2025–26 after all. It had not
appeared when the Budget page was built, which is why that page carries the
FY2025 book as "the last budget published in full" and says the year in between
has none. It does: *Eagle Mountain City Adopted Budget FY 2025-2026*, 646 pages,
produced through ClearGov like the FY2025 one. This script rebuilds it.

It rebuilds rather than parses. The book's tables are ClearGov renderings of the
same line items the statistics API serves, so the API gives every figure the
book prints, already structured — and, unlike the PDF, gives them at full
precision with the account number attached. Half the book is images (the whole
5-year capital plan is a picture of a table), so parsing would lose more than it
gained. What the PDF is used for is proof: BOOK_CHECKS below holds figures read
off its pages, and the build fails if the rebuild misses any of them.

Which version is which. This matters more than it sounds, because ClearGov holds
FY2026 twice and the two differ by $44M:

  1937425 / FY2026  the adopted book — $119,105,860. Ties to the PDF exactly, at
                    citywide, fund, department and account level. Same lineage
                    the FY2026–27 interim budget sits in.
  1917953 / FY2026  the same year as it stands now — $163,179,862. Capital that
                    was not finished by 30 June 2025 rolled forward into it,
                    which the book's own opening page says to expect.

data.budget-next.json compares FY2026–27 against the second one, so what it
calls last year's budget is the amended figure, not the adopted one. Both are
carried here, per line, and the page labels them.

The other columns are the book's own comparison columns, each a different
lineage, all confirmed against printed pages:

  1942690 / FY2024  FY2024 actual as the *department* pages print it — a
                    year-to-date snapshot taken while the book was being made.
  1917954 / FY2024  FY2024 actual as the *fund summary* pages print it — the
                    finished year. Higher: $102.3M against $95.2M citywide.
                    The book uses both, so both are here, each against the
                    table it belongs to.
  1942690 / FY2025  FY2025 estimated actual — $166,266,714.
  1917953 / FY2025  FY2025 adopted budget — $172,508,305, the FY2025 book's own
                    bottom line, which is the tie-out data.budget.json rests on.

Revenue grouping. The book's nine revenue categories (Taxes, Licenses & Permits,
Utility Revenue …) are not ClearGov's object taxonomy — they are the city's
account ranges, 31xxx through 39xxx, exactly as Utah's uniform chart of accounts
lays them out. Grouping on the account number reproduces all nine to the dollar,
citywide and per fund; grouping on the object taxonomy does not come close.

Usage:  python -m ingest.build_budget_book [--cache DIR]

--cache reuses (and fills) a local copy of the two API responses; the line-item
feed is ~29MB, and nothing in it changes between runs of this script.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from .build_budget_next import (
    API,
    MUNICIPALITY_ID,
    Book,
    chart_shape,
)

DOCS = Path(__file__).resolve().parent.parent / "docs"
OUT = DOCS / "data.budget-book.json"

# (year, native_version_id) for every column the book prints.
ADOPTED = (2026, 1937425)   # FY2025-26 as adopted — the book
AMENDED = (2026, 1917953)   # FY2025-26 as it stands, carry-forwards included
FY25_BUDGET = (2025, 1917953)
FY25_ESTIMATE = (2025, 1942690)
FY24_YTD = (2024, 1942690)  # what the department line tables print
FY24_ACTUAL = (2024, 1917954)  # what the fund summary tables print

# The city's revenue account ranges, which are what the book's revenue tables
# group on. Keyed by the first two digits of an account's middle segment:
# 10-00-31116-0000 → "31" → Taxes.
REVENUE_RANGES = [
    ("31", "Taxes"),
    ("32", "Licenses & Permits"),
    ("33", "Intergovernmental Revenue"),
    ("34", "Charges for Services"),
    ("35", "Utility Revenue"),
    ("36", "Fines & Forfeitures"),
    ("37", "Other Revenue"),
    ("38", "Interfund Transfers"),
    ("39", "Special Revenues & Resources"),
]
REVENUE_ORDER = [name for _, name in REVENUE_RANGES]

# The order the book lists expense objects in, which is not size order.
OBJECT_ORDER = [
    "Personnel Services",
    "Materials Supplies Services",
    "Depreciation",
    "Capital Outlay",
    "Grants",
    "Debt Service",
    "Interfund Transfers",
    "Fund Balance Appropriation",
]

# ---------------------------------------------------------------------------
# Read off the PDF. Every number below appears on a printed page of the FY
# 2025-2026 book; the page it appears on is named so the next person can check
# it without re-deriving anything. Nothing here is computed — that is the point.
# ---------------------------------------------------------------------------
BOOK_CHECKS = {
    # Comprehensive Fund Summary, all funds (p. 80)
    "citywide": {
        "revenue_total": 119_105_860,
        "expense_total": 119_105_860,
        "revenue": {
            "Taxes": 34_463_854,
            "Licenses & Permits": 1_725_164,
            "Intergovernmental Revenue": 13_536_627,
            "Charges for Services": 15_411_375,
            "Utility Revenue": 29_909_828,
            "Fines & Forfeitures": 157_700,
            "Other Revenue": 1_579_404,
            "Interfund Transfers": 7_377_723,
            "Special Revenues & Resources": 14_944_185,
        },
        "expense": {
            "Personnel Services": 16_992_670,
            "Materials Supplies Services": 59_678_777,
            "Depreciation": 5_114_455,
            "Capital Outlay": 23_010_006,
            "Grants": 1_226_221,
            "Debt Service": 3_506_892,
            "Interfund Transfers": 6_879_189,
            "Fund Balance Appropriation": 2_697_650,
        },
        # Prior-year columns of the same table, which pin the other lineages.
        "fy25_budget_expense": 172_508_305,
        "fy25_estimate_expense": 166_266_714,
        "fy25_estimate_revenue": 182_198_507,
        "fy24_actual_expense": 102_272_587,
        "fy24_actual_revenue": 117_780_430,
        # The FY2024 column of the same two tables, category by category. This
        # is what proves the second FY2024 lineage is the one the fund summaries
        # use, and re-proves the revenue account ranges in a second year.
        "fy24_revenue": {
            "Taxes": 30_207_460,
            "Licenses & Permits": 3_369_266,
            "Intergovernmental Revenue": 9_511_950,
            "Charges for Services": 20_477_632,
            "Utility Revenue": 21_738_887,
            "Fines & Forfeitures": 215_547,
            "Other Revenue": 11_376_399,
            "Interfund Transfers": 19_403_564,
            "Special Revenues & Resources": 1_479_724,
        },
        "fy24_expense": {
            "Personnel Services": 11_728_965,
            "Materials Supplies Services": 47_465_994,
            "Capital Outlay": 16_446_217,
            "Debt Service": 5_155_204,
            "Interfund Transfers": 21_476_207,
        },
    },
    # General Fund, Comprehensive Fund Summary (pp. 95–96)
    "general_fund": {
        "revenue_total": 34_634_720,
        "expense_total": 34_634_720,
        "revenue": {
            "Taxes": 26_009_902,
            "Licenses & Permits": 1_725_164,
            "Intergovernmental Revenue": 3_339_287,
            "Charges for Services": 1_983_250,
            "Utility Revenue": 12_000,
            "Fines & Forfeitures": 19_700,
            "Other Revenue": 520_000,
            "Interfund Transfers": 22_917,
            "Special Revenues & Resources": 1_002_500,
        },
        "fy25_budget_expense": 36_819_867,
        "fy25_estimate_expense": 36_136_547,
        "fy24_actual_expense": 35_698_630.02,
    },
    # Public Safety department page (pp. 490–493) — the deepest check, because
    # it pins a single account in a single fund against a printed line.
    "public_safety": {
        "total": 8_789_107,
        "groups": {
            "Personnel Services": 294_046,
            "Materials Supplies Services": 8_488_561,
            "Capital Outlay": 6_500,
        },
        "fy24_ytd": 6_405_185.85,
        "fy25_budget": 7_734_349,
        "accounts": {
            "10-21-42100-4520": 7_846_278,   # Contract Services — the sheriff
            "10-21-42100-1111": 79_691,      # Salaries - Full-Time Permanent
            "10-21-42100-7415": 6_500,       # Emergency Management Equipment
        },
    },
}


def fetch(path: str, cache: Path | None, name: str):
    if cache:
        f = cache / name
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    req = urllib.request.Request(f"{API}/{path}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.load(resp)
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / name).write_text(json.dumps(data), encoding="utf-8")
    return data


def load(cache: Path | None):
    cats = {c["id"]: c for c in fetch(
        f"categories/listByMunicipalityId?municipalityId={MUNICIPALITY_ID}", cache, "categories.json")}
    items = fetch(f"statistics/municipalities/{MUNICIPALITY_ID}/lineItems", cache, "lineItems.json")
    return cats, items


def revenue_category(account: str) -> str:
    """The book's revenue category for an account number, or None."""
    parts = (account or "").split("-")
    head = parts[2][:2] if len(parts) > 2 else ""
    for prefix, name in REVENUE_RANGES:
        if head == prefix:
            return name
    return "Other Revenue"


class Ledger:
    """Every column of the book, indexed by account so lines can be joined.

    A line item is identified by its account number and the fund it sits in.
    That pair is stable across all six versions, which is what makes a row of
    the book's comparison table possible: the same account, five years of
    different numbers beside it.
    """

    # fy24 and fy24a are the same year twice, because the book prints it twice:
    # its department tables use a mid-production year-to-date snapshot, its fund
    # summaries use the finished year. Both are carried on every line so each
    # table on the page can show the figure the book shows in that table.
    COLUMNS = [
        ("adopted", ADOPTED),
        ("amended", AMENDED),
        ("fy25b", FY25_BUDGET),
        ("fy25e", FY25_ESTIMATE),
        ("fy24", FY24_YTD),
        ("fy24a", FY24_ACTUAL),
    ]

    def __init__(self, items, cats):
        self.cats = cats
        self.books = {key: Book(items, cats, *ver) for key, ver in self.COLUMNS}
        self.adopted = self.books["adopted"]

    def name(self, cat_id):
        c = self.cats.get(cat_id)
        return c["name"] if c else None

    def key(self, row):
        return (row["sub_account_number"], self.name(row.get("native_fund_id")))

    def rows(self, side):
        """Every account that carries a figure in any column, in book order.

        Keyed union rather than the adopted year alone: an account funded last
        year and zeroed this year is a real row of the book's table — the book
        prints it, with a blank in the last column — and dropping it would make
        a cut look like an account that never existed.
        """
        merged: dict = {}
        for col, _ in self.COLUMNS:
            for r in self.books[col].lines(side):
                k = self.key(r)
                slot = merged.setdefault(k, {
                    "account": r["sub_account_number"],
                    "name": r["name"],
                    "fund": self.name(r.get("native_fund_id")),
                    "department": self.name(r.get("native_category_id")),
                    "function": self.books[col].parent(r.get("native_category_id")),
                    "object": self.name(r.get("native_object_id")),
                    "values": {},
                })
                slot["values"][col] = (r["total"] or 0)
                # Labels and taxonomy come from the adopted year where it has
                # them: an account renamed since is shown under the name the
                # book prints, not the name a prior year used.
                if col == "adopted":
                    slot.update(name=r["name"], department=self.name(r.get("native_category_id")),
                                function=self.books[col].parent(r.get("native_category_id")),
                                object=self.name(r.get("native_object_id")))
        return [r for r in merged.values() if any(v for v in r["values"].values())]


def col_values(row):
    """The five column figures for one line, rounded to whole dollars.

    Cents exist only in the actual columns; budgets are whole dollars. Lines are
    rounded for display — a table of 2,000 rows reading $52,668.78 is harder to
    scan than $52,669 — but every total is summed from the unrounded figures and
    rounded once, or 2,000 half-cents would drift the citywide total off the
    book's printed one.
    """
    v = row["values"]
    return [round(v.get(c, 0)) for c, _ in Ledger.COLUMNS]


def agg(rows):
    """Column totals for a set of lines: summed raw, rounded once."""
    return [round(sum(r["values"].get(col, 0) for r in rows)) for col, _ in Ledger.COLUMNS]


def cols_dict(rows):
    return {col: v for (col, _), v in zip(Ledger.COLUMNS, agg(rows))}


def summary_row(name, rows, extra=None):
    out = {"name": name, **cols_dict(rows)}
    if extra:
        out.update(extra)
    return out


def build(cats, items):
    led = Ledger(items, cats)

    expense = led.rows("exp")
    revenue = led.rows("rev")
    for r in expense + revenue:
        r["cols"] = col_values(r)
    for r in revenue:
        r["category"] = revenue_category(r["account"])

    # --- citywide summaries, the book's opening tables -----------------------
    by_object = {}
    for r in expense:
        by_object.setdefault(r["object"], []).append(r)
    by_revcat = {}
    for r in revenue:
        by_revcat.setdefault(r["category"], []).append(r)

    citywide = {
        "revenue": [summary_row(n, by_revcat[n]) for n in REVENUE_ORDER if n in by_revcat],
        "expense": [summary_row(n, by_object[n]) for n in OBJECT_ORDER if n in by_object],
    }
    # Any object outside the book's printed order would silently vanish above.
    for n in by_object:
        if n not in OBJECT_ORDER and any(r["cols"][0] for r in by_object[n]):
            raise SystemExit(f"expense object not in OBJECT_ORDER: {n}")

    # --- fund → department → line -------------------------------------------
    funds = {}
    for r in expense:
        funds.setdefault(r["fund"], {"expense": [], "revenue": []})["expense"].append(r)
    for r in revenue:
        funds.setdefault(r["fund"], {"expense": [], "revenue": []})["revenue"].append(r)

    fund_blocks = []
    for fund, parts in funds.items():
        exp, rev = parts["expense"], parts["revenue"]
        # Funds with nothing budgeted this year are kept, not dropped: the book
        # gives several of them a section apiece, because they still carry the
        # prior-year columns — an impact-fee fund that collected money last year
        # and budgets none this year is a fact about this budget, not an absence.
        # They are flagged so the page can hold them back rather than lead with them.
        active = any(r["cols"][0] or r["cols"][1] for r in exp + rev)

        depts = {}
        for r in exp:
            depts.setdefault((r["function"], r["department"]), []).append(r)

        dept_blocks = []
        for (function, dept), lines in depts.items():
            lines = sorted(lines, key=lambda r: (-abs(r["cols"][0]), r["account"]))
            groups = {}
            for r in lines:
                groups.setdefault(r["object"], []).append(r)
            dept_blocks.append({
                "name": dept,
                "function": function,
                **cols_dict(lines),
                "groups": [summary_row(n, groups[n]) for n in OBJECT_ORDER if n in groups],
                "lines": [{"a": r["account"], "n": r["name"], "o": r["object"], "v": r["cols"]} for r in lines],
            })
        dept_blocks.sort(key=lambda d: (-d["adopted"], -d["fy25b"], d["name"]))

        rev_by_cat = {}
        for r in rev:
            rev_by_cat.setdefault(r["category"], []).append(r)
        rev_lines = sorted(rev, key=lambda r: (-abs(r["cols"][0]), r["account"]))

        fund_blocks.append({
            "name": fund,
            "active": active,
            "group": led.adopted.parent(next(
                (r["native_fund_id"] for r in led.adopted.rows
                 if led.name(r.get("native_fund_id")) == fund and r.get("native_fund_id")), None)),
            **cols_dict(exp),
            "revenue_total": cols_dict(rev),
            "revenue": [summary_row(n, rev_by_cat[n]) for n in REVENUE_ORDER if n in rev_by_cat],
            "expense_groups": [summary_row(n, g) for n in OBJECT_ORDER
                               for g in [[r for r in exp if r["object"] == n]] if g],
            "departments": dept_blocks,
            "revenue_lines": [{"a": r["account"], "n": r["name"], "c": r["category"], "v": r["cols"]}
                              for r in rev_lines],
        })
    fund_blocks.sort(key=lambda f: (not f["active"], -f["adopted"], -f["fy25b"], f["name"]))

    # --- the book's other index: one entry per department, funds pooled -------
    # Section 8 of the book gives each department a page and adds up every fund
    # that pays into it. Public Safety is the clearest case: its page includes a
    # line in the impact-fee fund, so the fund-first view splits a department
    # the book prints as one. Both indexes are the same lines, added up twice.
    dept_pool = {}
    for r in expense:
        dept_pool.setdefault(r["department"], []).append(r)
    department_index = []
    for name, lines in dept_pool.items():
        by_fund = {}
        for r in lines:
            by_fund.setdefault(r["fund"], []).append(r)
        department_index.append({
            "name": name,
            **cols_dict(lines),
            "functions": sorted({r["function"] for r in lines if r["function"]}),
            "funds": sorted(
                (summary_row(f, rs) for f, rs in by_fund.items()),
                key=lambda f: (-f["adopted"], -f["fy25b"], f["name"])),
        })
    department_index.sort(key=lambda d: (-d["adopted"], -d["fy25b"], d["name"]))

    # --- what the amendment did ---------------------------------------------
    delta_fund = [
        {"name": f["name"], "adopted": f["adopted"], "amended": f["amended"],
         "change": f["amended"] - f["adopted"]}
        for f in fund_blocks if f["amended"] != f["adopted"]
    ]
    delta_fund.sort(key=lambda d: -abs(d["change"]))
    delta_object = [
        {"name": row["name"], "adopted": row["adopted"], "amended": row["amended"],
         "change": row["amended"] - row["adopted"]}
        for row in citywide["expense"] if row["amended"] != row["adopted"]
    ]
    delta_object.sort(key=lambda d: -abs(d["change"]))

    # Summed from the line items rather than from the fund blocks: a fund that
    # carried money in FY2024 and nothing in FY2025-26 is not in the blocks, but
    # it is in the book's prior-year columns.
    exp_cols, rev_cols = cols_dict(expense), cols_dict(revenue)
    totals = {
        "adopted": exp_cols["adopted"],
        "amended": exp_cols["amended"],
        "revenue_adopted": rev_cols["adopted"],
        "fy25_budget": exp_cols["fy25b"],
        "fy25_estimate": exp_cols["fy25e"],
        "fy25_estimate_revenue": rev_cols["fy25e"],
        "fy24_actual": exp_cols["fy24a"],
        "fy24_actual_revenue": rev_cols["fy24a"],
        "line_count": len(expense) + len(revenue),
        # Two counts, because a department can be paid out of more than one fund:
        # 42 departments filed 73 times. The page says departments where it means
        # the first and rows where it means the second.
        "department_count": len(department_index),
        "fund_department_count": sum(len(f["departments"]) for f in fund_blocks if f["active"]),
        "fund_count": sum(1 for f in fund_blocks if f["active"]),
        "dormant_fund_count": sum(1 for f in fund_blocks if not f["active"]),
    }
    totals["carried_forward"] = totals["amended"] - totals["adopted"]

    all_funds, general_fund = chart_shape(led.adopted)

    return {
        "citywide": citywide,
        "funds": fund_blocks,
        "departments": department_index,
        "totals": totals,
        "amendment": {"by_fund": delta_fund, "by_object": delta_object},
        "all_funds": all_funds,
        "general_fund": general_fund,
    }


def verify(built, cats, items):
    """Check the rebuild against figures printed in the PDF. Any miss is fatal."""
    misses = []

    def same(label, got, want):
        # A dollar of tolerance, because the book prints the same figure to the
        # cent on one page and to the dollar on another — its citywide table
        # rounds where its General Fund table does not. Anything wider than
        # rounding is a real mismatch and is reported.
        if abs(float(got) - float(want)) > 1:
            misses.append(f"{label}: rebuilt {got:,.2f}, book prints {want:,.2f}")

    cw = BOOK_CHECKS["citywide"]
    t = built["totals"]
    same("citywide adopted expenditure", t["adopted"], cw["expense_total"])
    same("citywide adopted revenue", t["revenue_adopted"], cw["revenue_total"])
    same("citywide FY2025 budget", t["fy25_budget"], cw["fy25_budget_expense"])
    same("citywide FY2025 estimated actual", t["fy25_estimate"], cw["fy25_estimate_expense"])
    same("citywide FY2024 actual", t["fy24_actual"], cw["fy24_actual_expense"])

    for row in built["citywide"]["revenue"]:
        if row["name"] in cw["revenue"]:
            same(f"citywide revenue · {row['name']}", row["adopted"], cw["revenue"][row["name"]])
        if row["name"] in cw["fy24_revenue"]:
            same(f"citywide FY2024 revenue · {row['name']}", row["fy24a"], cw["fy24_revenue"][row["name"]])
    for row in built["citywide"]["expense"]:
        if row["name"] in cw["expense"]:
            same(f"citywide expense · {row['name']}", row["adopted"], cw["expense"][row["name"]])
        if row["name"] in cw["fy24_expense"]:
            same(f"citywide FY2024 expense · {row['name']}", row["fy24a"], cw["fy24_expense"][row["name"]])

    gf = next((f for f in built["funds"] if f["name"] == "General Fund"), None)
    if not gf:
        misses.append("General Fund missing entirely")
    else:
        g = BOOK_CHECKS["general_fund"]
        same("General Fund adopted expenditure", gf["adopted"], g["expense_total"])
        same("General Fund adopted revenue", gf["revenue_total"]["adopted"], g["revenue_total"])
        same("General Fund FY2025 budget", gf["fy25b"], g["fy25_budget_expense"])
        same("General Fund FY2025 estimated actual", gf["fy25e"], g["fy25_estimate_expense"])
        same("General Fund FY2024 actual", gf["fy24a"], g["fy24_actual_expense"])
        for row in gf["revenue"]:
            if row["name"] in g["revenue"]:
                same(f"General Fund revenue · {row['name']}", row["adopted"], g["revenue"][row["name"]])

        ps = next((d for d in gf["departments"] if d["name"] == "Public Safety"), None)
        p = BOOK_CHECKS["public_safety"]
        if not ps:
            misses.append("Public Safety department missing from the General Fund")
        else:
            # The book gives a department one page and pools every fund that
            # pays into it — Public Safety's page includes a $10,000 line that
            # sits in the impact-fee fund and, in ClearGov's taxonomy, under
            # Capital Improvements rather than Public Safety. So the pool is by
            # department name alone, which is what the printed page is.
            pooled = [d for f in built["funds"] for d in f["departments"]
                      if d["name"] == "Public Safety"]
            total = lambda col: sum(d[col] for d in pooled)
            same("Public Safety department total", total("adopted"), p["total"])
            same("Public Safety FY2024 (book's year-to-date column)", total("fy24"), p["fy24_ytd"])
            same("Public Safety FY2025 budget", total("fy25b"), p["fy25_budget"])
            for name, want in p["groups"].items():
                row = next((g2 for g2 in ps["groups"] if g2["name"] == name), None)
                same(f"Public Safety · {name}", row["adopted"] if row else 0, want)
            for account, want in p["accounts"].items():
                line = next((l for l in ps["lines"] if l["a"] == account), None)
                same(f"Public Safety · {account}", line["v"][0] if line else 0, want)

    if misses:
        raise SystemExit(
            "the rebuild no longer matches the published FY2025-26 book:\n  - "
            + "\n  - ".join(misses)
            + "\n\nEvery figure above is printed in 25-26budget.pdf. A mismatch means either "
              "ClearGov restated a version or the version ids at the top of this file no "
              "longer point at the book — check before shipping."
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", type=Path, help="directory to cache the two API responses in")
    args = ap.parse_args()

    cats, items = load(args.cache)
    built = build(cats, items)
    verify(built, cats, items)

    t = built["totals"]
    out = {
        "_comment": (
            "Eagle Mountain's FY2025-26 ADOPTED budget, rebuilt line by line from the city's own "
            "ClearGov line items and checked against the published budget book (Eagle Mountain City "
            "Adopted Budget FY 2025-2026, 646pp). Every fund, every department, every account, with "
            "the four comparison columns the book prints beside them, plus the same budget as it now "
            "stands after mid-year amendment. Budgeted, not actual, except where a column says actual. "
            "Interfund transfers appear on both sides of any all-funds figure. Built by "
            "ingest/build_budget_book.py, which fails if it stops reproducing the book's printed totals."
        ),
        "fiscal_year": "2025-26",
        "fiscal_year_label": "FY2025–26",
        "status": "adopted",
        "period": "1 July 2025 – 30 June 2026",
        "source": {
            "title": "Eagle Mountain City Adopted Budget FY 2025-2026",
            "publisher": "ClearGov",
            "url": "https://cleargov.com/api/statistics/municipalities/315552/lineItems",
            "document": "25-26budget.pdf, 646 pages, produced by the city through ClearGov",
            "note": (
                "The figures are the line items behind the book rather than text scraped from it — "
                "the same data one step earlier, with the account number attached and no rounding. "
                "The build asserts them against totals printed in the book at citywide, fund, "
                "department and single-account level."
            ),
            "caveat": (
                "The book's cover calls it the adopted budget; its page footers still read “Tentative "
                "Budget”, an artifact of the template it was produced from. The figures are the adopted ones."
            ),
        },
        "columns": [
            {"key": "fy24a", "label": "FY2024 actual", "note": "the finished year, as the book's fund summary tables print it"},
            {"key": "fy24", "label": "FY2024 actual (department pages)", "note": "the same year as the book's department tables print it — a year-to-date snapshot taken while the book was being made, so it runs a little lower"},
            {"key": "fy25e", "label": "FY2025 estimated actual", "note": "the city's own estimate of how the prior year would finish"},
            {"key": "fy25b", "label": "FY2025 budget", "note": "the prior year as adopted — the FY2025 book's own figures"},
            {"key": "adopted", "label": "FY2025–26 adopted", "note": "the budget this book publishes"},
            {"key": "amended", "label": "FY2025–26 as amended", "note": "the same year as it stands now, with unfinished capital carried forward into it"},
        ],
        "totals": t,
        "amendment": {
            "note": (
                f"The adopted budget was ${t['adopted']:,}. The same year now stands at "
                f"${t['amended']:,} — ${t['carried_forward']:,} more, nearly all of it capital work "
                "that was not finished by 30 June 2025 and rolled forward. The book's own opening "
                "page says to expect exactly that. Both figures are real: one is what the Council "
                "voted for, the other is what the year became."
            ),
            **built["amendment"],
        },
        "citywide": built["citywide"],
        "departments": built["departments"],
        "all_funds": built["all_funds"],
        "general_fund": built["general_fund"],
        "funds": built["funds"],
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {OUT.relative_to(DOCS.parent)}  "
          f"adopted ${t['adopted']:,} · amended ${t['amended']:,} · "
          f"{t['fund_count']} funds · {t['department_count']} departments · {t['line_count']} lines")


if __name__ == "__main__":
    main()
