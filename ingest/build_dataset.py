"""
Parse every PDF in data/raw/, apply member normalization and topic tagging,
write per-meeting JSONs to data/parsed/, and roll the whole archive up into
docs/data.json (the file the public site reads).

  python -m ingest.build_dataset
  python -m ingest.build_dataset --only 2026-05-05__712.pdf

Schema of docs/data.json:

  {
    "generated_at": ISO timestamp,
    "source": "Eagle Mountain CivicClerk portal",
    "counts": { meetings, motions, votes, ord_lines, res_lines },
    "members": [{id,name,role,tenure_start,tenure_end,previous_role?}],
    "tags":    [{id,label}],
    "meetings":[{id,date,name,source_file,motion_count,ord_count,res_count}],
    "motions": [
      {id,meeting_id,date,page,agenda_ref,business_type,item_title,
       motion,outcome,tags:[tag_id],votes:[{member_id,vote}],
       raw_voters:[{name,vote}]   # only names that failed to resolve
      }
    ],
    "stats": {
      "by_member":   {member_id: {total,yes,no,abstain,excused,absent,
                                   yes_rate, by_tag:{tag_id:count}}},
      "agreement":   {"a|b": agreement_fraction, ...},   # alphabetical pair
      "by_tag_year": {tag_id: {year: count}},
      "by_outcome":  {"passed":n, "failed":n, "other":n}
    }
  }
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from . import parser  # noqa: E402

from .normalize import (  # noqa: E402
    load_members,
    load_tags,
    make_member_resolver,
    make_tagger,
)

RAW_DIR = ROOT / "data" / "raw"
PARSED_DIR = ROOT / "data" / "parsed"
DOCS_DATA = ROOT / "docs" / "data.json"


def parse_one(pdf: Path) -> dict:
    data = pdf.read_bytes()
    return parser.parse_document(pdf.name, data)


def event_id_from_name(name: str) -> int | None:
    # "<YYYY-MM-DD>__<eventId>.pdf"
    stem = Path(name).stem
    if "__" in stem:
        try:
            return int(stem.split("__", 1)[1])
        except ValueError:
            return None
    return None


def normalize_motion(raw_motion: dict, resolve, tag) -> tuple[dict, list[dict]]:
    """Convert a parser summary row into the public schema. Roll-call membership
    is reconstructed from the legacy `roll_call` string ("Name: Vote; …")."""
    votes: list[dict] = []
    unresolved: list[dict] = []
    rc = raw_motion.get("roll_call") or ""
    for seg in rc.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        if ":" not in seg:
            continue
        name, vote = seg.rsplit(":", 1)
        name = name.strip()
        vote = vote.strip()
        mid = resolve(name)
        if mid:
            votes.append({"member_id": mid, "vote": vote})
        elif name:
            unresolved.append({"name": name, "vote": vote})

    text = " ".join([
        raw_motion.get("item_title") or "",
        raw_motion.get("motion") or "",
        raw_motion.get("business_type") or "",
    ])
    tags = tag(text)

    return (
        {
            "page": raw_motion.get("page"),
            "agenda_ref": raw_motion.get("agenda_ref") or "",
            "business_type": raw_motion.get("business_type") or "",
            "item_title": raw_motion.get("item_title") or "",
            "motion": raw_motion.get("motion") or "",
            "outcome": raw_motion.get("outcome") or "",
            "tags": tags,
            "votes": votes,
        },
        unresolved,
    )


def classify_outcome(outcome: str) -> str:
    o = (outcome or "").lower()
    if not o:
        return "other"
    if "passed" in o or "approved" in o or "carried" in o:
        return "passed"
    if "fail" in o or "denied" in o or "not pass" in o:
        return "failed"
    return "other"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--only", default=None,
                   help="parse just one filename in data/raw/ (for debugging)")
    args = p.parse_args()

    members_doc = load_members()
    tags_doc = load_tags()
    resolve = make_member_resolver(members_doc)
    tag = make_tagger(tags_doc)

    pdfs = sorted(RAW_DIR.glob("*.pdf"))
    if args.only:
        pdfs = [p for p in pdfs if p.name == args.only]
        if not pdfs:
            print(f"no match in data/raw/ for {args.only!r}", file=sys.stderr)
            return 1

    PARSED_DIR.mkdir(parents=True, exist_ok=True)

    meetings: list[dict] = []
    motions: list[dict] = []
    motion_uid = 0
    all_unresolved: dict[str, int] = defaultdict(int)

    for pdf in pdfs:
        try:
            r = parse_one(pdf)
        except Exception as exc:
            print(f"  ! parse failed {pdf.name}: {exc}", file=sys.stderr)
            continue

        eid = event_id_from_name(pdf.name) or 0
        mdate = r.get("meeting_date") or pdf.name[:10]
        meeting_record = {
            "id": eid,
            "date": mdate,
            "source_file": pdf.name,
            "motion_count": len(r.get("motions", [])),
            "ord_count": len(r.get("ordinances", [])),
            "res_count": len(r.get("resolutions", [])),
        }
        meetings.append(meeting_record)

        # per-meeting full payload, including raw ord/res lines for drill-in
        per_meeting = {
            "meeting": meeting_record,
            "motions": [],
            "ordinances": r.get("ordinances", []),
            "resolutions": r.get("resolutions", []),
        }

        for rm in r.get("motions", []):
            norm, unresolved = normalize_motion(rm, resolve, tag)
            motion_uid += 1
            entry = {
                "id": motion_uid,
                "meeting_id": eid,
                "date": mdate,
                **norm,
            }
            if unresolved:
                entry["raw_voters"] = unresolved
                for u in unresolved:
                    all_unresolved[u["name"]] += 1
            motions.append(entry)
            per_meeting["motions"].append(entry)

        (PARSED_DIR / f"{pdf.stem}.json").write_text(
            json.dumps(per_meeting, indent=2, ensure_ascii=False)
        )

    # ===== aggregate stats =====
    by_member: dict[str, dict] = {}
    for m in members_doc["members"]:
        by_member[m["id"]] = {
            "total": 0, "yes": 0, "no": 0, "abstain": 0, "excused": 0,
            "absent": 0, "nay": 0, "yes_rate": 0.0,
            "by_tag": defaultdict(int),
        }

    pair_total: dict[tuple[str, str], int] = defaultdict(int)
    pair_agree: dict[tuple[str, str], int] = defaultdict(int)
    by_tag_year: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_outcome = {"passed": 0, "failed": 0, "other": 0}

    for mot in motions:
        year = (mot.get("date") or "")[:4]
        for t in mot.get("tags", []):
            by_tag_year[t][year] += 1
        by_outcome[classify_outcome(mot.get("outcome"))] += 1

        votes = mot.get("votes") or []
        for v in votes:
            mid = v["member_id"]
            vote = (v["vote"] or "").lower()
            row = by_member.setdefault(mid, {
                "total": 0, "yes": 0, "no": 0, "abstain": 0, "excused": 0,
                "absent": 0, "nay": 0, "yes_rate": 0.0, "by_tag": defaultdict(int),
            })
            row["total"] += 1
            if vote in ("yes", "no", "abstain", "excused", "absent", "nay"):
                row[vote] += 1
            for t in mot.get("tags", []):
                row["by_tag"][t] += 1

        # pairwise agreement
        for i in range(len(votes)):
            for j in range(i + 1, len(votes)):
                a, b = votes[i], votes[j]
                pair = tuple(sorted([a["member_id"], b["member_id"]]))
                pair_total[pair] += 1
                if _agreeable(a["vote"]) == _agreeable(b["vote"]):
                    pair_agree[pair] += 1

    for row in by_member.values():
        affirmative = row["yes"]
        opposed = row["no"] + row["nay"]
        denom = affirmative + opposed
        row["yes_rate"] = (affirmative / denom) if denom else 0.0
        row["by_tag"] = dict(row["by_tag"])

    agreement = {
        f"{a}|{b}": (pair_agree[(a, b)] / pair_total[(a, b)]) if pair_total[(a, b)] else 0.0
        for (a, b) in pair_total
    }
    agreement_n = {
        f"{a}|{b}": pair_total[(a, b)] for (a, b) in pair_total
    }

    counts = {
        "meetings": len(meetings),
        "motions": len(motions),
        "votes": sum(len(m.get("votes") or []) for m in motions),
        "ord_lines": sum(m["ord_count"] for m in meetings),
        "res_lines": sum(m["res_count"] for m in meetings),
    }

    tag_counts = defaultdict(int)
    for m in motions:
        for t in m.get("tags", []):
            tag_counts[t] += 1

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Eagle Mountain CivicClerk portal",
        "counts": counts,
        "members": [
            {k: v for k, v in m.items() if k != "aliases"}
            for m in members_doc["members"]
        ],
        "tags": [
            {"id": t["id"], "label": t["label"], "motion_count": tag_counts.get(t["id"], 0)}
            for t in tags_doc["tags"]
        ],
        "meetings": sorted(meetings, key=lambda m: m["date"], reverse=True),
        "motions": motions,
        "stats": {
            "by_member": by_member,
            "by_tag_year": {k: dict(v) for k, v in by_tag_year.items()},
            "by_outcome": by_outcome,
            "agreement": agreement,
            "agreement_n": agreement_n,
        },
        "unresolved_voters": dict(sorted(
            all_unresolved.items(), key=lambda kv: kv[1], reverse=True
        )[:50]),
    }

    DOCS_DATA.parent.mkdir(parents=True, exist_ok=True)
    DOCS_DATA.write_text(json.dumps(out, ensure_ascii=False))
    size_kb = DOCS_DATA.stat().st_size / 1024
    print(
        f"Wrote {DOCS_DATA.relative_to(ROOT)} ({size_kb:.0f} KB)  "
        f"meetings={counts['meetings']}  motions={counts['motions']}  "
        f"votes={counts['votes']}"
    )
    if all_unresolved:
        print(f"Top unresolved voters (consider adding to councilmembers.json):")
        for n, c in sorted(all_unresolved.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {n!r:35s}  x{c}")
    return 0


def _agreeable(vote: str) -> str:
    v = (vote or "").lower()
    if v in ("yes",):
        return "y"
    if v in ("no", "nay"):
        return "n"
    return "x"


if __name__ == "__main__":
    sys.exit(main())
