"""
Registry of the government bodies the tracker covers — the single source of
truth shared by the crawl, the dataset build, and (via the generated
docs/bodies.json) the public site's body switcher.

Each body has one of two sources:
  "civicclerk" — crawled from the CivicClerk OData portal by `category` name.
  "manual"     — no crawl; minutes PDFs are dropped into the body's raw_dir by
                 hand (named "<YYYY-MM-DD>__<n>.pdf") and parsed like the rest.

A civicclerk body may also carry `strays`: a regex matched against the event
name inside the portal's catch-all "General" category. The recorder files most
meetings under the body's own category, but a one-off — a special session called
at short notice, a retreat, a joint sitting — is sometimes created from the
"One Time Event" template and lands in "General" instead. Category filtering
alone never sees those, so a real meeting can be held, noticed and minuted while
every builder here looks straight past it. See civicclerk.STRAY_CATEGORY.

City Council keeps the original flat paths (data/raw, data/parsed, docs/data.json)
so existing files and links never move. New bodies nest under per-body subdirs
and get their own docs/data.<id>.json, loaded on demand by the site.

Helpers:
  all_bodies()        -> list[dict]            every body config
  get_body(body_id)   -> dict                  one config (raises on unknown id)
  default_body()      -> dict                  the body served when none is chosen
  raw_dir(body)       -> Path                  resolved against the repo root
  parsed_dir(body)    -> Path
  data_file(body)     -> Path
  members_path(body)  -> Path                  data/meta/<members file>
  summaries_path(body)-> Path                  data/meta/<summaries file>

Motion summaries (data/meta/<motion_summaries file>) are resolved by
ingest.summarize_motions.cache_path(), which falls back to a conventional name
if a body omits the key.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
META = ROOT / "data" / "meta"

# Order here is the order the switcher shows them in; the `default` body leads.
BODIES: list[dict] = [
    {
        "id": "city-council",
        "label": "City Council",
        "source": "civicclerk",
        "category": "City Council",
        # Council sittings the recorder filed under "General". A debate between
        # candidates is held in the chamber and named for the council, but it is
        # not the council meeting — no motion, no vote, nothing to minute — so it
        # is excluded by name. The visioning retreat is the opposite case: it
        # doesn't say "City Council" anywhere, and it is a noticed meeting of the
        # council with approved minutes, so it is named in.
        "strays": r"(?i)^(?!.*\bdebate\b)(?=.*(city council|annual visioning retreat))",
        "members": "councilmembers.json",
        "summaries": "meeting_summaries.json",
        "motion_summaries": "motion_summaries.json",
        "raw_dir": "data/raw",                      # legacy flat paths
        "parsed_dir": "data/parsed",
        "data_file": "docs/data.json",              # legacy URL preserved
        "default": True,
    },
    {
        "id": "planning-commission",
        "label": "Planning Commission",
        "source": "civicclerk",
        "category": "Planning Commission",
        # A joint sitting with the council is already carried in the Planning
        # Commission's own category under its own event id, so matching it here
        # too would file the same night twice. It stays with the council, which
        # is the body whose archive is otherwise missing it.
        "strays": r"(?i)^(?!.*\bdebate\b)(?!.*city council)(?=.*planning commission)",
        "members": "members.planning-commission.json",
        "summaries": "meeting_summaries.planning-commission.json",
        "motion_summaries": "motion_summaries.planning-commission.json",
        "raw_dir": "data/raw/planning-commission",
        "parsed_dir": "data/parsed/planning-commission",
        "data_file": "docs/data.planning-commission.json",
        "default": False,
    },
    {
        "id": "community-services-board",
        "label": "Community Services Board",
        "source": "manual",                          # no CivicClerk category — PDFs dropped in
        "category": None,
        "members": "members.community-services-board.json",
        "summaries": "meeting_summaries.community-services-board.json",
        "motion_summaries": "motion_summaries.community-services-board.json",
        "raw_dir": "data/raw/community-services-board",
        "parsed_dir": "data/parsed/community-services-board",
        "data_file": "docs/data.community-services-board.json",
        "default": False,
    },
    {
        "id": "redevelopment-agency-board",
        "label": "Redevelopment Agency Board",
        "source": "civicclerk",
        "category": "Redevelopment Agency Board",
        # The board sits immediately after the council on the same night, and
        # three of its 2025 meetings were created as one-off events.
        "strays": r"(?i)redevelopment agency",
        "members": "members.redevelopment-agency-board.json",
        "summaries": "meeting_summaries.redevelopment-agency-board.json",
        "motion_summaries": "motion_summaries.redevelopment-agency-board.json",
        "raw_dir": "data/raw/redevelopment-agency-board",
        "parsed_dir": "data/parsed/redevelopment-agency-board",
        "data_file": "docs/data.redevelopment-agency-board.json",
        "default": False,
    },
]


def all_bodies() -> list[dict]:
    return BODIES


def get_body(body_id: str) -> dict:
    for b in BODIES:
        if b["id"] == body_id:
            return b
    valid = ", ".join(b["id"] for b in BODIES)
    raise KeyError(f"unknown body {body_id!r}; valid ids: {valid}")


def default_body() -> dict:
    for b in BODIES:
        if b.get("default"):
            return b
    return BODIES[0]


def raw_dir(body: dict) -> Path:
    return ROOT / body["raw_dir"]


def parsed_dir(body: dict) -> Path:
    return ROOT / body["parsed_dir"]


def data_file(body: dict) -> Path:
    return ROOT / body["data_file"]


def members_path(body: dict) -> Path:
    return META / body["members"]


def summaries_path(body: dict) -> Path:
    return META / body["summaries"]
