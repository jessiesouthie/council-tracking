"""Build docs/data.staffing.json — how many people Eagle Mountain employs, and
how that compares with cities its own size.

Twice in August 2026, on the record, the city manager told the Council that
"our city staff is about the third of the size of city staff of equivalent
population" — once at the hearing on the property-tax increase the referendum
is now aimed at. The claim underwrites the 28 new positions on the Projections
page. Nobody had checked it, and it is checkable: every Utah city publishes an
audited ten-year FTE schedule, so the comparison the sentence asserts can just
be done.

The complication is that it cannot be done naively. Eagle Mountain employs
nobody in public safety — patrol is a contract with the Utah County Sheriff
(Eagle Mountain Division), fire is Unified Fire Authority, a separate district
funded through the UFSA levy that already appears on the Tax page. So the city
reports 0 FTE for police and 0 for fire while Murray and Logan staff both, and
public safety is about a third of their payrolls. Comparing raw headcount
compares service-delivery models, not staffing levels. This script therefore
reports three measures side by side, and the page shows all three:

  raw               every FTE the city employs
  excl_public_safety  minus police, fire, ambulance, dispatch, animal control,
                    crossing guards and emergency management — the services
                    Eagle Mountain buys rather than staffs
  core              the above, minus utilities as well, because Logan and
                    Murray run municipal power and Eagle Mountain does not

Where the figures come from. Each city's ACFR statistical section carries the
GASB 44 schedule "Full-time Equivalent City Government Employees by
Function/Program", ten years wide. PEER_SOURCES below names the PDF for each,
because there is no pattern to find them by: Herriman publishes to an S3
bucket, Lehi and Draper to hashed CMS paths, Riverton to a bare directory. That
list is data to refresh once a year, not something to automate.

There is no central Utah dataset of municipal FTEs to use instead. opendata.
utah.gov is decommissioned; Transparent Utah publishes per-employee pay, not
FTE, from a Salesforce app with no documented endpoint. The ACFRs are the
source, one city at a time.

Population comes from the Census Bureau's sub-county estimates for every city,
not from each city's own ACFR, so the denominator is one consistent series.
This matters: Eagle Mountain's FY2024 ACFR claims 66,500 residents where the
Census estimate is 60,575, and the city's own working figures in council run
higher still (77,000 in the Sheriff's workload study, "almost 80,000" at the
tax hearing). Using each city's self-reported number would let every city pick
its own denominator. The gap is reported rather than smoothed over.

Parsing. pdftotext -layout preserves the column grid, so the year header gives
each column's character span and rows are read by position. Two things in the
source documents make a naive parse wrong, and both are handled explicitly:

  - Subtotal rows are detected structurally, as rows equal to the rows above
    them, not by looking for the word "total". Herriman's FY2024 schedule
    prints its public safety subtotal as "Toal Public Safety", and a parser
    that trusts the label counts it as a department and overstates the city by
    60 FTE.
  - Eagle Mountain's own schedule has a broken header: the PDF lays "Last Ten
    Fiscal Years" over the year row, so no single line of text carries all
    eleven years in their true positions. The columns are recovered from the
    data rows, which are aligned normally.

ACFR_CHECKS holds the total each city prints for FY2024, read off the page. The
build fails if a rebuild stops reproducing them. That check is the whole
defense against a silent column-alignment bug, which is exactly the failure
mode that would produce a plausible-looking wrong answer.

Usage:  python -m ingest.build_staffing [--cache DIR]

--cache reuses (and fills) a local copy of the eight ACFRs and the two Census
files; that is ~60MB of downloads and none of it changes between runs.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import subprocess
import urllib.request
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
OUT = DOCS / "data.staffing.json"

RETRIEVED = "2026-08-18"
COMPARE_YEAR = 2024  # the newest fiscal year every city in the set publishes

UA = "civicrollcall.com staffing comparison (contact via civicrollcall.com)"

# ---------------------------------------------------------------------------
# The subject city.
# ---------------------------------------------------------------------------
EAGLE_MOUNTAIN = {
    "slug": "eaglemountain",
    "name": "Eagle Mountain",
    "census_name": "Eagle Mountain city",
    "url": "https://eaglemountain.gov/wp-content/uploads/2025/01/EMC-ACFR-2024.pdf",
    "title": "Eagle Mountain City ACFR, fiscal year ended 30 June 2024",
    "page": 111,
    "fiscal_year": 2024,
}

# ---------------------------------------------------------------------------
# The peer set: of the PEER_RANK_DEPTH Utah cities nearest Eagle Mountain in
# population, the ones that publish a comparable staffing schedule. "Nearest" is
# computed in build() from the Census estimates, never asserted here, so the
# rule can be re-checked rather than taken on trust.
#
# This list and EXCLUDED below must between them account for every city inside
# the rank cut. verify_peer_rule() enforces that and fails the build otherwise.
# The check exists because the first version of this file did not have it: two
# cities outside the cut (Lehi, ranked 30th and half again Eagle Mountain's
# size) had been added by hand, and Bountiful — inside the cut — had been
# dropped without a word when its download 404'd. A page that criticises other
# people's comparisons for hiding their selection cannot make its own by feel.
# ---------------------------------------------------------------------------
PEER_RANK_DEPTH = 15
PEER_SOURCES = {
    "herriman": {
        "name": "Herriman",
        "census_name": "Herriman city",
        "url": "https://herriman-website-files.s3.us-west-1.amazonaws.com/finance_reports/CAFR/99+12-31-24+ACFR+FINAL.pdf",
        "title": "Herriman City ACFR, fiscal year ended 30 June 2024",
        "page": 139,
        "fiscal_year": 2024,
    },
    "logan": {
        "name": "Logan",
        "census_name": "Logan city",
        "url": "https://cms9files.revize.com/loganut/departments/finance/FY%202024%20Final%20ACFR%20with%20Cover.pdf",
        "title": "City of Logan ACFR, fiscal year ended 30 June 2024",
        "page": 105,
        "fiscal_year": 2024,
    },
    "murray": {
        "name": "Murray",
        "census_name": "Murray city",
        "url": "https://murray.utah.gov/DocumentCenter/View/17252",
        "title": "Murray City ACFR, fiscal year ended 30 June 2025",
        "page": None,
        "fiscal_year": 2025,
        "note": "FY2025 report; its ten-year schedule carries the FY2024 column used here.",
    },
    "draper": {
        "name": "Draper",
        "census_name": "Draper city",
        "url": "https://www.draperutah.gov/media/rqxfju1m/draper-city-acfr-fy25.pdf",
        "title": "City of Draper ACFR, fiscal year ended 30 June 2025",
        "page": None,
        "fiscal_year": 2025,
        "note": "FY2025 report; its ten-year schedule carries the FY2024 column used here.",
    },
    "riverton": {
        "name": "Riverton",
        "census_name": "Riverton city",
        "url": "https://www.rivertonutah.gov/departments/finance/cafr/riverton-city-acfr-2024.pdf",
        "title": "Riverton City ACFR, fiscal year ended 30 June 2024",
        "page": None,
        "fiscal_year": 2024,
    },
    "americanfork": {
        "name": "American Fork",
        "census_name": "American Fork city",
        "url": "https://www.americanfork.gov/DocumentCenter/View/17570/FY-2024-Annual-Comprehensive-Financial-Report",
        "title": "City of American Fork ACFR, fiscal year ended 30 June 2024",
        "page": 141,
        "fiscal_year": 2024,
        "note": "American Fork prints its schedule as two five-year tables; the "
                "later one, covering 2020-2024, is the one read here.",
    },
}

# Cities in the size band that had to be left out, and why. Naming them is the
# difference between a peer set and a selection.
EXCLUDED = [
    {"city": "Millcreek", "reason": "Publishes basic financial statements only: 64 "
     "pages with no statistical section, so no staffing schedule exists to read."},
    {"city": "Taylorsville", "reason": "The city's finance pages refuse automated "
     "requests (HTTP 403), so the report could not be retrieved."},
    {"city": "Saratoga Springs", "reason": "The city's public audit archive stops at "
     "FY2017. No recent report is posted where it can be found: worth noting on its "
     "own account, since this is the city Eagle Mountain is most often compared with."},
    {"city": "Spanish Fork", "reason": "Publishes basic financial statements only: no "
     "statistical section, so no staffing schedule exists to read."},
    {"city": "Bountiful", "reason": "Its report is served through a viewer that returns "
     "a web page rather than the PDF, and could not be retrieved for this build."},
    {"city": "Tooele", "reason": "Publishes an audit report with no statistical "
     "section, so no staffing schedule exists to read."},
    {"city": "Cedar City", "reason": "No finance or archive page could be located that "
     "serves the report."},
    {"city": "Syracuse", "reason": "Publishes a schedule of full-time employees rather "
     "than full-time equivalents: a headcount of permanent staff that leaves part-time "
     "and seasonal work out entirely. That is a different measure from the one every "
     "other city here reports, and mixing the two would overstate nobody consistently."},
    {"city": "Roy", "reason": "Same as Syracuse: reports a headcount of full-time "
     "employees, not full-time equivalents, so it is not comparable with the rest."},
]

# ---------------------------------------------------------------------------
# Read off the PDFs. Every figure below is the total printed on the schedule's
# own page for the comparison year; the build fails if the rebuild misses any.
# Nothing here is computed — that is the point.
# ---------------------------------------------------------------------------
ACFR_CHECKS = {
    "eaglemountain": 146.57,
    "herriman": 198.33,
    "logan": 432.00,
    "murray": 415.00,
    "draper": 309.93,
    "riverton": 165.96,
    "americanfork": 292.40,
}

# Eagle Mountain's own ten-year column, read off page 111, which is what the
# growth section rests on. Checked separately because it is the one series the
# page draws a line through.
EM_TOTALS = {
    2014: 84.35, 2015: 72.48, 2016: 76.00, 2017: 84.02, 2018: 91.61,
    2019: 104.09, 2020: 113.09, 2021: 112.50, 2022: 113.25, 2023: 137.45,
    2024: 146.57,
}

CENSUS = {
    # Vintage 2025, released mid-2026. It supersedes vintage 2024 rather than
    # extending it: every year back to 2020 is re-estimated, and Eagle Mountain's
    # 2024 figure moved from 60,575 to 61,362. Always take the newest vintage
    # whole; mixing vintages inside one series invents growth that did not happen.
    "2020-2025": "https://www2.census.gov/programs-surveys/popest/datasets/"
                 "2020-2025/cities/totals/sub-est2025.csv",
    "2010-2019": "https://www2.census.gov/programs-surveys/popest/datasets/"
                 "2010-2019/cities/totals/sub-est2019_49.csv",
}

# Utah's own estimate, carried as a cross-check rather than as the denominator.
# The Utah Population Committee published its first statewide subcounty series in
# May 2026, covering July 2024 to July 2025. Two bodies estimating the same city
# by different methods and landing within 2% of each other is worth showing: it
# is what makes the gap against the city's own working figure meaningful rather
# than just another number.
UPC = ("https://d36oiwf74r1rap.cloudfront.net/wp-content/uploads/2026/05/"
       "2025-UPC-subcounty-dataset.csv")
UPC_SOURCE = {
    "title": "Utah Population Committee subcounty estimates, 2025",
    "publisher": "Kem C. Gardner Policy Institute, University of Utah",
    "url": "https://gardner.utah.edu/demographics/population-estimates/",
    "note": "Utah's first statewide subcounty estimates, released May 2026.",
}

# What the city itself says, in its own meetings. Not used in any calculation —
# quoted so the page can show the gap rather than pretend it away.
CITY_CLAIMED_POPULATION = [
    {"figure": 66500, "year": 2024, "source": "Eagle Mountain City ACFR 2024, statistical section",
     "note": "The city's own demographic table, for the same year the staff count covers."},
    {"figure": 77000, "as_of": "2026-04-14", "source": "Utah County Sheriff's Office workload study, presented to Council",
     "note": "Used as the service population behind the officers-per-1,000 figure."},
    {"figure": 80000, "as_of": "2026-08-06", "source": "City officials at the property-tax hearing",
     "note": "Given as \u201calmost 80,000\u201d."},
]

# ---------------------------------------------------------------------------
# Mapping each city's own department labels onto one common set of functions.
#
# Cities do not agree on how to group a payroll. Herriman keeps an
# "Administrative Services" block Eagle Mountain has no equivalent of; Logan
# files its landfill under "Environmental health"; Lehi puts Power and Water
# inside "Parks, Recreation and Culture". Comparing the printed groupings would
# compare the accountants, so every row is re-filed by what the people in it do.
#
# Rules match the row's own label, in order, first match wins. The group heading
# above a row is deliberately NOT used: pdftotext carries a heading forward
# across page breaks, so Herriman's Water and Storm Water rows arrive labeled
# "Public Safety" and Murray's entire schedule arrives labeled "Fire".
#
# PUBLIC_SAFETY is the consequential one. It is drawn to match the services
# Eagle Mountain buys instead of staffing. Courts are NOT in it: a justice court
# is a judicial function a city keeps whoever polices it, and Eagle Mountain has
# no court row to net off anyway.
# ---------------------------------------------------------------------------
RULES = [
    ("public_safety", r"police|officer|dispatch|fire\b|firefight|emt|ambulance|"
                      r"animal|crossing guard|emergency service|ordinance enforcement|"
                      r"^civilians$|^support staff$"),
    ("library",       r"library|literacy"),
    ("parks_rec",     r"parks|recreation|cemetery|senior|community event|fitness|"
                      r"golf|boat harbor|open space|trail"),
    ("utilities",     r"water|sewer|wastewater|storm|solid waste|power|electric|"
                      r"fiber|broadband|irrigation|utility|environmental health|"
                      r"garbage|refuse"),
    ("community_dev", r"planning|zoning|building|economic development|code enforcement|"
                      r"community development|development services"),
    ("public_works",  r"street|road|engineer|fleet|facilit|shop|public works|"
                      r"central garage|public infrastructure|highway|lighting"),
    ("general_gov",   r"general government|manager|"
                      r"administrat|executive|finance|treasur|recorder|attorney|legal|"
                      r"human resource|legislative|council|mayor|court|clerk|"
                      r"information|communication|gis|risk management|customer service|"
                      r"public information|non-departmental|energy|management|other"),
]
COMPILED = [(k, re.compile(p, re.I)) for k, p in RULES]

FUNCTION_LABELS = {
    "public_safety": "Police, fire & emergency",
    "library": "Library",
    "parks_rec": "Parks & recreation",
    "utilities": "Utilities",
    "community_dev": "Planning & development",
    "public_works": "Streets & public works",
    "general_gov": "General government",
    "unmapped": "Unclassified",
}
# Order drives the stacked bar and its legend, so it is also a color decision:
# community_dev and public_works take the two warm hues, which sit too close
# together in dark mode to be told apart when adjacent. Utilities between them
# separates the pair and still groups sensibly — public works and utilities are
# neighbors either way round.
FUNCTION_ORDER = ["general_gov", "public_safety", "community_dev", "utilities",
                  "public_works", "parks_rec", "library", "unmapped"]


def function_of(label: str) -> str:
    for key, rx in COMPILED:
        if rx.search(label):
            return key
    return "unmapped"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch(url: str, cache: Path | None, name: str) -> bytes:
    if cache:
        f = cache / name
        if f.exists():
            return f.read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / name).write_bytes(data)
    return data


def pdf_text(pdf: bytes, cache: Path | None, name: str) -> str:
    """pdftotext -layout. The layout flag is load-bearing: without the column
    grid every schedule collapses into an unparseable run of numbers."""
    if cache:
        f = cache / f"{name}.txt"
        if f.exists():
            return f.read_text(encoding="utf-8", errors="replace")
    proc = subprocess.run(["pdftotext", "-layout", "-", "-"], input=pdf,
                          capture_output=True, check=True)
    text = proc.stdout.decode("utf-8", errors="replace")
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / f"{name}.txt").write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------------------
# Parsing the schedule
# ---------------------------------------------------------------------------
HEAD = re.compile(r"full[\s–-]*time[\s–-]*equivalent", re.I)
YEARTOK = re.compile(r"\b(?:19|20)\d{2}\b")
LOOSE_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
BAD_HEADING = re.compile(r"^\s*(note|total|source)\b", re.I)
# A number belonging to data rather than to a caption. Schedule numbers and
# footnote markers ("Schedule 18", "by Function (1)") are one or two digits and
# must survive; decimals and thousands-separated figures must not.
DATA_LIKE = re.compile(r"\d+\.\d|\d,\d{3}|\b\d{3,}\b")
SEASONAL = re.compile(r"seasonal|part[\s-]?time", re.I)
TRAILING_NUM = re.compile(r"\s+[\d.]+$")


def numeric(tok: str):
    if tok in {"-", "—", "–"}:
        return 0.0
    t = tok.replace(",", "").replace("%", "")
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def year_columns(line: str):
    cols = [(int(m.group()), m.start(), m.end()) for m in YEARTOK.finditer(line)]
    return cols if len(cols) >= 4 else None


def tokens_with_pos(line: str):
    return [(m.group(), m.start(), m.end()) for m in re.finditer(r"\S+", line)]


def find_tables(lines):
    """Every FTE schedule in the document: (heading_idx, header_idx, cols).

    A real heading is prose: no data-like number in it, and not a Note/Total/
    Source line. Without that filter the schedule's own total row and the
    footnote explaining how FTE is calculated both look like headings and pull
    in whatever table follows.
    """
    out, seen = [], set()
    for i, ln in enumerate(lines):
        if not HEAD.search(ln) or "...." in ln:
            continue
        if BAD_HEADING.match(ln) or DATA_LIKE.search(ln):
            continue
        for j in range(i, min(i + 14, len(lines))):
            cols = year_columns(lines[j])
            if cols:
                if j not in seen:
                    seen.add(j)
                    out.append((i, j, cols))
                break
    return out


def widen_columns(lines, hi, yi, cols):
    """Recover columns when the year header is broken across lines.

    Eagle Mountain's schedule is the case this exists for: the PDF lays the
    title "Last Ten Fiscal Years" over the year row, so pdftotext emits
    "2014 2015 2016 2017Fiscal / Last Ten 2018 / Years2019 2020 2021 2022 2023
    2024" across three lines, and no single line carries all eleven years in
    their true positions. The data rows below are aligned normally, so the
    widest of them gives the real column anchors and the years — gathered from
    the whole header block and sorted — fill them in order.
    """
    years = sorted({int(m.group()) for j in range(max(0, hi - 2), min(yi + 3, len(lines)))
                    for m in LOOSE_YEAR.finditer(lines[j])})
    if len(years) <= len(cols):
        return cols
    header_years = [c[0] for c in cols]
    if header_years != sorted(header_years):
        return cols  # descending-year table; filling by sorted year would invert it
    widest = None
    for ln in lines[yi + 1: yi + 60]:
        toks = [(m.group(), m.start(), m.end()) for m in re.finditer(r"\S+", ln)
                if numeric(m.group()) is not None]
        if len(toks) == len(years) and widest is None:
            widest = toks
    if not widest:
        return cols
    return [(y, s, e) for y, (_t, s, e) in zip(years, widest)]


def row_values(line: str, cols):
    """Assign numeric tokens to year columns.

    A row carrying a figure for every year is zipped straight across: exact, and
    what most rows are. Geometry is only needed for ragged rows — Draper leaves
    early years blank where a department did not exist yet — and there a wide
    figure's center can drift most of a column away from the header token above
    it, so nearest-center is a last resort rather than the rule.
    """
    nums = [(t, s, e) for t, s, e in tokens_with_pos(line) if numeric(t) is not None]
    if len(nums) == len(cols):
        return {c[0]: numeric(t) for c, (t, _s, _e) in zip(cols, nums)}
    out = {}
    for tok, s, e in nums:
        best = min(cols, key=lambda c: abs((s + e) / 2 - (c[1] + c[2]) / 2))
        if abs((s + e) / 2 - (best[1] + best[2]) / 2) <= 14:
            out[best[0]] = numeric(tok)
    return out


def parse_schedule(text: str, pick: int = -1):
    lines = text.split("\n")
    tables = find_tables(lines)
    if not tables:
        return None
    hi, yi, cols = tables[pick]
    cols = widen_columns(lines, hi, yi, cols)
    first_col = min(c[1] for c in cols)
    years = sorted(c[0] for c in cols)
    rows, group = [], None
    for ln in lines[yi + 1: yi + 220]:
        if not ln.strip():
            continue
        if re.match(r"^\s*(source|note[s:]|\(\d\)|\*)", ln.strip(), re.I):
            break
        label = TRAILING_NUM.sub("", ln[:first_col]).strip()
        vals = row_values(ln, cols)
        if not vals:
            if label and len(label) < 60 and not label[:1].isdigit():
                group = label
            continue
        if not label:
            continue
        rows.append({"group": group, "label": label, "values": vals})
    return {"years": years, "rows": rows}


def tol(x: float) -> float:
    """Absolute floor plus 0.15% of the figure.

    The floor covers ordinary cent rounding. The proportional part covers the
    fact that some cities' printed totals do not equal the sum of their own
    printed rows — Eagle Mountain's FY2023 column adds to 137.34 against a
    printed 137.45. That is a rounding artifact in the source document, not a
    parse error, and the build records the delta rather than hiding it. 0.15% is
    still far tighter than any real mistake: miscounting a subtotal moves a
    total by whole FTEs, not hundredths.
    """
    return max(0.06, 0.0015 * abs(x))


def classify(rows, year):
    """Split rows into leaves, subtotals and the grand total, structurally.

    Label matching is not safe here: Herriman's FY2024 schedule prints its
    public safety subtotal as "Toal Public Safety", and a parser that trusts the
    word "total" reads it as a department and overstates the city by 60 FTE.
    What a subtotal actually *is* — a row equal to the rows immediately above it
    — survives the typo.

    Rows below a seasonal / part-time line are dropped: Murray's schedule runs
    past its FTE total into headcount of seasonal staff and a combined "Total
    Employees", a different measure from the one this schedule reports.
    """
    out, buf, leaves = [], [], []
    for r in rows:
        if SEASONAL.search(r["label"]):
            break
        v = r["values"].get(year)
        if v is None:
            out.append({**r, "kind": "blank"})
            continue
        if len(buf) > 1 and abs(v - sum(buf)) < tol(v):
            out.append({**r, "kind": "subtotal"})
            buf = []
        else:
            out.append({**r, "kind": "leaf"})
            buf.append(v)
            leaves.append(v)
    total = sum(leaves)
    grand = None
    for r in out:
        if r["kind"] == "blank":
            continue
        v = r["values"][year]
        # A grand total accounts for every leaf but itself. Testing it that way
        # finds it whether the buffer pass called it a leaf (Herriman, Draper,
        # where a group subtotal intervenes) or a subtotal (Logan, Murray).
        rest = total - (v if r["kind"] == "leaf" else 0)
        if abs(v - rest) < tol(v) and len(leaves) > 2:
            grand = r
    if grand:
        if grand["kind"] == "leaf":
            leaves.remove(grand["values"][year])
        grand["kind"] = "grand"
    return out


def city_year(text, year, pick=-1):
    """Everything one city's schedule says about one year."""
    sched = parse_schedule(text, pick=pick)
    if not sched or year not in sched["years"]:
        return None
    rows = classify(sched["rows"], year)
    leaves = [r for r in rows if r["kind"] == "leaf"]
    grand = next((r for r in rows if r["kind"] == "grand"), None)
    by_function, departments = {}, []
    for r in leaves:
        fn = function_of(r["label"])
        v = r["values"][year]
        by_function[fn] = by_function.get(fn, 0.0) + v
        departments.append({"label": r["label"], "function": fn, "fte": round(v, 2)})
    total = sum(by_function.values())
    return {
        "years": sched["years"],
        "total": round(total, 2),
        "printed_total": round(grand["values"][year], 2) if grand else None,
        "by_function": {k: round(v, 2) for k, v in by_function.items()},
        "departments": departments,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------
def population(cache: Path | None):
    """{city name -> {year -> estimate}} from the two Census vintages."""
    pops: dict[str, dict[int, int]] = {}
    recent = fetch(CENSUS["2020-2025"], cache, "sub-est2025.csv").decode("latin-1")
    for r in csv.DictReader(io.StringIO(recent)):
        if r["STNAME"] != "Utah" or r["SUMLEV"] != "162":
            continue
        d = pops.setdefault(r["NAME"], {})
        for y in range(2020, 2026):
            try:
                d[y] = int(r[f"POPESTIMATE{y}"])
            except (KeyError, ValueError):
                pass
    older = fetch(CENSUS["2010-2019"], cache, "sub-est2019_49.csv").decode("latin-1")
    for r in csv.DictReader(io.StringIO(older)):
        if r["SUMLEV"] != "162":
            continue
        d = pops.setdefault(r["NAME"], {})
        for y in range(2010, 2020):
            try:
                d.setdefault(y, int(r[f"POPESTIMATE{y}"]))
            except (KeyError, ValueError):
                pass
    return pops


def upc_population(cache: Path | None):
    """{city -> {year -> estimate}} from Utah's own subcounty series.

    Rows are per county, so a city straddling a county line — Draper sits in both
    Salt Lake and Utah — arrives as two rows that have to be added, and taking
    either one alone would halve the city.
    """
    out: dict[str, dict[int, int]] = {}
    text = fetch(UPC, cache, "upc-subcounty-2025.csv").decode("latin-1")
    for r in csv.DictReader(io.StringIO(text)):
        if r.get("VARIABLE") != "Total Population" or r.get("MEASURE") != "Estimate":
            continue
        d = out.setdefault(r["PLACE"].strip(), {})
        for col, year in (("JULY12023", 2023), ("JULY12024", 2024), ("JULY12025", 2025)):
            try:
                d[year] = d.get(year, 0) + int(float(r[col]))
            except (KeyError, ValueError):
                pass
    return out


# ---------------------------------------------------------------------------
# Personnel cost, read from what the Budget page already publishes
# ---------------------------------------------------------------------------
def personnel_cost():
    """Wages and benefits over time, and the biggest departments by payroll.

    Read from docs/data.budget-book.json and docs/data.budget-next.json rather
    than re-derived from ClearGov, so the Budget and Staffing pages cannot drift
    apart. ClearGov carries no FTE or position count anywhere — personnel exists
    there only as dollars, which is exactly why the ACFR is needed for headcount
    and this is needed for cost.
    """
    book = json.loads((DOCS / "data.budget-book.json").read_text(encoding="utf-8"))
    nxt = json.loads((DOCS / "data.budget-next.json").read_text(encoding="utf-8"))

    cw = next(g for g in book["citywide"]["expense"] if g["name"] == "Personnel Services")
    nxt_obj = next(o for o in nxt["objects"] if o["name"] == "Personnel Services")

    series = [
        {"key": "fy24a", "label": "FY2024 actual", "value": cw["fy24a"]},
        {"key": "fy25b", "label": "FY2025 budget", "value": cw["fy25b"]},
        {"key": "adopted", "label": "FY2026 adopted", "value": cw["adopted"]},
        {"key": "amended", "label": "FY2026 amended", "value": cw["amended"]},
        {"key": "fy2027", "label": "FY2027 tentative", "value": nxt_obj["total"]},
    ]

    # Personnel Services by department, from the fund/department groups.
    depts = []
    for fund in book["funds"]:
        for d in fund.get("departments", []):
            for g in d.get("groups", []):
                if g.get("name") != "Personnel Services":
                    continue
                depts.append({
                    "department": d["name"],
                    "fund": fund["name"],
                    "adopted": g.get("adopted", 0),
                    "fy24a": g.get("fy24a", 0),
                })
    depts = [d for d in depts if d["adopted"]]
    depts.sort(key=lambda d: -d["adopted"])

    return {
        "series": series,
        "share_of_budget": {
            "personnel": cw["adopted"],
            "all_funds": book["totals"]["adopted"],
        },
        "by_department": depts[:20],
        "fy2027_change": nxt_obj["change"],
        "source": {
            "note": "Wages and benefits as budgeted, from the same ClearGov line "
                    "items the Budget page is built from. Dollars, not headcount: "
                    "ClearGov carries no position count.",
            "files": ["data.budget-book.json", "data.budget-next.json"],
        },
    }


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def measures(by_function: dict, pop: int):
    """The three per-capita measures, per 1,000 residents."""
    total = sum(by_function.values())
    ps = by_function.get("public_safety", 0.0)
    ut = by_function.get("utilities", 0.0)
    per = lambda v: round(v / (pop / 1000), 3)
    return {
        "total": round(total, 2),
        "public_safety": round(ps, 2),
        "utilities": round(ut, 2),
        "excl_public_safety": round(total - ps, 2),
        "core": round(total - ps - ut, 2),
        "per_1k": {
            "raw": per(total),
            "excl_public_safety": per(total - ps),
            "core": per(total - ps - ut),
        },
    }


def rank_cities(pops: dict, year: int):
    """Every Utah city, ordered by how close it is to Eagle Mountain in size."""
    target = pops[EAGLE_MOUNTAIN["census_name"]][year]
    out = []
    for name, series in pops.items():
        if name == EAGLE_MOUNTAIN["census_name"] or year not in series:
            continue
        plain = name.replace(" city", "").replace(" metro township", "")
        out.append((abs(series[year] - target), plain, series[year]))
    return sorted(out)


def verify_peer_rule(pops: dict):
    """The peer set must be exactly the rule, or the build fails.

    Every city inside the rank cut has to be either used or explicitly excluded
    with a reason, and nothing outside the cut may be used. This is the check
    that was missing the first time, when two cities from outside the cut had
    been added by hand and one inside it had been dropped in silence.
    """
    ranked = rank_cities(pops, COMPARE_YEAR)[:PEER_RANK_DEPTH]
    inside = [name for _gap, name, _pop in ranked]
    used = {src["name"] for src in PEER_SOURCES.values()}
    named = {x["city"] for x in EXCLUDED}

    problems = []
    for name in inside:
        if name not in used and name not in named:
            problems.append(
                f"{name} is among the {PEER_RANK_DEPTH} nearest cities but is neither "
                "compared nor listed in EXCLUDED with a reason")
    for name in sorted(used - set(inside)):
        problems.append(
            f"{name} is compared but falls outside the {PEER_RANK_DEPTH} nearest — "
            "either widen PEER_RANK_DEPTH and say so, or drop it")
    for name in sorted(named - set(inside)):
        problems.append(f"{name} is listed as excluded but is not inside the rank cut")
    if problems:
        raise SystemExit("peer set does not match the stated rule:\n  - "
                         + "\n  - ".join(problems))
    return ranked


def build(cache: Path | None):
    pops = population(cache)
    upc = upc_population(cache)
    ranked = verify_peer_rule(pops)

    def pop_of(census_name, year=COMPARE_YEAR):
        return pops[census_name][year]

    # --- Eagle Mountain -----------------------------------------------------
    em_pdf = fetch(EAGLE_MOUNTAIN["url"], cache, "eaglemountain.pdf")
    em_text = pdf_text(em_pdf, cache, "eaglemountain")
    em = city_year(em_text, COMPARE_YEAR)
    if not em:
        raise SystemExit("Eagle Mountain's FTE schedule did not parse")

    em_pop = pops[EAGLE_MOUNTAIN["census_name"]]
    em_series, em_deltas = [], []
    dept_series: dict[str, dict] = {}
    sched = parse_schedule(em_text)
    for y in sched["years"]:
        cy = city_year(em_text, y)
        if not cy:
            continue
        printed = EM_TOTALS.get(y)
        if printed is not None and abs(cy["total"] - printed) > tol(printed):
            raise SystemExit(
                f"Eagle Mountain {y}: rebuilt {cy['total']:.2f}, ACFR prints {printed:.2f}")
        if printed is not None and abs(cy["total"] - printed) > 0.005:
            em_deltas.append({"year": y, "rebuilt": cy["total"], "printed": printed,
                              "delta": round(cy["total"] - printed, 2)})
        em_series.append({
            "year": y,
            "fte": cy["total"],
            "printed": printed,
            "population": em_pop.get(y),
            "per_1k": round(cy["total"] / (em_pop[y] / 1000), 3) if em_pop.get(y) else None,
            "by_function": cy["by_function"],
        })
        # The department view wants each row across the decade, not just the
        # latest column: which teams the city actually grew is the question, and
        # a single year cannot answer it.
        for dep in cy["departments"]:
            rec = dept_series.setdefault(dep["label"], {
                "label": dep["label"], "function": dep["function"], "values": {}})
            rec["values"][str(y)] = dep["fte"]

    # --- peers --------------------------------------------------------------
    peers = []
    for slug, src in PEER_SOURCES.items():
        text = pdf_text(fetch(src["url"], cache, f"{slug}.pdf"), cache, slug)
        cy = city_year(text, COMPARE_YEAR)
        if not cy:
            raise SystemExit(f"{src['name']}: no FY{COMPARE_YEAR} column found")
        check = ACFR_CHECKS[slug]
        if abs(cy["total"] - check) > tol(check):
            raise SystemExit(
                f"{src['name']}: rebuilt {cy['total']:.2f}, ACFR prints {check:.2f}")
        p = pop_of(src["census_name"])
        m = measures(cy["by_function"], p)
        upc_p = upc.get(src["name"], {}).get(COMPARE_YEAR)
        # A city with essentially no police or fire on its own payroll buys the
        # service. Deciding it from the schedule beats asserting it here.
        model = "contract" if m["public_safety"] < 0.25 * m["total"] else "in_house"
        peers.append({
            "city": src["name"],
            "population": p,
            "fiscal_year": COMPARE_YEAR,
            "public_safety_model": model,
            "population_upc": upc_p,
            "by_function": cy["by_function"],
            "departments": cy["departments"],
            **m,
            "source": {
                "title": src["title"],
                "url": src["url"],
                "page": src.get("page"),
                "retrieved": RETRIEVED,
                "note": src.get("note"),
                "printed_total": cy["printed_total"],
            },
        })
    peers.sort(key=lambda c: -c["per_1k"]["raw"])

    pop_by_city = {name: pop for _gap, name, pop in ranked}
    excluded = [{**x, "population": pop_by_city.get(x["city"])} for x in EXCLUDED]

    em_pop_now = pop_of(EAGLE_MOUNTAIN["census_name"])
    em_measures = measures(em["by_function"], em_pop_now)

    em_upc = upc.get("Eagle Mountain", {})
    latest_year = max(em_pop.keys())
    how_many = {
        "comparison_year": COMPARE_YEAR,
        "comparison_population": em_pop_now,
        "latest_year": latest_year,
        "census": {y: em_pop[y] for y in sorted(em_pop) if y >= 2020},
        "upc": {y: v for y, v in sorted(em_upc.items())},
        "census_latest": em_pop.get(latest_year),
        "upc_latest": em_upc.get(max(em_upc)) if em_upc else None,
        "city_claims": CITY_CLAIMED_POPULATION,
        "note": (
            "Two independent official estimates, the Census Bureau's and Utah's own, "
            "agree with each other closely and both run well below the figures the "
            "city uses in its own meetings. The comparison on this page has to use one "
            "series measured the same way for every city, so it uses the Census "
            "figure; if the city's higher number is right, Eagle Mountain is leaner "
            "still, not less lean."
        ),
    }

    # --- the claim ----------------------------------------------------------
    def median(vals):
        v = sorted(vals)
        n = len(v)
        return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2

    claim = {}
    for key in ("raw", "excl_public_safety", "core"):
        peer_vals = [c["per_1k"][key] for c in peers]
        med = median(peer_vals)
        mean = sum(peer_vals) / len(peer_vals)
        claim[key] = {
            "eagle_mountain": em_measures["per_1k"][key],
            "peer_median": round(med, 3),
            "peer_mean": round(mean, 3),
            "pct_of_median": round(em_measures["per_1k"][key] / med * 100),
            "pct_of_mean": round(em_measures["per_1k"][key] / mean * 100),
        }

    return {
        "eagle_mountain": {
            "population": em_pop_now,
            "fiscal_year": COMPARE_YEAR,
            "public_safety_model": "contract",
            "public_safety_note": (
                "Patrol is contracted from the Utah County Sheriff's Office (Eagle "
                "Mountain Division); fire and EMS come from Unified Fire Authority, a "
                "separate district funded through the UFSA levy. The 4.82 FTE the city "
                "does employ under public safety are crossing guards."
            ),
            "by_function": em["by_function"],
            "departments": em["departments"],
            **em_measures,
            "series": em_series,
            "department_series": sorted(
                dept_series.values(),
                key=lambda r: -(r["values"].get(str(COMPARE_YEAR)) or 0)),
            "printed_total_deltas": em_deltas,
            "source": {
                "title": EAGLE_MOUNTAIN["title"],
                "url": EAGLE_MOUNTAIN["url"],
                "page": EAGLE_MOUNTAIN["page"],
                "retrieved": RETRIEVED,
            },
        },
        "peers": peers,
        "peer_rule": {
            "depth": PEER_RANK_DEPTH,
            "measured_on": COMPARE_YEAR,
            "ranked": [
                {"rank": i, "city": name, "population": pop,
                 "gap": gap, "used": name in {s["name"] for s in PEER_SOURCES.values()}}
                for i, (gap, name, pop) in enumerate(ranked, 1)
            ],
        },
        "how_many_people": how_many,
        "excluded": excluded,
        "claim": claim,
        "personnel_cost": personnel_cost(),
        "population_sources": {
            "census": {
                "title": "Annual Estimates of the Resident Population for Incorporated Places",
                "publisher": "US Census Bureau, vintage 2025",
                "url": CENSUS["2020-2025"],
                "note": "Used as the denominator for every city, so the comparison is "
                        "measured one way throughout.",
            },
            "upc": UPC_SOURCE,
        },
        "functions": [{"key": k, "label": FUNCTION_LABELS[k]} for k in FUNCTION_ORDER],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", type=Path,
                    help="directory to cache the ACFR PDFs and Census files in")
    args = ap.parse_args()

    built = build(args.cache)
    em = built["eagle_mountain"]
    claim = built["claim"]

    out = {
        "_comment": (
            "How many people Eagle Mountain employs, and how that compares with Utah "
            "cities its own size. Headcount is full-time equivalents from each city's "
            "audited ACFR statistical section (the GASB 44 schedule, ten years wide); "
            "population is the Census Bureau's sub-county estimate for every city, so "
            "the denominator is one consistent series rather than each city's own "
            "claim. Eagle Mountain employs nobody in police or fire (patrol is a Utah "
            "County Sheriff contract, fire is Unified Fire Authority), so raw "
            "per-capita headcount compares service-delivery models rather than "
            "staffing levels, and three measures are carried: raw, excluding public "
            "safety, and excluding public safety and utilities. Built by "
            "ingest/build_staffing.py, which fails if it stops reproducing the totals "
            "printed in each city's own report."
        ),
        "body": "city-council",
        "fiscal_year": COMPARE_YEAR,
        "fiscal_year_label": f"FY{COMPARE_YEAR}",
        "generated_from": RETRIEVED,
        "method": {
            "headcount": (
                "Full-time equivalents as each city reports them in the statistical "
                "section of its own audited annual financial report. FTE is not "
                "headcount: it counts hours, so two half-time staff are one FTE."
            ),
            "population": (
                "US Census Bureau sub-county estimates, vintage 2025, the newest "
                "published, used for every city including Eagle Mountain, so the "
                "comparison is measured one way throughout. Utah's own estimates, from "
                "the Utah Population Committee, are carried alongside as a check and "
                f"agree closely: for 2024 they differ by 122 people out of {em['population']:,}. "
                "The city's own figures run higher, and the page sets them out rather "
                "than choosing between them."
            ),
            "normalization": (
                "Cities group departments differently, so every row is re-filed by "
                "function from its own label. Public safety means police, fire, "
                "ambulance, dispatch, animal control, crossing guards and emergency "
                "management, the services Eagle Mountain buys rather than staffs. "
                "Courts are counted as general government: a justice court is a "
                "judicial function a city keeps whoever polices it."
            ),
            "why_2024": (
                "2024 is the most recent year Eagle Mountain has published an audited "
                "staff count for. The city's FY2025 audit was accepted by the Council, "
                "but the full annual report, the volume that carries the staffing "
                "schedule, is not posted on the city's finance page, where every year "
                "from 2019 to 2024 is. Comparing Eagle Mountain's 2024 against other "
                "cities' 2025 would not be a comparison, so every city here is 2024."
            ),
            "peer_rule": (
                f"Take the {PEER_RANK_DEPTH} Utah cities closest to Eagle Mountain in "
                "population, and compare the ones that publish a comparable staffing "
                "schedule. Every city inside that cut is either compared or named below "
                "with the reason it could not be: the build refuses to run otherwise, so "
                "no city can be dropped quietly and none can be added by hand. Nine of the "
                f"{PEER_RANK_DEPTH} are unusable: three publish no statistical section, two "
                "count full-time staff rather than full-time equivalents, and four could "
                "not be retrieved at all."
            ),
            "caveats": [
                "FTE excludes contracted labor entirely. That is the whole point for "
                "Eagle Mountain, and it means the raw column understates what the city "
                "actually commands.",
                "Murray's and Draper's newest reports are FY2025; the FY2024 column of "
                "their ten-year schedules is the one read here, so every city is the "
                "same year.",
                "Lehi's schedule prints no total; its figure is the sum of its own rows.",
                "Logan and Murray run municipal power utilities and Eagle Mountain does "
                "not, which is why the third measure sets utilities aside as well.",
            ],
        },
        "headline": {
            "fte": em["total"],
            "population": em["population"],
            "per_1k": em["per_1k"]["raw"],
            "public_safety_fte": em["public_safety"],
            "claim": (
                "Our city staff is about the third of the size of city staff of "
                "equivalent population."
            ),
            "claim_speaker": "Ben Reeves, City Manager",
            "claim_meetings": ["2026-08-06", "2026-08-04"],
            "verdict": {
                "raw": claim["raw"]["pct_of_median"],
                "excl_public_safety": claim["excl_public_safety"]["pct_of_median"],
                "core": claim["core"]["pct_of_median"],
            },
        },
        **built,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {OUT.relative_to(DOCS.parent)}  "
          f"{em['total']} FTE · {em['population']:,} residents · "
          f"{em['per_1k']['raw']}/1k raw, {em['per_1k']['excl_public_safety']}/1k excl "
          f"public safety · {len(built['peers'])} peers · "
          f"claim: {claim['raw']['pct_of_median']}% of peer median raw, "
          f"{claim['excl_public_safety']['pct_of_median']}% excluding public safety")


if __name__ == "__main__":
    main()
