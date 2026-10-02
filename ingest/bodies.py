"""
Registry of the government bodies the tracker covers — the single source of
truth shared by the crawl, the dataset build, and (via the generated
docs/bodies.json) the public site's body switcher.

Each body has one of two sources:
  "civicclerk" — crawled from the CivicClerk OData portal by `category` name.
  "manual"     — no crawl; minutes PDFs are dropped into the body's raw_dir by
                 hand (named "<YYYY-MM-DD>__<n>.pdf") and parsed like the rest.

City Council keeps the original flat paths (data/raw, data/parsed, docs/data.json)
so existing files and links never move. New bodies nest under per-body subdirs
and get their own docs/data.<id>.json, loaded on demand by the site.

Helpers:
  all_bodies()        -> list[dict]            every body config
  public_bodies()     -> list[dict]            the ones the site shows (not "hidden")
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
        "authority": (
            "The City Council is Eagle Mountain's elected governing body. It decides: "
            "its votes enact ordinances, adopt budgets, set tax and utility rates, and "
            "approve rezones and development agreements outright."
        ),
        "actor": "the council",
        "source": "civicclerk",
        "category": "City Council",
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
        "authority": (
            "The Planning Commission is an appointed body. On most land-use items it "
            "RECOMMENDS to the City Council rather than deciding: the council votes "
            "later and can go the other way. Say \"recommended\", not \"approved\", "
            "unless the motion itself shows the commission held the final say (site "
            "plans and conditional use permits are the usual exceptions). Never write "
            "that the city approved something the commission only recommended."
        ),
        "actor": "the commission",
        "source": "civicclerk",
        "category": "Planning Commission",
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
        "authority": (
            "The Community Services Board is an appointed advisory board covering parks, "
            "recreation, arts and community events. It ADVISES: its motions are "
            "recommendations and requests to the City Council and to city staff, not "
            "decisions the city is bound by. Never write that the city approved "
            "something this board voted on."
        ),
        "actor": "the board",
        "source": "manual",                          # no CivicClerk category — PDFs dropped in
        "category": None,
        "members": "members.community-services-board.json",
        "summaries": "meeting_summaries.community-services-board.json",
        "motion_summaries": "motion_summaries.community-services-board.json",
        "raw_dir": "data/raw/community-services-board",
        "parsed_dir": "data/parsed/community-services-board",
        "data_file": "docs/data.community-services-board.json",
        "default": False,
        # Off the public site for now: still crawled and built, but left out of
        # bodies.json, the meeting pages, the sitemap and the site agent.
        "hidden": True,
    },
    {
        "id": "redevelopment-agency-board",
        "label": "Redevelopment Agency Board",
        "authority": (
            "The Redevelopment Agency Board is the City Council sitting as the board of "
            "the city's redevelopment agency, a separate legal entity. It decides, but "
            "what it decides is agency business: tax increment, project area budgets, "
            "agency bonds and participation agreements. Write \"the agency\", not "
            "\"the city\", and do not imply a change to general city services."
        ),
        "actor": "the agency board",
        "source": "civicclerk",
        "category": "Redevelopment Agency Board",
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


def public_bodies() -> list[dict]:
    """The bodies the public site shows: every body not marked "hidden"."""
    return [b for b in BODIES if not b.get("hidden")]


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
