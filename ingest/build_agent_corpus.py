"""
Build the knowledge corpus the site's Q&A agent retrieves from.

The agent (a Cloudflare Worker that proxies Claude — see worker/) can't run the
site's Python or read data/. It answers questions by searching one static file,
docs/agent-corpus.json, which this script assembles from everything the site
already publishes:

  * every motion, with its plain-English enrichment and full roll-call vote
  * every meeting (as a compact record, plus the prose summary when transcribed)
  * transcript passages, windowed so a quote can be retrieved and cited
  * each council member, their role and tenure, and how often they vote yes
  * the proposed tax change and the adopted budget
  * a short per-body overview so the agent knows what's on the site

Each corpus entry is a self-contained, *citable* chunk: it carries a `url` that
deep-links back into the site (motions.html?id=, meetings.html?id=, …) so the
agent's answers can point the visitor at the source.

GitHub Pages only serves docs/, so the output lives at docs/agent-corpus.json
and is committed alongside data.json by the weekly ingest.

Run:  python -m ingest.build_agent_corpus [--all-bodies | --body <id>]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from . import bodies

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = DOCS / "agent-corpus.json"

# Transcript passages are windowed so a retrieved chunk is big enough to hold a
# real exchange but small enough to be one citation. Words, not tokens — cheap
# to compute and good enough for keyword retrieval.
PASSAGE_WORDS = 230
PASSAGE_OVERLAP = 40

# A diarized transcript line looks like "Speaker A: ..."; keep the label, it
# tells the agent (and reader) that turns are changing.
_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS.sub(" ", str(text or "")).strip()


def _vote_summary(motion: dict, member_name) -> tuple[str, dict]:
    """A one-line tally plus a "who voted how" clause, for the searchable text."""
    tally = {"Yes": 0, "No": 0, "Other": 0}
    yes, no = [], []
    for v in motion.get("votes", []):
        name = member_name(v.get("member_id"))
        vote = (v.get("vote") or "").strip()
        if vote.lower() in ("yes", "aye", "y"):
            tally["Yes"] += 1
            yes.append(name)
        elif vote.lower() in ("no", "nay", "n"):
            tally["No"] += 1
            no.append(name)
        else:
            tally["Other"] += 1
    parts = []
    if tally["Yes"] or tally["No"]:
        parts.append(f"Vote: {tally['Yes']} yes, {tally['No']} no.")
    if yes:
        parts.append("In favor: " + ", ".join(yes) + ".")
    if no:
        parts.append("Opposed: " + ", ".join(no) + ".")
    return " ".join(parts), tally


def _motion_docs(data: dict, body: dict, member_name) -> list[dict]:
    docs = []
    tag_label = {t["id"]: t.get("label", t["id"]) for t in data.get("tags", [])}
    for m in data.get("motions", []):
        vote_line, _ = _vote_summary(m, member_name)
        tags = [tag_label.get(t, t) for t in m.get("tags", [])]
        # Order matters: lead with the human-readable enrichment, which is what
        # a visitor's question will most resemble; fall back to the raw record.
        text = " ".join(
            _clean(x)
            for x in (
                m.get("headline"),
                m.get("summary"),
                m.get("impact"),
                m.get("significance"),
                m.get("item_title"),
                m.get("motion"),
                f"Outcome: {m.get('outcome')}." if m.get("outcome") else "",
                vote_line,
                ("Topics: " + ", ".join(tags) + ".") if tags else "",
            )
            if x
        )
        docs.append(
            {
                "id": f"{body['id']}:motion:{m['id']}",
                "kind": "motion",
                "body": body["id"],
                "title": _clean(m.get("headline") or m.get("item_title") or "Motion"),
                "date": m.get("date", ""),
                "url": f"motions.html?id={m['id']}&body={body['id']}",
                "tags": m.get("tags", []),
                "text": text,
            }
        )
    return docs


def _meeting_docs(data: dict, body: dict, summaries: dict[int, str]) -> list[dict]:
    docs = []
    motions_by_meeting: dict[int, list[dict]] = {}
    for m in data.get("motions", []):
        motions_by_meeting.setdefault(m.get("meeting_id"), []).append(m)
    for mt in data.get("meetings", []):
        mid = mt["id"]
        heads = [
            _clean(x.get("headline") or x.get("item_title"))
            for x in motions_by_meeting.get(mid, [])
        ]
        heads = [h for h in heads if h]
        summary = summaries.get(mid, "")
        text_parts = [
            f"Meeting of {mt.get('date','')}.",
            f"{mt.get('motion_count', 0)} motions "
            f"({mt.get('ord_count', 0)} ordinances, {mt.get('res_count', 0)} resolutions).",
        ]
        if heads:
            text_parts.append("Decisions included: " + "; ".join(heads[:40]) + ".")
        if summary:
            text_parts.append(summary)
        docs.append(
            {
                "id": f"{body['id']}:meeting:{mid}",
                "kind": "meeting",
                "body": body["id"],
                "title": f"Council meeting — {mt.get('date','')}",
                "date": mt.get("date", ""),
                "url": f"meetings.html?id={mid}&body={body['id']}",
                "tags": [],
                "text": _clean(" ".join(text_parts)),
            }
        )
    return docs


def _member_docs(data: dict, body: dict) -> list[dict]:
    docs = []
    motions = data.get("motions", [])
    for mem in data.get("members", []):
        mid = mem["id"]
        yes = no = total = 0
        for m in motions:
            for v in m.get("votes", []):
                if v.get("member_id") != mid:
                    continue
                total += 1
                vote = (v.get("vote") or "").lower()
                if vote in ("yes", "aye", "y"):
                    yes += 1
                elif vote in ("no", "nay", "n"):
                    no += 1
        tenure = f"{mem.get('tenure_start','')} to {mem.get('tenure_end','') or 'present'}"
        text = (
            f"{mem.get('name','')} — {mem.get('role','Councilmember')}. "
            f"Term: {tenure}. "
            f"Cast {total} recorded votes on file: {yes} yes, {no} no."
        )
        docs.append(
            {
                "id": f"{body['id']}:member:{mid}",
                "kind": "member",
                "body": body["id"],
                "title": _clean(mem.get("name", "")),
                "date": mem.get("tenure_start", ""),
                "url": f"motions.html?member={mid}&body={body['id']}",
                "tags": [],
                "text": _clean(text),
            }
        )
    return docs


def _overview_doc(data: dict, body: dict) -> dict:
    counts = data.get("counts", {})
    tags = data.get("tags", [])
    top = sorted(tags, key=lambda t: t.get("motion_count", 0), reverse=True)[:8]
    tag_line = ", ".join(f"{t['label']} ({t.get('motion_count',0)})" for t in top)
    members = ", ".join(
        f"{m.get('name','')} ({m.get('role','Councilmember')})" for m in data.get("members", [])
    )
    text = (
        f"This site tracks the {body.get('label', body['id'])} of Eagle Mountain City, Utah. "
        f"It covers {counts.get('meetings', 0)} meetings, {counts.get('motions', 0)} motions, "
        f"and {counts.get('votes', 0)} recorded votes. "
        f"Common topics: {tag_line}. "
        f"People on record: {members}. "
        "Visitors can search motions by topic, member, outcome, or year, browse meetings, "
        "read the proposed property tax change and the adopted budget, and open full meeting "
        "transcripts."
    )
    return {
        "id": f"{body['id']}:overview",
        "kind": "overview",
        "body": body["id"],
        "title": f"About {body.get('label', body['id'])}",
        "date": data.get("generated_at", "")[:10],
        "url": f"index.html?body={body['id']}",
        "tags": [],
        "text": _clean(text),
    }


def _load_transcripts(body_id: str) -> tuple[dict[int, str], list[dict]]:
    """Return (meeting_id -> summary text, transcript passage docs)."""
    index_path = DOCS / "transcripts" / "index.json"
    if not index_path.exists():
        return {}, []
    index = json.loads(index_path.read_text(encoding="utf-8"))
    entries = index.get(body_id, [])
    summaries: dict[int, str] = {}
    passages: list[dict] = []
    for e in entries:
        mid = e.get("id")
        date = e.get("date", "")
        summ_file = e.get("summary_file")
        if summ_file and (DOCS / summ_file).exists():
            summaries[mid] = _clean((DOCS / summ_file).read_text(encoding="utf-8"))
        text_file = e.get("text_file")
        if text_file and (DOCS / text_file).exists():
            words = _clean((DOCS / text_file).read_text(encoding="utf-8")).split(" ")
            step = PASSAGE_WORDS - PASSAGE_OVERLAP
            for i in range(0, len(words), step):
                chunk = " ".join(words[i : i + PASSAGE_WORDS])
                if len(chunk) < 60:
                    continue
                passages.append(
                    {
                        "id": f"{body_id}:transcript:{mid}:{i}",
                        "kind": "transcript",
                        "body": body_id,
                        "title": f"Transcript — {e.get('title','Meeting')} ({date})",
                        "date": date,
                        "url": f"meetings.html?id={mid}&body={body_id}",
                        "tags": [],
                        "text": chunk,
                    }
                )
    return summaries, passages


def _extra_docs() -> list[dict]:
    """Tax and budget live in their own files and belong to the city council."""
    docs = []
    tax_path = DOCS / "data.tax.json"
    if tax_path.exists():
        tax = json.loads(tax_path.read_text(encoding="utf-8"))
        rates = tax.get("rates", {})
        rate_lines = []
        for key, r in rates.items():
            if isinstance(r, dict):
                rate_lines.append(
                    f"{r.get('label', key)}: rate {r.get('rate')}"
                    + (f", revenue ${r.get('revenue'):,}" if isinstance(r.get("revenue"), (int, float)) else "")
                )
        text = (
            f"Proposed property tax change for {tax.get('fiscal_year_label', tax.get('fiscal_year',''))}, "
            f"status: {tax.get('status','')}. Purpose: {tax.get('purpose','')} "
            f"Utah taxes a primary residence on {int(tax.get('residential_ratio',0)*100)}% of market value. "
            + " ".join(rate_lines)
        )
        docs.append(
            {
                "id": "city-council:tax",
                "kind": "tax",
                "body": "city-council",
                "title": f"Proposed property tax — {tax.get('fiscal_year_label','')}",
                "date": "",
                "url": "tax.html",
                "tags": ["budget"],
                "text": _clean(text),
            }
        )
    budget_path = DOCS / "data.budget.json"
    if budget_path.exists():
        b = json.loads(budget_path.read_text(encoding="utf-8"))
        total = b.get("total")
        gf = b.get("general_fund")
        text = (
            f"Adopted budget for fiscal year {b.get('fiscal_year','')}. "
            + (f"Total all funds: ${total:,}. " if isinstance(total, (int, float)) else "")
            + (f"General fund: ${gf:,}. " if isinstance(gf, (int, float)) else "")
            + f"Source: {b.get('source','')}."
        )
        docs.append(
            {
                "id": "city-council:budget",
                "kind": "budget",
                "body": "city-council",
                "title": f"Adopted budget — FY {b.get('fiscal_year','')}",
                "date": "",
                "url": "budget.html",
                "tags": ["budget"],
                "text": _clean(text),
            }
        )
    return docs


def _data_file_for(body: dict) -> Path:
    # bodies.py stores data_file as a repo-root-relative path ("docs/data.json").
    return ROOT / body.get("data_file", "docs/data.json")


def build(body_ids: list[str] | None = None) -> dict:
    all_bodies = bodies.all_bodies()
    if body_ids:
        all_bodies = [b for b in all_bodies if b["id"] in body_ids]

    docs: list[dict] = []
    body_meta = []
    for body in all_bodies:
        path = _data_file_for(body)
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        by_id = {m["id"]: m for m in data.get("members", [])}

        def member_name(mid, _by=by_id):
            m = _by.get(mid)
            return m["name"] if m else (mid or "")

        summaries, passages = _load_transcripts(body["id"])
        docs.append(_overview_doc(data, body))
        docs += _member_docs(data, body)
        docs += _meeting_docs(data, body, summaries)
        docs += _motion_docs(data, body, member_name)
        docs += passages
        body_meta.append(
            {"id": body["id"], "label": body.get("label", body["id"]), "default": body.get("default", False)}
        )

    docs += _extra_docs()

    corpus = {
        "version": 1,
        "bodies": body_meta,
        "count": len(docs),
        "docs": docs,
    }
    OUT.write_text(json.dumps(corpus, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    size_kb = OUT.stat().st_size / 1024
    kinds: dict[str, int] = {}
    for d in docs:
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(docs)} chunks, {size_kb:.0f} KB")
    print("  by kind: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    return corpus


def main() -> None:
    ap = argparse.ArgumentParser(description="Build docs/agent-corpus.json")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all-bodies", action="store_true", help="include every configured body (default)")
    g.add_argument("--body", help="build for a single body id")
    args = ap.parse_args()
    build([args.body] if args.body else None)


if __name__ == "__main__":
    main()
