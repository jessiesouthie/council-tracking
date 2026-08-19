"""
Build the knowledge corpus the site's Q&A agent retrieves from.

The agent (a Cloudflare Worker that proxies Claude — see worker/) can't run the
site's Python or read data/. It answers questions by searching one static file,
docs/agent-corpus.json, which this script assembles from everything the site
already publishes:

  * every motion, with its plain-English enrichment and full roll-call vote
  * every meeting (as a compact record, plus the prose summary when transcribed),
    including ones held but not yet minuted, which exist only as a transcript,
    and ones never minuted at all, which exist only as a posted agenda
  * the next meeting on the calendar and the business its agenda lists
  * transcript passages, windowed so a quote can be retrieved and cited
  * each council member, their role and tenure, and how often they vote yes
  * the tax change — as noticed and as adopted — and the adopted budget
  * how the city's rate compares with other cities, and the road fees on utility
    bills that a rate comparison can't see
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


def _meeting_docs(
    data: dict,
    body: dict,
    summaries: dict[int, str],
    tx_meta: dict[int, dict] | None = None,
) -> list[dict]:
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

    # Meetings that have been held and transcribed but whose minutes the city
    # hasn't published yet exist only in the transcript index — data.json has no
    # row for them, so without this the agent's most recent meeting was weeks
    # stale and the newest ones existed only as loose transcript passages with
    # nothing to date or frame them. meetings.html already surfaces these the
    # same way (see its `transcriptOnly` list); this is that rule, corpus-side.
    known = {mt["id"] for mt in data.get("meetings", [])}
    for mid, meta in sorted((tx_meta or {}).items(), key=lambda kv: kv[1].get("date", "")):
        if mid in known:
            continue
        date = meta.get("date", "")
        text_parts = [
            f"Meeting of {date}.",
            "The minutes for this meeting have not been published yet, so the site "
            "has no motion list or roll-call record for it — the account below comes "
            "from the meeting recording and its transcript.",
        ]
        summary = summaries.get(mid, "")
        if summary:
            text_parts.append(summary)
        docs.append(
            {
                "id": f"{body['id']}:meeting:{mid}",
                "kind": "meeting",
                "body": body["id"],
                "title": f"{meta.get('title') or 'Meeting'} — {date} (minutes not yet published)",
                "date": date,
                "url": f"meetings.html?id={mid}&body={body['id']}",
                "tags": [],
                "text": _clean(" ".join(text_parts)),
            }
        )

    # And the meetings that were held but never minuted or recorded at all: the
    # special sessions and work sessions whose agenda is the entire surviving
    # record (ingest.build_agenda_only). Without these the agent would answer
    # "there was no meeting that night", which is worse than saying what little
    # is known — so the entry leads with what it is and isn't.
    for entry in sorted(_agenda_only(body["id"]), key=lambda e: e.get("date", "")):
        mid = entry.get("id")
        if mid is None or mid in known or mid in (tx_meta or {}):
            continue
        date = entry.get("date", "")
        items = [_clean(it.get("plain") or it.get("title"))
                 for head in entry.get("agenda") or []
                 for it in head.get("items") or []]
        headings = [_clean(h.get("title")) for h in entry.get("agenda") or []
                    if h.get("title") and not h.get("procedural")]
        text_parts = [
            f"Meeting of {date}: {entry.get('title') or 'Meeting'}"
            + (f", {entry['start_label']}" if entry.get("start_label") else "")
            + (f", {entry['location']}" if entry.get("location") else "") + ".",
            "The city published no minutes for this meeting, so there is no motion "
            "list and no roll-call record of it anywhere on this site. What follows "
            "is the agenda posted beforehand — what the council intended to take up, "
            "not what it decided.",
        ]
        if headings:
            text_parts.append("On the agenda: " + "; ".join(headings[:40]) + ".")
        if items:
            text_parts.append("Items listed: " + "; ".join(i for i in items[:40] if i) + ".")
        if entry.get("recording_url"):
            text_parts.append("The city posted a recording of this meeting on its "
                              "portal, but it has not been transcribed, so its "
                              "contents cannot be quoted.")
        docs.append(
            {
                "id": f"{body['id']}:meeting:{mid}",
                "kind": "meeting",
                "body": body["id"],
                "title": f"{entry.get('title') or 'Meeting'} — {date} (agenda only, no minutes)",
                "date": date,
                "url": f"meetings.html?id={mid}&body={body['id']}",
                "tags": [],
                "text": _clean(" ".join(text_parts)),
            }
        )
    return docs


def _agenda_only(body_id: str) -> list[dict]:
    """Agenda-only meetings for a body, or [] before that build has ever run."""
    path = DOCS / "data.agenda-only.json"
    if not path.exists():
        return []
    try:
        feed = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    entries = (feed.get("bodies") or {}).get(body_id)
    return entries if isinstance(entries, list) else []


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
        "The site is organised in five sections: Meetings (every meeting, its agenda, "
        "minutes and transcript), Votes (the searchable roll-call record, at motions.html), "
        "Members (each member's voting record), Finances (at finances.html, covering the "
        "property tax rate, the budget, the General Fund projections and city staffing), and "
        "About (how the site is built, plus a glossary at definitions.html). "
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


def _load_transcripts(body_id: str) -> tuple[dict[int, str], list[dict], dict[int, dict]]:
    """Return (meeting_id -> summary text, transcript passage docs, meeting_id -> meta).

    The third value carries the date and title of every transcribed meeting, so
    _meeting_docs can give a meeting whose minutes aren't published yet a dated,
    citable record of its own instead of leaving it as loose passages.
    """
    index_path = DOCS / "transcripts" / "index.json"
    if not index_path.exists():
        return {}, [], {}
    index = json.loads(index_path.read_text(encoding="utf-8"))
    entries = index.get(body_id, [])
    summaries: dict[int, str] = {}
    passages: list[dict] = []
    meta: dict[int, dict] = {}
    for e in entries:
        mid = e.get("id")
        date = e.get("date", "")
        meta[mid] = {"date": date, "title": _clean(e.get("title", "")) or "Meeting"}
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
    return summaries, passages, meta


def _upcoming_doc(body: dict) -> dict | None:
    """The one meeting a resident can still turn up to.

    Everything else in the corpus is the record of what already happened, so
    "when is the next council meeting" — one of the few questions a visitor can
    act on — had nothing to retrieve and got answered from whatever old motion
    happened to contain the word "next". The meetings page has carried this card
    since it was built; the agent just couldn't see the file behind it.
    """
    path = DOCS / "data.upcoming.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    events = (payload.get("bodies") or {}).get(body["id"]) or []
    events = [e for e in events if e.get("date")]
    if not events:
        return None
    ev = sorted(events, key=lambda e: (e.get("date", ""), e.get("start", "")))[0]

    label = body.get("label", body["id"])
    date = ev.get("date", "")
    parts = [
        f"The next {label} meeting — the upcoming one, not yet held — is scheduled for "
        f"{date}"
        + (f" at {ev['start_label']}" if ev.get("start_label") else "")
        + (f", at {_clean(ev['location'])}" if ev.get("location") else "")
        + "."
    ]
    sessions = ev.get("sessions") or []
    if sessions:
        parts.append(
            "Sessions: "
            + "; ".join(
                f"{_clean(s.get('label') or 'Session')} at {s.get('start_label', '')}"
                + (" (public comment)" if s.get("public_comment") else "")
                for s in sessions
            )
            + "."
        )
    if not ev.get("agenda_posted"):
        parts.append("The agenda for it has not been posted yet.")
    else:
        # Procedural scaffolding — the pledge, the vote to close the session —
        # is on every agenda and is not why anyone attends. The meetings page
        # drops it from this card for the same reason.
        items = [
            _clean(i.get("title"))
            for h in (ev.get("agenda") or [])
            if not h.get("procedural")
            for i in (h.get("items") or [])
        ]
        items = [i for i in items if i]
        hearings = sum(
            1
            for h in (ev.get("agenda") or [])
            if not h.get("procedural")
            for i in (h.get("items") or [])
            if re.search(r"public hearing", str(i.get("kind") or ""), re.I)
        )
        posted = ev.get("agenda_posted_on")
        parts.append(
            f"Its agenda was posted{f' on {posted}' if posted else ''} and lists "
            f"{len(items)} items of business"
            + (f", including {hearings} public hearing{'s' if hearings != 1 else ''}" if hearings else "")
            + "."
        )
        if items:
            parts.append("On the agenda: " + "; ".join(items[:25]) + ".")
    parts.append(
        "Nothing here has been voted on — it is what the body is scheduled to take up."
    )
    return {
        "id": f"{body['id']}:upcoming:{ev.get('id', date)}",
        "kind": "upcoming",
        "body": body["id"],
        "title": f"Next {label} meeting — {date}",
        "date": date,
        "url": f"meetings.html?body={body['id']}",
        "tags": [],
        "text": _clean(" ".join(parts)),
    }


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
        # Once a rate is adopted the chunk has to lead with it. Every rate in the
        # file is still listed, including the one that was noticed and dropped,
        # so a question about the 0.001700 people saw on the notice still lands.
        adopted = rates.get("adopted") or {}
        headline = (
            f"Adopted property tax rate for {tax.get('fiscal_year_label', tax.get('fiscal_year',''))}: "
            f"{adopted.get('rate_display','')}, adopted {adopted.get('date','')} — "
            f"{adopted.get('pct','')}% over the certified rate, raising "
            f"${adopted.get('revenue_increase',0):,}. {adopted.get('vote','')}. "
            f"It is not the {rates.get('proposed',{}).get('rate_display','')} the city noticed. "
            if adopted
            else f"Proposed property tax change for {tax.get('fiscal_year_label', tax.get('fiscal_year',''))}, "
                 f"status: {tax.get('status','')}. "
        )
        text = (
            headline
            + f"Purpose: {tax.get('purpose','')} "
            f"Utah taxes a primary residence on {int(tax.get('residential_ratio',0)*100)}% of market value. "
            + " ".join(rate_lines)
        )
        docs.append(
            {
                "id": "city-council:tax",
                "kind": "tax",
                "body": "city-council",
                "title": f"{'Adopted' if adopted else 'Proposed'} property tax — {tax.get('fiscal_year_label','')}",
                "date": "",
                "url": "tax.html",
                "tags": ["budget"],
                "text": _clean(text),
            }
        )
        # "Where can I read the actual notice?" is a question the agent should
        # answer with a URL, not a paraphrase. The published PDFs are their own
        # chunk so the links survive retrieval intact.
        pubs = (tax.get("documents") or {}).get("items") or []
        if pubs:
            listed = " ".join(
                f"{d.get('title','')} ({d.get('publisher','')}"
                + (f", {d.get('dated')}" if d.get("dated") else "")
                + f"): {d.get('url','')} — {d.get('note','')}"
                for d in pubs
            )
            docs.append(
                {
                    "id": "city-council:tax-documents",
                    "kind": "tax",
                    "body": "city-council",
                    "title": "The published truth-in-taxation documents (PDFs)",
                    "date": "",
                    "url": "tax.html#p-source",
                    "tags": ["budget"],
                    "text": _clean(
                        "The city's own published documents behind the property tax increase, each a PDF. "
                        + listed
                    ),
                }
            )
    # The city's levy is one line of seven. Asked "what are my property taxes?",
    # the agent should be able to name all of them rather than answer with the
    # city's rate alone, so the whole-bill breakdown is its own citable chunk.
    bill_path = DOCS / "data.tax-bill.json"
    if bill_path.exists():
        bill = json.loads(bill_path.read_text(encoding="utf-8"))
        ents = sorted(bill.get("entities", []), key=lambda e: -e.get("rate", 0.0))
        total = sum(e.get("rate", 0.0) for e in ents)
        lines = [
            f"{e.get('name','')}: rate {e.get('rate', 0):.6f}"
            + (f" ({e.get('rate', 0) / total * 100:.1f}% of the bill)" if total else "")
            + f" — {e.get('what','')}"
            for e in ents
        ]
        area = bill.get("tax_area", {})
        text = (
            f"Every property tax levied on an Eagle Mountain parcel, not just the city's. "
            f"These are the {bill.get('rate_year','')} final adopted rates for Utah County tax area "
            f"{area.get('code','')}, which covers most of the city, and they total {total:.6f}. "
            f"{len(ents)} taxing entities appear on the bill and the City Council sets exactly one of them. "
            + " ".join(lines)
            + " "
            + " ".join(bill.get("caveats", []))
        )
        docs.append(
            {
                "id": "city-council:tax-bill",
                "kind": "tax",
                "body": "city-council",
                "title": f"Every property tax on an Eagle Mountain bill — {bill.get('rate_year','')} rates",
                "date": "",
                "url": "tax.html#p-bill",
                "tags": ["budget"],
                "text": _clean(text),
            }
        )
    budget_path = DOCS / "data.budget.json"
    if budget_path.exists():
        b = json.loads(budget_path.read_text(encoding="utf-8"))
        total = b.get("total")
        # Both of these read out of the shipped file's own shape: general_fund is
        # a block with a total inside it, and source is a title/publisher/url
        # object — printed raw, either one puts a Python dict in the agent's mouth.
        gf = (b.get("general_fund") or {}).get("total")
        src = b.get("source") or {}
        text = (
            f"Adopted budget for fiscal year {b.get('fiscal_year','')}. "
            + (f"Total all funds: ${total:,}. " if isinstance(total, (int, float)) else "")
            + (f"General fund: ${gf:,}. " if isinstance(gf, (int, float)) else "")
            + f"Source: {src.get('title','')} ({src.get('publisher','')})."
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
    # The published book is a year behind what the Council is actually deciding.
    # Asked "what's in the budget?", the agent should reach the interim FY2026-27
    # figures first, so they get their own chunk rather than being folded into the
    # adopted-book one — and it says plainly that nothing here is final yet.
    next_path = DOCS / "data.budget-next.json"
    if next_path.exists():
        nb = json.loads(next_path.read_text(encoding="utf-8"))
        t = nb.get("totals", {}).get("fy2027", {})
        prior = nb.get("totals", {}).get("fy2026", {})
        gfn = nb.get("general_fund", {})
        ps = nb.get("public_safety", {})
        top_funds = ", ".join(
            f"{f['name']} ${f['total']:,}" for f in nb.get("funds", [])[:5]
        )
        top_capital = ", ".join(
            f"{c['name']} ${c['total']:,}" for c in nb.get("capital", [])[:6]
        )
        dates = "; ".join(f"{c['date']} {c['label']}" for c in nb.get("calendar", []))
        text = (
            f"{nb.get('fiscal_year_label','')} budget — {nb.get('status','')}, not final. "
            f"{nb.get('status_note','')} "
            f"All funds ${t.get('gross',0):,}, against the ${prior.get('gross',0):,} FY2025-26 ended on "
            "— that year was adopted at $119,105,860 and grew as capital carried into it, so this "
            "comparison is against the larger, later figure. "
            f"${t.get('reserves',0):,} of that is a reserve appropriation rather than money planned to be "
            f"spent, so the like-for-like comparison is ${t.get('net',0):,} against ${prior.get('net',0):,}. "
            f"Budgeted borrowing totals ${nb.get('borrowing',{}).get('total',0):,}, almost all of it a "
            f"$200,000,000 sewer bond for the new wastewater treatment plant. "
            f"General Fund ${gfn.get('total',0):,}, up from ${gfn.get('prior',0):,} — but it covers less: "
            f"planning, building inspection and permitting moved out to a new "
            f"${gfn.get('moved_out',{}).get('fund_total',0):,} Community Development fund paid for by permit "
            f"and plan-check fees, taking about "
            f"${gfn.get('moved_out',{}).get('revenue_moved',0):,} of fee revenue with them. "
            f"The proposed property-tax increase appears as a {nb.get('tax_line',{}).get('revenue_account','')} "
            f"revenue line of ${nb.get('tax_line',{}).get('interim_revenue',0):,} and an identical restricted "
            f"expense line inside Public Safety; it cannot be spent unless the increase passes. "
            f"The Sheriff contract line falls from ${ps.get('contract_prior',0):,} to ${ps.get('contract',0):,} "
            f"with ${ps.get('restricted',0):,} held restricted — together ${ps.get('combined',0):,}, the proposed "
            f"property-tax revenue exactly. Largest funds: {top_funds}. "
            f"Largest capital lines: {top_capital}. Key dates: {dates}."
        )
        docs.append(
            {
                "id": "city-council:budget-next",
                "kind": "budget",
                "body": "city-council",
                "title": f"Next year's budget — {nb.get('fiscal_year_label','')} (interim)",
                "date": "",
                "url": "budget.html#next-year",
                "tags": ["budget"],
                "text": _clean(text),
            }
        )
    # The FY2025-26 book: the one budget itemised to the account, so the agent
    # can answer "how much went to the library" rather than only "how much went
    # to community services". Its own chunk, because the year has two totals and
    # an answer that quotes the wrong one is wrong by $44M.
    book_path = DOCS / "data.budget-book.json"
    if book_path.exists():
        bb = json.loads(book_path.read_text(encoding="utf-8"))
        bt = bb.get("totals", {})
        top_funds = ", ".join(
            f"{f['name']} ${f['adopted']:,}" for f in bb.get("funds", [])[:6])
        top_depts = ", ".join(
            f"{d['name']} ${d['adopted']:,}" for d in bb.get("departments", [])[:8])
        revenue = ", ".join(
            f"{r['name']} ${r['adopted']:,}" for r in bb.get("citywide", {}).get("revenue", []))
        spending = ", ".join(
            f"{o['name']} ${o['adopted']:,}" for o in bb.get("citywide", {}).get("expense", []))
        text = (
            f"{bb.get('fiscal_year_label','')} adopted budget — the year {bb.get('period','')}, "
            "published in full as a budget book and itemised on this site down to every account. "
            f"Adopted at ${bt.get('adopted',0):,} across {bt.get('fund_count',0)} funds and "
            f"{bt.get('department_count',0)} departments, {bt.get('line_count',0):,} lines in all. "
            f"The same year stood at ${bt.get('amended',0):,} by the end of it: "
            f"${bt.get('carried_forward',0):,} of building work that was not finished the year before "
            "rolled forward into it. Quote the adopted figure for what the Council voted on and the "
            "amended one for what the year actually ran, and say which is which. "
            f"For comparison, FY2025 was adopted at ${bt.get('fy25_budget',0):,}. "
            f"Revenue by source: {revenue}. Spending by type: {spending}. "
            f"Largest funds: {top_funds}. Largest departments: {top_depts}. "
            "Any figure can be looked up by fund, by department or by account number in the "
            "line-by-line explorer on the Budget page."
        )
        docs.append(
            {
                "id": "city-council:budget-book",
                "kind": "budget",
                "body": "city-council",
                "title": f"Last year's budget, line by line — {bb.get('fiscal_year_label','')} (adopted)",
                "date": "",
                "url": "budget.html#book-year",
                "tags": ["budget"],
                "text": _clean(text),
            }
        )
    docs += _county_docs()
    docs += _state_docs()
    # Staffing is the one question the budget files cannot answer: ClearGov
    # carries personnel as dollars and has no headcount anywhere. Two chunks,
    # because "how many people work for the city" and "are we understaffed
    # compared to other cities" are different questions and the second one has
    # a contested answer that should retrieve whole rather than in pieces.
    staff_path = DOCS / "data.staffing.json"
    if staff_path.exists():
        st = json.loads(staff_path.read_text(encoding="utf-8"))
        em = st.get("eagle_mountain", {})
        fns = {f["key"]: f["label"] for f in st.get("functions", [])}
        split = ", ".join(
            f"{fns.get(k, k)} {v}" for k, v in (em.get("by_function") or {}).items()
        )
        series = em.get("series") or []
        first = series[0] if series else {}
        docs.append(
            {
                "id": "city-council:staffing",
                "kind": "staffing",
                "body": "city-council",
                "title": f"How many people work for Eagle Mountain — FY{st.get('fiscal_year','')}",
                "date": "",
                "url": "staffing.html",
                "tags": ["budget"],
                "text": _clean(
                    f"Eagle Mountain City employed {em.get('total')} full-time equivalents in "
                    f"FY{st.get('fiscal_year','')}, for a Census-estimated {em.get('population',0):,} "
                    f"residents — {em.get('per_1k',{}).get('raw')} per 1,000. The city employs nobody "
                    "in police or fire: patrol is contracted from the Utah County Sheriff's Office "
                    "(Eagle Mountain Division) and fire and ambulance come from Unified Fire "
                    f"Authority, so the {em.get('public_safety')} FTE it reports under public safety "
                    "are crossing guards. By function: " + split + ". "
                    f"In {first.get('year','')} the city had {first.get('fte')} FTE for "
                    f"{first.get('population',0):,} residents, so headcount has grown more slowly "
                    "than population. Separately, the city's HR director told the Council in May 2026 "
                    "that the city then had 236 employees against fewer than 100 full-time in 2019 — "
                    "that is a headcount including part-time staff two years later, not the same "
                    "measure as full-time equivalents."
                ),
            }
        )
        claim = st.get("claim", {})
        peers = ", ".join(
            f"{c['city']} {c['per_1k']['raw']} raw and {c['per_1k']['excl_public_safety']} excluding "
            f"police and fire" for c in st.get("peers", [])
        )
        docs.append(
            {
                "id": "city-council:staffing-comparison",
                "kind": "staffing",
                "body": "city-council",
                "title": "Is Eagle Mountain a third the size of comparable cities?",
                "date": "",
                "url": "staffing.html",
                "tags": ["budget"],
                "text": _clean(
                    "The city manager told the Council in August 2026 that Eagle Mountain's staff is "
                    "about a third the size of cities of equivalent population. Checked against the "
                    "audited FTE schedules of the Utah cities nearest Eagle Mountain in population, "
                    "the answer depends on whether contracted services are counted. On raw headcount "
                    f"Eagle Mountain is {claim.get('raw',{}).get('pct_of_median')}% of the peer median "
                    f"({claim.get('raw',{}).get('eagle_mountain')} per 1,000 against "
                    f"{claim.get('raw',{}).get('peer_median')}) — the leanest city in the set, and the "
                    "claim is about right. Excluding police and fire on every city's side, which is "
                    "the like-for-like comparison because Eagle Mountain contracts both, it is "
                    f"{claim.get('excl_public_safety',{}).get('pct_of_median')}% of the median "
                    f"({claim.get('excl_public_safety',{}).get('eagle_mountain')} against "
                    f"{claim.get('excl_public_safety',{}).get('peer_median')}) — nearer two thirds than "
                    "one third. Excluding utilities as well it is "
                    f"{claim.get('core',{}).get('pct_of_median')}%. So the claim is true of the "
                    "headcount and overstated as a measure of how thinly the city is staffed: most of "
                    "the gap is that other cities employ their own police and firefighters. Peers, per "
                    "1,000 residents: " + peers + "."
                ),
            }
        )
    return docs


def _ordinal(n: int) -> str:
    if n % 100 in (11, 12, 13):
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _state_docs() -> list[dict]:
    """Every truth-in-taxation notice in Utah, and what the council actually did.

    Two chunks again, for the same reason the county comparison is two. "Is this
    the biggest tax increase in Utah?" wants the ranking; "how much did my taxes
    go up?" wants the adopted rate and nothing else, and burying that answer
    behind seventy-nine other entities would be a way of not giving it. Ranks are
    computed from the figures rather than stored, so the agent and the charts
    can't drift apart.
    """
    path = DOCS / "data.tax-state.json"
    if not path.exists():
        return []
    s = json.loads(path.read_text(encoding="utf-8"))
    notices = s.get("notices", [])
    if not notices:
        return []

    em_name = s.get("eagle_mountain", {}).get("entity", "")
    ad = s.get("eagle_mountain", {}).get("adopted", {})
    t = s.get("totals", {})
    em = next((n for n in notices if n.get("entity") == em_name), None)
    if em is None:
        return []

    # Eagle Mountain is ranked on what its council adopted, the way the page
    # ranks it; its own noticed row is dropped from the comparison so it is
    # never measured against itself.
    def rank_of(key: str, value: float) -> int:
        return sum(1 for n in notices if n.get("entity") != em_name and n.get(key, 0) > value) + 1

    # Eagle Mountain appears in this list at what it adopted, not what it
    # noticed — the same substitution the page's ranking makes, so the agent and
    # the chart never quote different numbers for the same row.
    def val(n: dict, key: str):
        return ad.get(key, 0) if n.get("entity") == em_name else n.get(key, 0)

    def rate(n: dict):
        return ad.get("rate") if n.get("entity") == em_name else n.get("proposed_rate")

    def levy(n: dict) -> str:
        return "no rate published" if rate(n) is None else \
            f"levy {n.get('certified_rate',0):.6f} to {rate(n):.6f}"

    top = sorted(notices, key=lambda n: -val(n, "pct"))[:15]
    top_lines = "; ".join(
        f"{n.get('entity','')} ({n.get('county','')} County, {n.get('type','').lower()}) "
        f"+{val(n,'pct')}%, {levy(n)}, ${val(n,'annual_increase'):,.2f} a year on a "
        f"${n.get('home_value',0):,} home, ${val(n,'revenue'):,} raised, "
        + ("adopted" if n.get("entity") == em_name else "hearing")
        + f" {n.get('hearing',{}).get('date','')}"
        for n in top
    )

    text = (
        f"Every {s.get('year','')} truth-in-taxation notice in Utah, not just Eagle Mountain's and not "
        f"just cities. {t.get('notices',0)} notices from {t.get('entities',0)} taxing entities in "
        f"{t.get('counties',0)} counties — cities, towns, school districts, water conservancy districts "
        f"and fire districts — asking for ${t.get('revenue',0):,} between them, with hearings from "
        f"{t.get('hearings_from','')} to {t.get('hearings_to','')}. "
        f"Eagle Mountain's is the only settled figure on the list: its council adopted "
        f"{ad.get('rate_display','')} on {ad.get('date','')}, an increase of {ad.get('pct',0)}%. "
        f"Measured against every other entity's noticed figure that is the "
        f"{_ordinal(rank_of('pct', ad.get('pct', 0)))}-steepest increase in the state, the "
        f"{_ordinal(rank_of('annual_increase', ad.get('annual_increase', 0)))}-heaviest on an average home "
        f"at ${ad.get('annual_increase',0):,.2f} a year, and the "
        f"{_ordinal(rank_of('revenue', ad.get('revenue', 0)))}-largest by revenue at "
        f"${ad.get('revenue',0):,}. Every other entity's hearing may still land lower than its notice. "
        f"The fifteen steepest proposals in Utah: {top_lines}. "
        + " ".join(s.get("caveats", []))
    )

    adopted_text = (
        f"What Eagle Mountain's City Council actually adopted. On {ad.get('date','')}, at the "
        f"truth-in-taxation hearing, the council voted to set the city property tax rate at "
        f"{ad.get('rate_display','')} — not the {em.get('proposed_rate',0):.6f} on the public notice. "
        f"{ad.get('vote','')}. That is an increase of {ad.get('pct',0)}% over the "
        f"{em.get('certified_rate',0):.6f} certified rate, rather than the {em.get('pct',0)}% noticed. "
        f"It adds about ${ad.get('annual_increase',0):,.2f} a year to a "
        f"${ad.get('home_value',0):,} primary residence, instead of ${em.get('annual_increase',0):,.2f}, "
        f"and raises roughly ${ad.get('revenue',0):,} rather than ${em.get('revenue',0):,}. "
        f"The money goes to the Utah County Sheriff's contract and the deputies it funds. "
        f"The increase does not take effect until the full budget passes at the budget adoption "
        f"hearing on August 18, 2026. The motion as read: \"{ad.get('motion','')}\" "
        f"Source: {ad.get('source',{}).get('title','')}, transcribed from the recording; "
        f"the minutes were not published when this was written. {ad.get('derivation','')}"
    )

    return [
        {
            "id": "city-council:tax-state",
            "kind": "tax",
            "body": "city-council",
            "title": f"Every tax increase proposed in Utah — {s.get('year','')} truth-in-taxation notices",
            "date": "",
            "url": "tax.html#p-state-proposals",
            "tags": ["budget"],
            "text": _clean(text),
        },
        {
            "id": "city-council:tax-adopted",
            "kind": "tax",
            "body": "city-council",
            "title": f"The rate the council adopted — {ad.get('rate_display','')}, {ad.get('date','')}",
            "date": ad.get("date", ""),
            "url": "tax.html#p-state-proposals",
            "tags": ["budget"],
            "text": _clean(adopted_text),
        },
    ]


def _county_docs() -> list[dict]:
    """The comparison against other cities, and the fees that comparison can't see.

    Two chunks, not one. "Is Eagle Mountain's tax high?" and "does my city charge
    a road fee?" are different questions with different answers, and folding the
    fees into the long rate chunk would bury them behind twenty-six city rates.
    Both deep-link to the same section, so a visitor lands on the charts either
    way. Ranks are computed here from the rates themselves rather than read from
    the stored rank fields, so a chunk can never disagree with the bars the
    reader is looking at.
    """
    path = DOCS / "data.tax-county.json"
    if not path.exists():
        return []
    c = json.loads(path.read_text(encoding="utf-8"))
    cities = c.get("cities", [])
    if not cities:
        return []

    em = c.get("eagle_mountain", {})
    em_rate = em.get("current_rate", 0.0)
    # The rate Eagle Mountain will actually levy: adopted where the council has
    # voted, the noticed figure where it has not.
    em_adopted = em.get("adopted_rate")
    em_prop = em_adopted or em.get("proposed_rate", 0.0)
    em_word = "adopted" if em_adopted else "proposed"
    rates = sorted(x.get("current_rate", 0.0) for x in cities)
    mid = len(rates) // 2
    median = rates[mid] if len(rates) % 2 else (rates[mid - 1] + rates[mid]) / 2
    low_to_high = sorted(cities, key=lambda x: x.get("current_rate", 0.0))
    em_pos = next(
        (i + 1 for i, x in enumerate(low_to_high) if x.get("name") == "Eagle Mountain"), 0
    )

    # Where the proposal would land: every city carried at the rate its own
    # published figures imply, which is what the second chart ranks by.
    resulting = {p.get("city"): p.get("resulting_rate") for p in c.get("proposals", [])}
    high_to_low = sorted(
        cities,
        key=lambda x: resulting.get(x.get("name")) or x.get("current_rate", 0.0),
        reverse=True,
    )
    em_prop_pos = next(
        (i + 1 for i, x in enumerate(high_to_low) if x.get("name") == "Eagle Mountain"), 0
    )

    rate_lines = ", ".join(
        f"{x.get('name','')} {x.get('current_rate', 0):.6f}" for x in reversed(low_to_high)
    )
    prop_lines = " ".join(
        f"{p.get('city','')}: +{p.get('pct')}% for {p.get('purpose','')}"
        + (f" ({p['levy']})" if p.get("levy") else "")
        + f", about ${p.get('annual_increase', 0):,.0f} a year on a ${p.get('home_value', 0):,} home."
        for p in c.get("proposals", [])
    )
    sw = c.get("statewide", {})
    sw_em = sw.get("eagle_mountain", {})

    # Salt Lake County is the page's second comparison. Its ranks are computed
    # the way its charts rank: current against current, proposed against every
    # city's resulting rate. The stored rank fields measure the proposed rate
    # against current rates instead, which would put a different number in the
    # agent's mouth than the bars show.
    sl = c.get("salt_lake", {})
    sl_cities = sl.get("cities", [])
    sl_res = {p.get("city"): p.get("resulting_rate") for p in sl.get("proposals", [])}
    sl_now_below = sum(1 for x in sl_cities if x.get("current_rate", 0.0) < em_rate)
    sl_prop_above = sum(
        1
        for x in sl_cities
        if (sl_res.get(x.get("name")) or x.get("current_rate", 0.0)) > em_prop
    )

    text = (
        f"How Eagle Mountain's city property tax rate compares with other cities. "
        f"These are {c.get('rate_year','')} final adopted municipal rates — the city's own levy only, "
        f"not the county, school district or special districts. "
        f"Eagle Mountain's {em_rate:.6f} is the {_ordinal(em_pos)}-lowest of the {len(cities)} cities in "
        f"Utah County, against a county median of {median:.6f}. "
        f"The {em_word} {em_prop:.6f} "
        + ("makes" if em_adopted else "would make")
        + f" it the {_ordinal(em_prop_pos)}-highest, once every other "
        f"city's own proposed increase is applied. "
        f"Rates today, highest to lowest: {rate_lines}. "
        f"{len(c.get('proposals', []))} Utah County cities noticed a {c.get('proposal_year','')} increase. "
        f"{prop_lines} "
        + (
            f"Statewide, of all {sw.get('total_cities','')} Utah cities, Eagle Mountain's current rate is the "
            f"{_ordinal(sw_em.get('rank_current_low', 0))}-lowest; the {em_word} rate is the "
            f"{_ordinal(sw_em.get('rank_adopted_high') or sw_em.get('rank_proposed_high', 0))}-highest, "
            f"measuring it against every other city's current rate — the state does not publish a full set "
            f"of 2026 proposals. "
            if sw_em
            else ""
        )
        + (
            f"The same page carries a second comparison against the {len(sl_cities)} cities of Salt Lake "
            f"County, where Eagle Mountain is shown as an out-of-county reference: its current rate would be "
            f"the {_ordinal(sl_now_below + 1)}-lowest there, and the {em_word} rate the "
            f"{_ordinal(sl_prop_above + 1)}-highest once each of those cities' own increases is applied. "
            if sl_cities
            else ""
        )
        + " ".join(c.get("caveats", []))
    )

    docs = [
        {
            "id": "city-council:tax-compare",
            "kind": "tax",
            "body": "city-council",
            "title": f"How the rate compares — Utah County cities, {c.get('rate_year','')} rates",
            "date": "",
            "url": "tax.html#s-compare",
            "tags": ["budget"],
            "text": _clean(text),
        }
    ]

    # Several cities pay for streets with a flat monthly utility fee instead of a
    # rate. Asked "who charges more?", the agent should be able to say that a
    # rate comparison misses it, and by how much.
    t = c.get("transportation_fees", {})
    fee_cities = t.get("cities", [])
    if fee_cities:
        tax_path = DOCS / "data.tax.json"
        ratio = 0.55
        if tax_path.exists():
            ratio = json.loads(tax_path.read_text(encoding="utf-8")).get("residential_ratio", ratio)
        taxable = 500000 * ratio
        fee_lines = " ".join(
            f"{f.get('name','')}: ${f.get('monthly', 0):.2f} a month, ${f.get('monthly', 0) * 12:,.2f} a year "
            f"— the same as a property tax rate of {f.get('monthly', 0) * 12 / taxable:.6f} on a $500,000 home. "
            f"{f.get('note','')}"
            for f in sorted(fee_cities, key=lambda f: -f.get("monthly", 0))
        )
        fee_text = (
            f"Transportation utility fees — road fees charged on the monthly city utility bill, not through "
            f"the property tax. {len(fee_cities)} Utah County cities charge one, so a comparison of property "
            f"tax rates understates what a household there pays for streets. "
            f"A fee is set by resolution and is not subject to Truth-in-Taxation notices or hearings, and it "
            f"is flat: the same on every home, whatever it is worth. Residential rates as of "
            f"{t.get('as_of','')}: {fee_lines} "
            f"Eagle Mountain charges no such fee. {t.get('eagle_mountain', {}).get('note','')} "
            f"For scale, Eagle Mountain's entire city property tax on a $500,000 primary residence is "
            f"${em_rate * taxable:,.2f} a year today. "
            + " ".join(t.get("caveats", []))
        )
        docs.append(
            {
                "id": "city-council:tax-fees",
                "kind": "tax",
                "body": "city-council",
                "title": "Road fees on the utility bill — Utah County cities that charge one",
                "date": "",
                "url": "tax.html#s-compare",
                "tags": ["budget"],
                "text": _clean(fee_text),
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

        summaries, passages, tx_meta = _load_transcripts(body["id"])
        docs.append(_overview_doc(data, body))
        upcoming = _upcoming_doc(body)
        if upcoming:
            docs.append(upcoming)
        docs += _member_docs(data, body)
        docs += _meeting_docs(data, body, summaries, tx_meta)
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
