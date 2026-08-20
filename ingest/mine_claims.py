"""
Read the transcripts for claims already in circulation, and propose them for checking.

check_claim.py answers "what does the record say about this claim". This is the
step before it: "what claims are out there at all". The answer turns out to be
sitting in the transcripts already, because public comment is where a rumour
surfaces in a form this project can use — someone believed it strongly enough to
drive to a meeting, say it on the record, and have it transcribed and published.

The tell is not the claim, it is the attribution people put in front of it.
"I've heard that…", "I read on Facebook that…", "people are saying…" — a speaker
marking a statement as something in circulation rather than as their own opinion.
That marker is what separates a rumour from an argument, and it is what this
searches for. A resident saying "this tax is too high" is an opinion and does not
belong on the claims page; one saying "I heard the highway is going to be six
lanes" is reporting something going around, and that is checkable.

Precision, not recall, is the target: a queue that is mostly noise does not get
read. Roughly half of what comes back is a real candidate, which is a reasonable
trade for a list a person skims in a few minutes.

Nothing here captures a name. Half the transcripts are not diarized at all, and
the diarized ones carry "Speaker J" rather than an identity — which suits the
claims page's own rule that the claim is quoted and the claimant is not named.

Run:  python -m ingest.mine_claims
      python -m ingest.mine_claims --since 2026-01-01 --top 25
      python -m ingest.mine_claims --write        # append candidates to the inbox
      python -m ingest.mine_claims --body community-services-board
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
INDEX = DOCS / "transcripts" / "index.json"
CLAIMS = DOCS / "data.claims.json"
INBOX = ROOT / "data" / "claims" / "inbox.jsonl"

# Someone marking a statement as second-hand. This is the whole search: it is the
# grammar of a rumour being repeated, and it is rare enough in a council meeting
# that a hit is nearly always worth a look.
#
# Two tiers, because they are not equally diagnostic. "I've heard that" and "the
# rumour is" mark second-hand information and almost nothing else. "on Facebook",
# "I read that", "going around" are context — they appear around rumours, and
# equally around a councilmember describing their own posting history. So a weak
# marker only counts when something checkable sits in the same breath as it.
STRONG = re.compile(
    r"\b("
    r"i(?:'ve| have)? (?:heard|been told)"
    r"|i was told"
    r"|people are saying|they(?:'re| are) saying|everyone(?:'s| is) saying"
    r"|word (?:on the street|is)"
    r"|(?:there(?:'s| is) a |the )rumou?rs?"
    r"|rumou?rs? (?:i|that|are|have)"
    r"|someone (?:posted|said|told)|somebody (?:posted|said|told)"
    r"|the (?:claim|story) (?:going around|is that)"
    r")\b",
    re.I,
)
WEAK = re.compile(
    r"\b("
    r"i read (?:on|in|that)"
    r"|(?:saw|read) (?:it )?on (?:facebook|social media|nextdoor|the internet)"
    r"|(?:on|from) (?:facebook|social media|nextdoor)"
    r"|going around"
    r"|the word (?:out there|around)"
    r")\b",
    re.I,
)
CIRCULATING = re.compile(f"{STRONG.pattern}|{WEAK.pattern}", re.I)

# The marker without a claim behind it. "What I heard David say" is attribution,
# "I'd love to hear from you" is an invitation, and "I heard from residents" is
# a councilmember describing their inbox. All three carry a marker and none of
# them is a rumour, so they are subtracted rather than left to a human to skim.
NOT_A_CLAIM = re.compile(
    r"\b("
    r"i(?:'ve| have)? heard (?:you|him|her|them|from|a lot|that concern)"
    r"|what i(?:'ve| have)? heard \w+ say"
    r"|heard \w+ say"
    r"|(?:love|like|want|glad) to hear"
    r"|hear from (?:you|the public|residents|citizens)"
    r"|public hearing|hearing is (?:now )?(?:open|closed)"
    r"|haven'?t heard|didn'?t hear|can you hear"
    r"|hearing from (?:competitors|suppliers|vendors)"
    r")\b",
    re.I,
)

# A claim has something in it that can be checked. Weighted, not required for a
# strong marker — the frontage-road rumour carries no figure and is still the
# best kind of hit — but a weak marker earns its place only by carrying one.
CHECKABLE = (
    (re.compile(r"\d{1,3}(?:,\d{3})+|\$\s?\d|\d+\s?(?:%|percent|million|acres|lanes|deputies|homes|units)", re.I), 2.4),
    (re.compile(r"\b(?:the city|the council|the mayor|city hall|they)\b\s+(?:is|are|was|were|has|have|had|will|would|voted|approved|sold|bought|paid|spent|plans?|allowing)", re.I), 2.0),
    (re.compile(r"\b(?:every|all|none|nobody|no one|always|never|only)\b", re.I), 0.9),
    (re.compile(r"\b(?:approved|denied|sold|bought|rezoned|annexed|contract|lawsuit|secret|hidden|behind closed doors)\b", re.I), 1.2),
    (re.compile(r"\b(?:data ?cent(?:er|re)|tax|sheriff|police|fire|water|road|highway|park|library|budget|developer)\b", re.I), 0.8),
)

_WS = re.compile(r"\s+")
_SENT = re.compile(r"(?<=[.!?])\s+")
_SPEAKER = re.compile(r"^\s*(?:Speaker\s+[A-Z0-9]+|[A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){0,3})\s*:\s*")


def _clean(text: str) -> str:
    return _WS.sub(" ", str(text or "")).strip()


def sentences(text: str) -> list[str]:
    """The transcript as sentences, with any speaker label stripped off the front.

    The .txt files are hard-wrapped mid-sentence, so lines are joined before
    splitting. Diarized files carry a "Speaker J:" prefix, which is dropped: it
    identifies nobody, and carrying it into a quotation would only imply it did.
    """
    joined = _clean(text.replace("\n", " "))
    out = []
    for raw in _SENT.split(joined):
        s = _SPEAKER.sub("", raw).strip()
        if s:
            out.append(s)
    return out


def score(passage: str, marker_sentence: str) -> float:
    """How likely this is a claim in circulation rather than an aside.

    `marker_sentence` is the sentence the marker was found in. A weak marker is
    only counted when the checkable part sits in that sentence or the one after
    it — otherwise "I would rant on Facebook" scores off a figure three
    sentences later that has nothing to do with it.
    """
    if NOT_A_CLAIM.search(passage):
        return 0.0

    strong = bool(STRONG.search(passage))
    if not strong and not WEAK.search(passage):
        return 0.0

    total = 3.0 if strong else 1.6
    scope = passage if strong else marker_sentence
    for pattern, weight in CHECKABLE:
        if pattern.search(scope):
            total += weight

    # A marker at the very end of a window means the claim itself fell outside
    # it; those read as fragments and are worth less than a complete thought.
    if CIRCULATING.search(passage[-60:]) and not CIRCULATING.search(passage[:-60]):
        total -= 1.5
    if len(passage.split()) < 14:
        total -= 1.2
    return total


def windows(text: str, after: int = 2) -> list[tuple[str, str]]:
    """Each sentence carrying a marker, plus the sentences that follow it.

    The claim almost never sits in the sentence with the marker in it — "I've
    heard" is followed by what was heard — so the window runs forward. Returns
    (window, marker sentence): the second is what a weak marker is judged on.
    """
    sents = sentences(text)
    out = []
    for i, s in enumerate(sents):
        if not CIRCULATING.search(s):
            continue
        # One sentence of run-up carries the subject when the marker sentence
        # opens with "And that…".
        start = max(0, i - 1)
        near = " ".join(sents[i : i + 2])
        out.append((" ".join(sents[start : i + 1 + after]), near))
    return out


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", str(text or "").lower())}


def already_known() -> list[set[str]]:
    """Token sets for every claim already published or already queued."""
    known = []
    if CLAIMS.exists():
        for c in json.loads(CLAIMS.read_text(encoding="utf-8")).get("claims", []):
            known.append(_tokens(c.get("claim", "") + " " + c.get("ruling", "")))
    if INBOX.exists():
        for line in INBOX.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                known.append(_tokens(json.loads(line).get("claim", "")))
            except json.JSONDecodeError:
                continue
    return known


def is_new(passage: str, known: list[set[str]], threshold: float = 0.55) -> bool:
    """True unless the passage mostly repeats something already on the list."""
    words = _tokens(passage)
    if not words:
        return False
    for other in known:
        if not other:
            continue
        overlap = len(words & other) / len(other)
        if overlap >= threshold:
            return False
    return True


def mine(body: str, since: str | None = None) -> list[dict]:
    if not INDEX.exists():
        raise SystemExit(f"{INDEX.relative_to(ROOT)} is missing — run: python -m ingest.build_transcripts")

    index = json.loads(INDEX.read_text(encoding="utf-8"))
    meetings = index.get(body) or []
    if not meetings:
        raise SystemExit(f"no transcripts on file for body {body!r}")

    known = already_known()
    found: list[dict] = []
    for m in meetings:
        if since and str(m.get("date", "")) < since:
            continue
        path = DOCS / str(m.get("text_file", ""))
        if not path.exists():
            print(f"  {m.get('date')}: {path.name} missing, skipped", file=sys.stderr)
            continue
        for passage, marker in windows(path.read_text(encoding="utf-8")):
            s = score(passage, marker)
            if s <= 3.0:
                continue
            if not is_new(passage, known):
                continue
            known.append(_tokens(passage))  # don't return the same rumour twice
            found.append({
                "score": round(s, 1),
                "meeting": m.get("id"),
                "date": m.get("date"),
                "passage": _clean(passage),
            })

    found.sort(key=lambda r: (-r["score"], str(r["date"])))
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--body", default="city-council", help="body id (default city-council)")
    ap.add_argument("--since", help="only meetings on or after this ISO date")
    ap.add_argument("--top", type=int, default=20, help="how many candidates to show (default 20)")
    ap.add_argument("--write", action="store_true",
                    help=f"append the candidates to {INBOX.relative_to(ROOT)} for triage")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    found = mine(args.body, args.since)[: args.top]

    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return 0

    if not found:
        print("nothing new. Every circulating claim in these transcripts is already "
              "published or already queued.")
        return 0

    for row in found:
        print(f"\n── {row['score']:4.1f}  meeting #{row['meeting']} · {row['date']}"
              f"  (meetings.html?id={row['meeting']})")
        print(f"   {row['passage']}")

    if args.write:
        # `claim` holds the passage as spoken, because the one-sentence version
        # of it is an editorial act and belongs to whoever checks it. The flag
        # says so, and stays on the row until a person rewrites it.
        with INBOX.open("a", encoding="utf-8") as fh:
            for row in found:
                fh.write(json.dumps({
                    "claim": row["passage"],
                    "seen": f"public comment, meeting #{row['meeting']}",
                    "first_seen": row["date"],
                    "meeting": row["meeting"],
                    "needs_wording": True,
                }, ensure_ascii=False) + "\n")
        print(f"\nappended {len(found)} candidates to {INBOX.relative_to(ROOT)}")
    else:
        print(f"\n{len(found)} candidates. These are things somebody said were going around —")
        print("not claims yet. Re-run with --write to queue them, then check each one with")
        print("  python -m ingest.check_claim --inbox")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
