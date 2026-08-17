"""
Write one plain-English sentence for each item on an upcoming agenda, and merge
those sentences into docs/data.upcoming.json.

  python -m ingest.summarize_agenda                # anything new on the calendar
  python -m ingest.summarize_agenda --dry-run      # show what would run, no LLM
  python -m ingest.summarize_agenda --force        # re-write sentences already cached
  python -m ingest.summarize_agenda --merge-only   # re-merge the cache, no LLM

Why this exists: the agenda is written for the record, not for a reader. An item
arrives as "A Resolution of Eagle Mountain City, Utah, Awarding the Bid for EMC
6.0 MGD Wastewater Treatment Facility Expansion - GMP-2 Oxidation Ditch and
Miscellaneous Site Improvements to MGC Contractors, Inc. and Approving the
Contract." A resident deciding whether to give up a Tuesday evening needs "The
council will award the contract to build the next stage of the sewer plant."

This is the forward-looking twin of summarize_motions.py, and follows the same
conventions:

* Shells out to the `claude` CLI rather than pulling in an SDK — no API key to
  manage. If `claude` isn't installed we warn and exit 0, and the site simply
  shows the city's own wording.
* Idempotent, keyed by a content hash of the item (see ingest.agenda_summaries),
  so a re-run costs nothing and an amended agenda re-summarizes only what
  changed.
* Never called from CI. The weekly ingest rebuilds the calendar and re-merges
  whatever is cached; run this locally when a new agenda is posted, and commit
  data/meta/agenda_summaries.json alongside docs/data.upcoming.json.

The one rule that matters: this describes what the council is being ASKED to do.
Nothing on this page has happened yet, and a sentence that sounds like a decision
already taken would be worse than the legalese it replaced.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import agenda_summaries

ROOT = Path(__file__).resolve().parent.parent
UPCOMING = ROOT / "docs" / "data.upcoming.json"

CLAUDE_TIMEOUT_S = 300
# A council agenda runs to about two dozen items; the cap is a guard against a
# pathological parse, not a real limit.
MAX_ITEMS_PER_CALL = 30


PROMPT = """\
You are writing plain-English descriptions of what the Eagle Mountain, Utah City
Council is ABOUT TO CONSIDER at its next meeting, for a public transparency site
read by residents and local journalists. Assume the reader has no legal or
municipal background.

Each numbered item below is taken from the posted agenda. NOTHING HAS HAPPENED
YET — the meeting has not been held. For EACH item, produce:

  "plain" — ONE sentence, at most 28 words, saying what the council is being
            asked to do and to what. Plain words, sentence case, ends with a
            period.

            Good: "The council will award the contract to build the next stage of
                   the city's sewer plant."
            Good: "The council will decide whether to undo the property tax
                   increase it approved on August 6."
            Good: "Staff will walk the council through a study of how many city
                   employees it needs."
            Bad:  "The council approved the wastewater treatment facility
                   expansion."            (it hasn't — this is next week's agenda)
            Bad:  "Consideration of a resolution awarding a bid pursuant to the
                   procurement code."      (legalese, says nothing)
            Bad:  "An important item affecting the community."  (empty)

RULES
- Future or neutral tense only: "will decide", "will consider", "will hold a
  public hearing on", "staff will present". Never past tense, and never imply an
  outcome — the vote has not been taken.
- Ground every word ONLY in the item's title and background text below. Do NOT
  invent figures, names, addresses, dollar amounts, or effects. Keep a concrete
  number (dollars, acres, percent, a code section's subject) when the item states
  one, and drop it when it doesn't.
- Say what the thing IS in ordinary words: "sewer plant" not "wastewater
  treatment facility"; "apartments" not "multi-family residential units"; "the
  city's audited books for last year" not "the Fiscal Year 2025 Audited Financial
  Statements"; "street-naming rules on new subdivision plats" not "Municipal Code
  16.20.040 (Preliminary Plats) regarding Street Names".
- Drop the boilerplate every item carries: "A Resolution of Eagle Mountain City,
  Utah,", "An Ordinance of…", "Approving", "Adopting" as an opener.
- Where the item is a public hearing, say so — that is the reader's cue that they
  can speak on it.
- Housekeeping stays housekeeping. If the item really is routine paperwork, say
  so plainly ("The council will sign off on the minutes of four earlier
  meetings") rather than dressing it up.
- If an item is genuinely too thin to describe (a bare title with no background),
  write the shortest honest sentence the words support. Never pad.

Output ONLY a JSON object mapping each item's number (as a string) to an object
with a "plain" key. No markdown, no code fence, no commentary.

Example of the exact output shape:
{"1": {"plain": "The council will decide whether to raise the property tax rate for the coming year."}, "2": {"plain": "Staff will brief the council on plans for a business park along Pony Express Parkway."}}
"""


def build_prompt(meeting_date: str, items: list[dict]) -> str:
    lines = [PROMPT, f"\n=== AGENDA: {meeting_date} ===\n"]
    for i, it in enumerate(items, 1):
        lines.append(f"--- ITEM {i} ---")
        if it.get("heading"):
            lines.append(f"Under heading: {it['heading']}")
        if it.get("kind"):
            lines.append(f"Type: {it['kind']}")
        lines.append(f"Title as posted: {it.get('title') or '(none)'}")
        if it.get("background"):
            lines.append(f"Background from the agenda: {it['background']}")
        lines.append("")
    return "\n".join(lines)


def call_claude(prompt: str) -> dict:
    """Run `claude -p` and parse its JSON reply. Raises on failure."""
    proc = subprocess.run(
        ["claude", "-p"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:300]}")

    out = proc.stdout.strip()
    if not out:
        raise RuntimeError("claude returned nothing")
    if out.startswith("```"):
        out = out.split("\n", 1)[1] if "\n" in out else out
        out = out.rsplit("```", 1)[0]
    start, end = out.find("{"), out.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"no JSON object in claude output: {out[:200]!r}")
    return json.loads(out[start : end + 1])


def clean(text: str, limit: int = 240) -> str:
    """One tidy sentence. A model that ignores the word limit gets cut at a
    sentence boundary rather than mid-word."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = cut.rfind(". ")
    return (cut[: stop + 1] if stop > 60 else cut.rstrip(" ,;:")).strip()


def worth_summarizing(head: dict) -> bool:
    """Procedural blocks — the pledge, the manager's slot, adjournment — never
    reach the page, so they never reach the model either."""
    return not head.get("procedural")


def collect(feed: dict) -> list[tuple[str, str, list[dict]]]:
    """-> [(body id, meeting date, [item, ...])] for every posted agenda on file.

    Each item carries its heading along, because "Bid award" under
    "TRUTH-IN-TAXATION" and the same words under "CONSENT AGENDA" are not the
    same item, and the model reads better with the context.
    """
    out = []
    for body_id, events in (feed.get("bodies") or {}).items():
        for ev in events:
            items = [
                {**item, "heading": head.get("title", "")}
                for head in ev.get("agenda") or []
                if worth_summarizing(head)
                for item in head["items"]
            ]
            if items:
                out.append((body_id, ev["date"], items))
    return out


def merge_into_feed(feed: dict, cache: dict[str, dict]) -> int:
    """Write cached sentences into the feed in place. Returns how many landed."""
    n = 0
    for events in (feed.get("bodies") or {}).values():
        for ev in events:
            for head in ev.get("agenda") or []:
                for item in head["items"]:
                    got = cache.get(agenda_summaries.item_key(item))
                    plain = (got or {}).get("plain", "")
                    if plain != item.get("plain", ""):
                        item["plain"] = plain
                        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--force", action="store_true",
                    help="re-summarize items already in the cache")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would run; no LLM calls, no writes")
    ap.add_argument("--merge-only", action="store_true",
                    help="just re-merge the cache into docs/data.upcoming.json")
    args = ap.parse_args()

    if not UPCOMING.exists():
        print(f"no {UPCOMING.relative_to(ROOT)} — run build_upcoming first",
              file=sys.stderr)
        return 1
    feed = json.loads(UPCOMING.read_text())
    cache = agenda_summaries.load_cache()

    if not args.merge_only:
        if not shutil.which("claude"):
            print("⚠ `claude` CLI not found — skipping agenda summaries. The site "
                  "still builds; items fall back to the city's own wording.",
                  file=sys.stderr)
            return 0

        agendas = collect(feed)
        todo = [
            (body_id, date, [
                it for it in items
                if args.force or not (cache.get(agenda_summaries.item_key(it)) or {}).get("plain")
            ])
            for body_id, date, items in agendas
        ]
        todo = [t for t in todo if t[2]]
        pending = sum(len(items) for _, _, items in todo)
        cached = sum(len(items) for _, _, items in agendas) - pending
        print(f"cache: {agenda_summaries.CACHE.relative_to(ROOT)} ({len(cache)} items)")
        print(f"{pending} items to summarize across {len(todo)} agendas ({cached} cached)")

        if args.dry_run:
            for body_id, date, items in todo:
                print(f"  would summarize {body_id} {date}: {len(items)} items")
            return 0

        failures = 0
        for body_id, date, items in todo:
            for start in range(0, len(items), MAX_ITEMS_PER_CALL):
                chunk = items[start : start + MAX_ITEMS_PER_CALL]
                try:
                    reply = call_claude(build_prompt(date, chunk))
                except Exception as exc:
                    print(f"  ! {body_id} {date}: {exc}", file=sys.stderr)
                    failures += 1
                    continue
                got = 0
                for i, item in enumerate(chunk, 1):
                    plain = clean((reply.get(str(i)) or {}).get("plain"))
                    if plain:
                        cache[agenda_summaries.item_key(item)] = {"plain": plain}
                        got += 1
                print(f"  ✓ {body_id} {date}: {got}/{len(chunk)} items")
                # Checkpoint after each call, so a Ctrl-C keeps what it earned.
                agenda_summaries.save_cache(cache)
        if failures:
            print(f"  {failures} agenda(s) failed — re-run to retry just those",
                  file=sys.stderr)

    changed = merge_into_feed(feed, cache)
    if changed:
        UPCOMING.write_text(json.dumps(feed, indent=2, ensure_ascii=False) + "\n")
    print(f"{'wrote' if changed else 'no change to'} "
          f"{UPCOMING.relative_to(ROOT)} ({changed} items updated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
