"""
Find the record behind a claim, so a check starts from citations rather than memory.

The intake problem this solves is not "where do rumours come from" — that part
is manual and stays manual, see data/claims/README.md. It is the part after: a
claim arrives as one sentence of somebody's paraphrase, and answering it means
knowing which of ~2,000 motions, meetings, transcript passages and figures bear
on it. docs/agent-corpus.json already indexes all of that, in citable chunks
that each carry a deep link back into the site, because the Q&A agent needed
exactly this. This searches it from the command line.

What comes back is candidate sources, ranked — not a verdict. Deciding what a
claim asserts, and whether the record bears it out, is the editorial act this
script deliberately does not attempt; the ranked list is what you read before
writing the entry in docs/data.claims.json by hand.

Scoring is BM25-ish and deliberately simple: rarer words count for more, a
chunk isn't rewarded for repeating a term, and phrases the caller quotes must
appear verbatim or the chunk is dropped. The corpus is 3-4 MB of prose, so this
runs in well under a second and needs nothing installed.

Run:  python -m ingest.check_claim "every dollar goes to public safety"
      python -m ingest.check_claim "the city has its own police" --kind staffing
      python -m ingest.check_claim '"a third of the size"' --top 12 --full
      python -m ingest.check_claim --inbox          # every unchecked intake row
      python -m ingest.check_claim --inbox --json   # same, machine-readable
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "docs" / "agent-corpus.json"
CLAIMS = ROOT / "docs" / "data.claims.json"
INBOX = ROOT / "data" / "claims" / "inbox.jsonl"

# Words that are in half the corpus because of what the corpus is about. Left in
# the query they rank every chunk about the city equally, which is the same as
# ranking nothing. Ordinary English stopwords are handled by the IDF term.
NOISE = {
    "eagle", "mountain", "city", "council", "councilmember", "meeting",
    "said", "says", "say", "the", "and", "that", "this", "with", "from",
    "they", "their", "there", "have", "has", "was", "were", "for", "our",
    "you", "your", "what", "who", "how", "why", "are", "not", "but", "all",
    "its", "it", "a", "an", "of", "to", "in", "on", "is", "be", "by", "at",
}

_WORD = re.compile(r"[a-z0-9][a-z0-9'%.$-]*")
_PHRASE = re.compile(r'"([^"]+)"|“([^”]+)”')


def tokens(text: str) -> list[str]:
    """Lowercase words, with the corpus-wide noise dropped."""
    return [w for w in _WORD.findall(str(text or "").lower()) if w not in NOISE and len(w) > 1]


def load_corpus() -> list[dict]:
    if not CORPUS.exists():
        raise SystemExit(
            f"{CORPUS.relative_to(ROOT)} is missing — run: python -m ingest.build_agent_corpus"
        )
    return json.loads(CORPUS.read_text(encoding="utf-8")).get("docs", [])


def index(docs: list[dict]) -> tuple[list[set[str]], dict[str, int], float]:
    """Per-document token sets, document frequencies, and the mean length.

    Sets, not counts: a transcript passage that says "tax" eleven times is not
    eleven times more about tax than one that says it once, and term frequency
    weighting mostly rewards length here.
    """
    sets = [set(tokens(d.get("title", "") + " " + d.get("text", ""))) for d in docs]
    df: Counter[str] = Counter()
    for s in sets:
        df.update(s)
    avg = sum(len(s) for s in sets) / max(1, len(sets))
    return sets, df, avg


def search(claim: str, docs: list[dict], sets, df, avg, *, kind: str | None = None,
           top: int = 8) -> list[tuple[float, dict]]:
    """Rank chunks against the claim. Quoted phrases are required, not weighted."""
    phrases = [(m.group(1) or m.group(2)).lower() for m in _PHRASE.finditer(claim)]
    terms = set(tokens(_PHRASE.sub(" ", claim))) | {t for p in phrases for t in tokens(p)}
    if not terms:
        return []

    n = len(docs)
    scored = []
    for i, doc in enumerate(docs):
        if kind and doc.get("kind") != kind:
            continue
        hay = (doc.get("title", "") + " " + doc.get("text", "")).lower()
        if any(p not in hay for p in phrases):
            continue
        hits = terms & sets[i]
        if not hits:
            continue
        # IDF, damped by document length so a 3,000-word transcript window
        # doesn't outrank the two-sentence figure that actually answers it.
        score = sum(math.log(1 + n / (1 + df[t])) for t in hits)
        score *= avg / (avg + len(sets[i]))
        # A chunk written as a claim and its verdict is what a claim query is
        # looking for, when one already exists.
        if doc.get("kind") == "claim":
            score *= 1.25
        scored.append((score, doc))

    scored.sort(key=lambda r: -r[0])
    return scored[:top]


def existing_claims() -> list[dict]:
    if not CLAIMS.exists():
        return []
    return json.loads(CLAIMS.read_text(encoding="utf-8")).get("claims", [])


def read_inbox() -> list[dict]:
    """Intake rows, newest last. A blank line or a # comment is skipped."""
    if not INBOX.exists():
        return []
    rows = []
    for lineno, line in enumerate(INBOX.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"  {INBOX.name}:{lineno}: skipped, not valid JSON ({exc.msg})", file=sys.stderr)
            continue
        row["_line"] = lineno
        rows.append(row)
    return rows


def report(claim: str, hits: list[tuple[float, dict]], *, full: bool, note: str = "") -> None:
    print(f"\n── {claim}")
    if note:
        print(f"   {note}")
    if not hits:
        print("   nothing in the corpus matched. Either the record doesn't cover it — which is")
        print("   itself a finding, and the entry should say so — or the wording is too far from")
        print("   how the meeting put it. Try the words a councilmember would have used.")
        return
    for score, doc in hits:
        head = f"   {score:6.2f}  [{doc.get('kind','')}] {doc.get('title','')}"
        print(head if len(head) < 118 or full else head[:115] + "…")
        meta = "          " + doc.get("url", "")
        if doc.get("date"):
            meta += f"  ·  {doc['date']}"
        print(meta)
        text = doc.get("text", "")
        print("          " + (text if full else text[:280] + ("…" if len(text) > 280 else "")))
        print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("claim", nargs="*", help="the claim, as it is circulating")
    ap.add_argument("--inbox", action="store_true",
                    help=f"check every unchecked row in {INBOX.relative_to(ROOT)}")
    ap.add_argument("--kind", help="restrict to one chunk kind (motion, meeting, transcript, tax, staffing, budget, claim)")
    ap.add_argument("--top", type=int, default=8, help="how many candidates per claim (default 8)")
    ap.add_argument("--full", action="store_true", help="print whole chunks rather than the first 280 characters")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    claim_text = " ".join(args.claim).strip()
    if not claim_text and not args.inbox:
        ap.error("give a claim to check, or --inbox")

    docs = load_corpus()
    sets, df, avg = index(docs)

    queue: list[dict] = []
    if claim_text:
        queue.append({"claim": claim_text})
    if args.inbox:
        published = {c.get("id") for c in existing_claims()}
        for row in read_inbox():
            if row.get("published_as") in published:
                continue
            queue.append(row)
        if not queue:
            print(f"{INBOX.relative_to(ROOT)}: nothing unchecked.")
            return 0

    results = []
    for row in queue:
        claim = row.get("claim", "")
        hits = search(claim, docs, sets, df, avg, kind=args.kind, top=args.top)
        results.append({
            "claim": claim,
            "seen": row.get("seen", ""),
            "candidates": [
                {"score": round(s, 2), "kind": d.get("kind"), "title": d.get("title"),
                 "url": d.get("url"), "date": d.get("date"), "text": d.get("text")}
                for s, d in hits
            ],
        })
        if not args.json:
            seen = row.get("seen", "")
            first = row.get("first_seen", "")
            note = " · ".join(x for x in (seen, first) if x)
            report(claim, hits, full=args.full, note=note)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print("\nThese are candidates, not a verdict. Read the meetings before writing the entry;")
        print("the page's own rules are in docs/data.claims.json under `how`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
