#!/usr/bin/env bash
#
# transcribe_meeting.sh — one-command meeting transcript + enriched summary.
#
# Given a CivicClerk event id, this:
#   1. looks up the meeting (date + recording URL) from the public API
#   2. streams the audio and transcribes it on-device (whisper.cpp, Metal)
#   3. writes .txt/.srt/.vtt into data/transcripts/<body>/
#   4. generates the prose "enriched summary" with the `claude` CLI
#   5. publishes to docs/ via `python -m ingest.build_transcripts`
#
# Idempotent: steps whose output already exists are skipped (override with
# --force to re-transcribe, --resummarize to regenerate the summary).
#
# Usage:
#   scripts/transcribe_meeting.sh <EVENT_ID> [--body city-council]
#                                            [--force] [--resummarize]
#
# Requirements: ffmpeg, whisper-cli (brew install ffmpeg whisper-cpp),
#               claude CLI (for the summary), python3.
set -euo pipefail

# ---- locate repo root (script lives in <root>/scripts) ----
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ---- config ----
BODY="city-council"
FORCE=0
RESUMMARIZE=0
EVENT_ID=""
API_BASE="${COUNCIL_API_BASE:-https://eaglemountainut.api.civicclerk.com/v1}"
PORTAL="${COUNCIL_PORTAL:-https://eaglemountainut.portal.civicclerk.com}"
CACHE="${COUNCIL_CACHE:-$HOME/.cache/council-tracking}"
MODEL="$CACHE/ggml-large-v3-turbo.bin"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"

# ---- parse args ----
while [ $# -gt 0 ]; do
  case "$1" in
    --body)        BODY="$2"; shift 2;;
    --force)       FORCE=1; shift;;
    --resummarize) RESUMMARIZE=1; shift;;
    -h|--help)     grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    -*)            echo "unknown flag: $1" >&2; exit 2;;
    *)             EVENT_ID="$1"; shift;;
  esac
done
[ -n "$EVENT_ID" ] || { echo "usage: $0 <EVENT_ID> [--body <id>] [--force] [--resummarize]" >&2; exit 2; }

say() { printf '\033[1;32m▸ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---- dependency checks ----
for bin in ffmpeg whisper-cli python3 curl; do
  command -v "$bin" >/dev/null || die "missing dependency: $bin"
done
HAVE_CLAUDE=1; command -v claude >/dev/null || HAVE_CLAUDE=0

# ---- 1. resolve the meeting from the API ----
say "Looking up event $EVENT_ID …"
META_JSON="$(curl -fsS "$API_BASE/Events?\$filter=id%20eq%20$EVENT_ID" -H 'Accept: application/json')" \
  || die "API lookup failed"

read -r DATE MP4 AGENDA_FILE_ID < <(python3 - "$META_JSON" <<'PY'
import json, sys
d = json.loads(sys.argv[1]).get("value", [])
if not d:
    print("NONE NONE NONE"); sys.exit(0)
e = d[0]
date = (e.get("eventDate") or "")[:10] or "unknown"
mp4  = e.get("mediaSourcePathMp4") or e.get("mediaStreamPath") or ""
agenda = ""
for f in (e.get("publishedFiles") or []):
    if f.get("fileType") == 1:           # 1 = Agenda
        agenda = str(f.get("fileId") or ""); break
print(date, mp4 or "NONE", agenda or "NONE")
PY
)

[ "$DATE" != "NONE" ] || die "event $EVENT_ID not found on the portal"
[ "$MP4" != "NONE" ]  || die "event $EVENT_ID has no recording yet (mediaSourcePathMp4 empty)"

STEM="${DATE}__${EVENT_ID}"
DIR="data/transcripts/$BODY"
mkdir -p "$DIR"
WAV="$DIR/$STEM.wav"
TXT="$DIR/$STEM.txt"
SUMMARY="$DIR/$STEM.summary.md"
say "Meeting $STEM  ·  body=$BODY"

# ---- ensure the whisper model is cached ----
if [ ! -f "$MODEL" ]; then
  say "Downloading whisper model (~1.6 GB, one time) → $MODEL"
  mkdir -p "$CACHE"
  curl -fL --progress-bar -o "$MODEL" "$MODEL_URL" || die "model download failed"
fi

# ---- 2 + 3. extract audio and transcribe ----
if [ "$FORCE" = 1 ] || [ ! -f "$TXT" ]; then
  say "Extracting audio (streaming from CivicClerk; a long meeting can take a few minutes) …"
  ffmpeg -y -nostdin -loglevel error -i "$MP4" -vn -ac 1 -ar 16000 -c:a pcm_s16le "$WAV" \
    || die "ffmpeg audio extraction failed"

  say "Transcribing on-device (whisper.cpp) …"
  whisper-cli -m "$MODEL" -f "$WAV" -l en -otxt -osrt -ovtt -of "$DIR/$STEM" -pp \
    || die "whisper-cli failed"
  rm -f "$WAV"

  # Collapse runs of identical consecutive lines — whisper hallucinates repeats
  # over long silences (e.g. the closed-session gap).
  python3 - "$TXT" <<'PY'
import sys
p = sys.argv[1]
out, prev = [], None
for line in open(p, encoding="utf-8"):
    s = line.rstrip("\n")
    if s.strip() and s == prev:
        continue
    out.append(s); prev = s
open(p, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
  words=$(wc -w < "$TXT" | tr -d ' ')
  say "Transcript written: $TXT  (${words} words)"
else
  say "Transcript exists — skipping transcription (use --force to redo)."
fi

# ---- 4. enriched summary via the claude CLI ----
if [ "$RESUMMARIZE" = 1 ] || [ ! -f "$SUMMARY" ]; then
  if [ "$HAVE_CLAUDE" = 0 ]; then
    echo "⚠ 'claude' CLI not found — skipping summary. Transcript is still published." >&2
  else
    say "Generating enriched summary with claude …"
    AGENDA_TXT=""
    if [ "$AGENDA_FILE_ID" != "NONE" ]; then
      AGENDA_TXT="$(curl -fsS "$API_BASE/Meetings/GetMeetingFileStream(fileId=$AGENDA_FILE_ID,plainText=true)" || true)"
    fi

    INSTRUCTIONS=$(cat <<'PROMPT'
You are producing an "enriched meeting summary" in Markdown from the full
speech-to-text transcript of an Eagle Mountain, UT public meeting. The official
agenda and the transcript follow this instruction block as input.

Write for a reader who did NOT attend: explanatory PROSE, not fragmentary
bullets. Ground every claim ONLY in the transcript — do not invent facts, names,
numbers, or votes. Keep short direct quotes (in quotation marks) where useful.

KNOWN ROSTER (correct these common Whisper mishearings):
- Mayor Jared Gray.
- Councilmembers: Melissa Clark, Brett Wright, Craig Whiting, Rich Wood, Zac Huish.
  "Hewish" in the transcript = Zac Huish. Roll-call surnames are reliable.
- Zac Hilton is a STAFF member (parks/rec) — a different person from Councilmember Zac Huish.
Attribute discussion to a named member only when the transcript makes it clear
(explicit names, or roll-call context); otherwise say "a councilmember."

Produce these sections, with this exact structure and numbering:

# Enriched Meeting Summary — Eagle Mountain <BODY> , <DATE>
*One italic attribution line: note it was generated automatically from the audio, that times are approximate elapsed-recording times, and that speaker attribution without diarization is imperfect.*

Then **Elected officials present** and **Key staff/presenters** lines (bold labels).

## 1. Meeting Map
A Markdown TABLE with columns: Agenda ref | Topic | ~Start (elapsed) | One-line note — one row per distinct item/topic. After the table, a short "Discussed but NOT on the agenda" line and an "On agenda but little/no discussion" line.

## 2. Per-Item Enrichment
For each substantive item, a `### <ref> — <title>` heading followed by 1–3 explanatory PARAGRAPHS that open by stating what the item is and why it was before the body, then narrate the debate, concerns, and staff answers.

## 3. Decisions & Votes
A numbered list; each entry a complete sentence (motion, mover/seconder if stated, outcome, vote split).

## 4. Notable Moments
A bulleted list; each bullet a short **bold lead-in.** then a self-contained sentence or two (public comments, disagreements, off-agenda items).

## 5. Transcript Quality Notes
Brief prose: likely misheard proper nouns, speaker-attribution limits, and audio-unclear spots.

## 6. Minutes-vs-Recording Contrast
2–3 items, each showing "*Minutes would say:*" vs. "*Recording captures:*" as a short paragraph.

Output ONLY the Markdown document, starting at the `# Enriched Meeting Summary` H1. No preamble or commentary.
PROMPT
)

    if {
        printf '%s\n\n' "$INSTRUCTIONS"
        printf '=== OFFICIAL AGENDA ===\n%s\n\n' "${AGENDA_TXT:-(agenda unavailable)}"
        printf '=== FULL TRANSCRIPT ===\n'
        cat "$TXT"
      } | claude -p > "$SUMMARY.tmp" && [ -s "$SUMMARY.tmp" ]; then
        # Trim anything before the first H1, just in case.
        python3 - "$SUMMARY.tmp" "$SUMMARY" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
t = open(src, encoding="utf-8").read()
i = t.find("# Enriched Meeting Summary")
open(dst, "w", encoding="utf-8").write(t[i:] if i >= 0 else t)
PY
        rm -f "$SUMMARY.tmp"
        say "Summary written: $SUMMARY"
    else
        rm -f "$SUMMARY.tmp"
        die "claude summary generation failed"
    fi
  fi
else
  say "Summary exists — skipping (use --resummarize to regenerate)."
fi

# ---- 5. publish to docs/ ----
say "Publishing to docs/ …"
python3 -m ingest.build_transcripts

echo
say "Done: $STEM"
echo "   Recording : $PORTAL/event/$EVENT_ID/media"
echo "   Transcript: $TXT"
echo "   Summary   : $SUMMARY"
echo "   Preview locally:  python3 -m http.server 8787  →  http://127.0.0.1:8787/docs/meetings.html?id=$EVENT_ID"
echo "   Go live:  git add data/transcripts docs/transcripts && git commit -m \"data: transcript $STEM\" && git push"
