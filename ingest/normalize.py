"""
Apply council-member name normalization and topic tagging to parsed motions.

Reads:
  data/meta/councilmembers.json
  data/meta/tags.json

Exports:
  load_members(), load_tags()
  resolve_member_id(name) -> str | None
  tag_motion(motion_dict) -> list[str]   # list of tag ids
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
META = ROOT / "data" / "meta"


def load_members(filename: str = "councilmembers.json") -> dict:
    """Load a body's member registry from data/meta/<filename>. Defaults to the
    City Council roster; other bodies pass their own (e.g.
    "members.planning-commission.json")."""
    return json.loads((META / filename).read_text())


def load_tags() -> dict:
    return json.loads((META / "tags.json").read_text())


def _alias_map(members_doc: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in members_doc.get("members", []):
        for a in m.get("aliases", []) or []:
            out[_norm_name(a)] = m["id"]
        # canonical name should resolve too even if not listed
        out.setdefault(_norm_name(m["name"]), m["id"])
    return out


def _norm_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z\s\-']", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def make_member_resolver(members_doc: dict):
    aliases = _alias_map(members_doc)
    ignore = {_norm_name(x) for x in (members_doc.get("ignore") or [])}

    def resolve(name: str) -> str | None:
        n = _norm_name(name)
        if not n or n in ignore:
            return None
        return aliases.get(n)

    return resolve


def make_ignore_check(members_doc: dict):
    ignore = {_norm_name(x) for x in (members_doc.get("ignore") or [])}
    return lambda name: _norm_name(name) in ignore


def make_tagger(tags_doc: dict):
    rules = []
    for t in tags_doc.get("tags", []):
        pats = [re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
                for kw in t.get("keywords", [])]
        rules.append((t["id"], pats))

    def tag(text: str) -> list[str]:
        if not text:
            return []
        hits: list[str] = []
        for tid, pats in rules:
            for p in pats:
                if p.search(text):
                    hits.append(tid)
                    break
        return hits

    return tag
