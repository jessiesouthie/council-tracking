"""
Publish meeting transcripts to the public site.

Source of truth lives in data/transcripts/<body>/<YYYY-MM-DD>__<eventId>.*:
  .txt         full plain-text transcript (one line per Whisper segment)
  .srt / .vtt  timestamped versions (optional; .srt is used to derive duration)
  .summary.md  the enriched, prose meeting summary

GitHub Pages only serves docs/, so this copies the two files the site needs
(.summary.md and .txt) into docs/transcripts/<body>/ and writes a manifest at
docs/transcripts/index.json that the meetings page reads to know which meetings
have a transcript (and to render meetings whose minutes aren't published yet).

Run:  python -m ingest.build_transcripts
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import bodies

ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = ROOT / "data" / "transcripts"
OUT_ROOT = ROOT / "docs" / "transcripts"

# Filename is "<YYYY-MM-DD>__<eventId>"; mirrors data/raw + data/parsed naming.
STEM_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})__(\d+)$")
SRT_TS_RE = re.compile(r"-->\s*(\d{2}):(\d{2}):(\d{2})")
# A cloud (AssemblyAI) transcript prefixes every line with a diarized turn label.
SPEAKER_RE = re.compile(r"^Speaker [A-Z]+:")

# CivicClerk public portal for Eagle Mountain; event id -> media page.
PORTAL_MEDIA = "https://eaglemountainut.portal.civicclerk.com/event/{id}/media"

# A meeting the recorder filed under the portal's catch-all category is hand-filed
# here under a synthetic id (900000 + MMDD) rather than its portal event id, so the
# crawler cannot later re-add it under the real id and leave the site showing the
# night twice. That id is ours; the recording still lives under the city's, so the
# "watch it" link has to be sent back to the event the city actually published.
SYNTHETIC_TO_PORTAL = {
    900826: 770,   # 26 August 2026 special session
}


def _duration_from_srt(srt: Path) -> str | None:
    """Return a human duration ("4h 44m") from the last cue end time, or None."""
    if not srt.exists():
        return None
    last = 0
    for h, m, s in SRT_TS_RE.findall(srt.read_text(encoding="utf-8", errors="ignore")):
        last = max(last, int(h) * 3600 + int(m) * 60 + int(s))
    if not last:
        return None
    hrs, mins = last // 3600, (last % 3600) // 60
    return f"{hrs}h {mins:02d}m" if hrs else f"{mins}m"


def build() -> dict:
    manifest: dict[str, list[dict]] = {}
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    for body in bodies.all_bodies():
        bid = body["id"]
        src_dir = SRC_ROOT / bid
        if not src_dir.is_dir():
            continue
        entries: list[dict] = []
        for summary in sorted(src_dir.glob("*.summary.md")):
            stem = summary.name[: -len(".summary.md")]
            m = STEM_RE.match(stem)
            if not m:
                print(f"  skip (bad name): {summary.name}")
                continue
            date, event_id = m.group(1), int(m.group(2))
            txt = src_dir / f"{stem}.txt"
            if not txt.exists():
                print(f"  skip (no .txt): {stem}")
                continue

            out_dir = OUT_ROOT / bid
            out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(summary, out_dir / summary.name)
            shutil.copyfile(txt, out_dir / txt.name)

            text = txt.read_text(encoding="utf-8", errors="ignore")
            # Cloud transcripts carry per-turn "Speaker X:" labels; on-device
            # Whisper ones don't. The site renders diarized turns as grouped
            # speaker blocks, so tell it which transcripts have them.
            diarized = any(SPEAKER_RE.match(ln) for ln in text.splitlines())

            # A hand/AI-built letter->name map (data/…/<stem>.speakers.json) lets
            # the site show inferred real names instead of "Speaker A". Optional;
            # unmapped letters stay as "Speaker X" in the UI.
            speakers_file = None
            speakers_src = src_dir / f"{stem}.speakers.json"
            if diarized and speakers_src.exists():
                shutil.copyfile(speakers_src, out_dir / speakers_src.name)
                speakers_file = f"transcripts/{bid}/{speakers_src.name}"

            entries.append({
                "id": event_id,
                "date": date,
                "stem": stem,
                "title": f"{body['label']} Meeting",
                "summary_file": f"transcripts/{bid}/{summary.name}",
                "text_file": f"transcripts/{bid}/{txt.name}",
                "speakers_file": speakers_file,
                "diarized": diarized,
                "media_url": PORTAL_MEDIA.format(
                    id=SYNTHETIC_TO_PORTAL.get(event_id, event_id)),
                "duration": _duration_from_srt(src_dir / f"{stem}.srt"),
                "words": len(text.split()),
            })
        if entries:
            entries.sort(key=lambda e: e["date"], reverse=True)
            manifest[bid] = entries
            print(f"  {bid}: {len(entries)} transcript(s)")

    index = OUT_ROOT / "index.json"
    index.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {index.relative_to(ROOT)}")
    return manifest


if __name__ == "__main__":
    build()
