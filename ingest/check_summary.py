"""
Cross-check a generated meeting summary against facts the pipeline already
knows, and put anything that disagrees in front of a person.

The summaries are written by a model from the transcript. Everything in them is
prose, so nothing downstream ever compared them against the machine-readable
record of the same meeting — pipeline-quality-controls.md names that as the gap
worth closing next.

The first check is the cheapest one available. The Meeting map table gives each
item an elapsed position in the recording, and the .srt gives the recording's
true length. A position past the end of the audio cannot be right, whatever the
meeting was. On 2026-09-08 the Planning Commission summary placed two items at
2:28 and 2:32 in a recording that stops at 2:22:11.

This REPORTS, it does not correct. A drifting timestamp is a symptom and the
right response is a person looking at the meeting, not a script rewriting prose
a model wrote. Exit status is 1 when something was flagged, so a caller can act.

Run:
  python3 -m ingest.check_summary                          # every body, every meeting
  python3 -m ingest.check_summary --body planning-commission
  python3 -m ingest.check_summary --stem 2026-09-08__678 --body planning-commission
"""

from __future__ import annotations

import argparse
import math
import re
from pathlib import Path

from . import bodies

ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPTS = ROOT / "data" / "transcripts"

SRT_TIME = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->")

# An elapsed mark as the Meeting map writes it. The leading "~" the prompt asks
# for is optional and stripped before matching.
#
# A two-field mark is ambiguous and cannot be read on its own: "1:14" is an hour
# and 14 minutes in a four-hour council meeting and 74 seconds in a twenty-minute
# special session, and the prompt does not pin it down. The scale is therefore
# chosen per summary in `_scale`, against the one number that is not in dispute
# — the length of the recording.
ELAPSED = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")

# Meetings run long and a model rounds; only a mark clearly past the end is a
# finding. Two minutes is wider than any rounding and narrower than the 6- and
# 10-minute overruns that prompted this.
TOLERANCE_S = 120


def recording_seconds(srt: Path) -> int | None:
    """Length of the recording, from the last cue in the .srt."""
    try:
        text = srt.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    last = 0
    for h, m, s, _ms in SRT_TIME.findall(text):
        last = max(last, int(h) * 3600 + int(m) * 60 + int(s))
    return last or None


def _parse_elapsed(cell: str) -> tuple[int, int, int] | None:
    """The mark's raw fields. Three fields are H:MM:SS; two stay unresolved."""
    m = ELAPSED.match(cell.strip().lstrip("~").strip())
    if not m:
        return None
    a, b, c = m.groups()
    return (int(a), int(b), int(c)) if a else (-1, int(b), int(c))


def _scale(marks: list[tuple[int, int, int]], dur: int) -> str:
    """Whether this summary's two-field marks mean H:MM or MM:SS.

    A three-field mark is unambiguous, but a summary may mix the two in one
    column: the Community Services Board map for 22 July runs "23:46", "41:34",
    "1:24:26" — minutes and seconds until the meeting passes an hour, then
    hours. So the reading is chosen for the two-field marks alone.

    The map is chronological, so the test is which reading keeps it that way.
    Read as H:MM, that board's "23:46" would land nineteen hours after the row
    below it. Where both readings run forwards — a map with no three-field mark
    to anchor it — the tie goes to whichever finishes nearest the end of the
    recording, since a map should span roughly the meeting it describes.
    """
    two = [m for m in marks if m[0] < 0]
    if not two:
        return "hms"
    if not dur:
        return "hm"

    def backwards(scale: str) -> int:
        seq = [_seconds(m, scale) for m in marks]
        return sum(1 for a, b in zip(seq, seq[1:]) if b < a)

    def off(scale: str) -> float:
        last = max(_seconds(m, scale) for m in two)
        return abs(math.log(last / dur)) if last > 0 else float("inf")

    # Fewest steps backwards wins; a tie is broken on span, never on how many
    # marks overrun — that is the thing being tested and cannot judge itself.
    return min(("hm", "ms"), key=lambda sc: (backwards(sc), off(sc)))


def _seconds(mark: tuple[int, int, int], scale: str) -> int:
    a, b, c = mark
    if a >= 0:
        return a * 3600 + b * 60 + c
    return b * 60 + c if scale == "ms" else b * 3600 + c * 60


def meeting_map_marks(summary: Path) -> list[tuple[str, tuple[int, int, int]]]:
    """(row label, raw elapsed fields) for each Meeting map row carrying one."""
    try:
        lines = summary.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    out: list[tuple[str, int]] = []
    col: int | None = None
    in_map = False
    for line in lines:
        if line.startswith("## "):
            in_map = line.strip().lower().startswith("## meeting map")
            col = None
            continue
        if not in_map or "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if col is None:
            for i, c in enumerate(cells):
                if "elapsed" in c.lower():
                    col = i
                    break
            continue
        if col >= len(cells) or set("".join(cells)) <= set("-: "):
            continue
        mark = _parse_elapsed(cells[col])
        if mark is not None:
            out.append((cells[0] or "(row)", mark))
    return out


def _hms(s: int) -> str:
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def check(body_id: str, stem: str) -> list[str]:
    """Problems found with one meeting. Empty means nothing to look at."""
    d = TRANSCRIPTS / body_id
    summary, srt = d / f"{stem}.summary.md", d / f"{stem}.srt"
    if not summary.exists():
        return []

    problems: list[str] = []
    dur = recording_seconds(srt)
    marks = meeting_map_marks(summary)
    if dur is None:
        # No timing source. Say so rather than reporting a clean check.
        if marks:
            problems.append(f"no .srt to check {len(marks)} elapsed mark(s) against")
        return problems

    scale = _scale([m for _lab, m in marks], dur)
    resolved = [(lab, _seconds(m, scale)) for lab, m in marks]
    over = [(lab, s) for lab, s in resolved if s > dur + TOLERANCE_S]
    for lab, s in over:
        problems.append(
            f"meeting map row {lab!r} sits at {_hms(s)}, past the end of the "
            f"recording ({_hms(dur)})")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--body", help="only this body")
    ap.add_argument("--stem", help="only this meeting (needs --body)")
    args = ap.parse_args()
    if args.stem and not args.body:
        ap.error("--stem needs --body")

    body_ids = [args.body] if args.body else [b["id"] for b in bodies.all_bodies()]
    for bid in body_ids:
        bodies.get_body(bid)          # raises on an unknown id

    flagged = checked = 0
    for bid in body_ids:
        d = TRANSCRIPTS / bid
        if not d.is_dir():
            continue
        stems = ([args.stem] if args.stem
                 else sorted(f.name[: -len(".summary.md")] for f in d.glob("*.summary.md")))
        for stem in stems:
            problems = check(bid, stem)
            checked += 1
            if problems:
                flagged += 1
                print(f"{bid}/{stem}")
                for p in problems:
                    print(f"  · {p}")

    print(f"\n{checked} summary(ies) checked, {flagged} flagged for review")
    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main())
