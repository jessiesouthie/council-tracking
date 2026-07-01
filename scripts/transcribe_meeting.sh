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
#                                [--force] [--resummarize] [--no-summary] [--cloud]
#
#   --no-summary   transcribe + publish only; skip the summary (uses NO Claude
#                  credits). Run again later without the flag to add the summary.
#   --cloud        transcribe via AssemblyAI with speaker diarization instead of
#                  local Whisper. Needs an API key in
#                  ~/.config/council-tracking/assemblyai.key (or $ASSEMBLYAI_API_KEY).
#                  ~$0.15/hr (~$1/meeting). Default is free local Whisper.
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
NO_SUMMARY=0
CLOUD=0
EVENT_ID=""
ASSEMBLYAI_KEYFILE="${COUNCIL_ASSEMBLYAI_KEYFILE:-$HOME/.config/council-tracking/assemblyai.key}"
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
    --no-summary)  NO_SUMMARY=1; shift;;
    --cloud)       CLOUD=1; shift;;
    -h|--help)     grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    -*)            echo "unknown flag: $1" >&2; exit 2;;
    *)             EVENT_ID="$1"; shift;;
  esac
done
[ -n "$EVENT_ID" ] || { echo "usage: $0 <EVENT_ID> [--body <id>] [--force] [--resummarize] [--no-summary] [--cloud]" >&2; exit 2; }

say() { printf '\033[1;32m▸ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---- dependency checks ----
for bin in python3 curl; do
  command -v "$bin" >/dev/null || die "missing dependency: $bin"
done
if [ "$CLOUD" = 0 ]; then
  for bin in ffmpeg whisper-cli; do
    command -v "$bin" >/dev/null || die "missing dependency: $bin (or use --cloud)"
  done
fi
HAVE_CLAUDE=1; command -v claude >/dev/null || HAVE_CLAUDE=0

# In cloud mode, resolve the AssemblyAI key from env or a local keyfile (kept
# out of the repo and out of shell history). Never pass it on the command line.
API_KEY=""
if [ "$CLOUD" = 1 ]; then
  API_KEY="${ASSEMBLYAI_API_KEY:-}"
  if [ -z "$API_KEY" ] && [ -f "$ASSEMBLYAI_KEYFILE" ]; then
    API_KEY="$(tr -d '[:space:]' < "$ASSEMBLYAI_KEYFILE")"
  fi
  [ -n "$API_KEY" ] || die "no AssemblyAI key. Put it in $ASSEMBLYAI_KEYFILE (chmod 600) or export ASSEMBLYAI_API_KEY"
fi

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

# ---- ensure the whisper model is cached (local mode only) ----
if [ "$CLOUD" = 0 ] && [ ! -f "$MODEL" ]; then
  say "Downloading whisper model (~1.6 GB, one time) → $MODEL"
  mkdir -p "$CACHE"
  curl -fL --progress-bar -o "$MODEL" "$MODEL_URL" || die "model download failed"
fi

# ---- 2 + 3. transcribe ----
if [ "$FORCE" = 1 ] || [ ! -f "$TXT" ]; then
  if [ "$CLOUD" = 1 ]; then
    # AssemblyAI reads the CivicClerk recording URL directly (no upload) and
    # returns diarized turns. Writes speaker-labeled .txt plus .srt/.vtt.
    say "Transcribing via AssemblyAI (with speaker diarization) …"
    python3 - "$API_KEY" "$MP4" "$TXT" "$DIR/$STEM.srt" "$DIR/$STEM.vtt" <<'PY'
import json, sys, time, urllib.request, urllib.error
key, audio_url, txt_path, srt_path, vtt_path = sys.argv[1:6]
BASE = "https://api.assemblyai.com/v2"
hdr = {"authorization": key, "content-type": "application/json"}

def call(method, url, data=None, raw=False):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            b = r.read()
            return b if raw else json.loads(b)
    except urllib.error.HTTPError as e:
        sys.exit(f"AssemblyAI HTTP {e.code}: {e.read().decode(errors='ignore')[:300]}")

job = call("POST", f"{BASE}/transcript",
           {"audio_url": audio_url, "speaker_labels": True, "language_code": "en_us"})
tid = job["id"]
print(f"  job {tid} submitted; polling…", flush=True)

t = None
for _ in range(600):                 # up to ~2h wall-clock guard
    time.sleep(12)
    t = call("GET", f"{BASE}/transcript/{tid}")
    st = t.get("status")
    if st == "completed":
        break
    if st == "error":
        sys.exit("AssemblyAI error: " + str(t.get("error")))
    print(f"  status: {st}…", flush=True)
else:
    sys.exit("timed out waiting for AssemblyAI")

utt = t.get("utterances") or []
if utt:
    lines = [f"Speaker {u.get('speaker','?')}: {u.get('text','').strip()}" for u in utt]
else:
    lines = [s.strip() for s in (t.get("text") or "").split(". ") if s.strip()]
open(txt_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")

for path, ext in ((srt_path, "srt"), (vtt_path, "vtt")):
    try:
        open(path, "wb").write(call("GET", f"{BASE}/transcript/{tid}/{ext}", raw=True))
    except SystemExit:
        print(f"  ({ext} export skipped)", flush=True)
print(f"  {len(utt)} speaker turns", flush=True)
PY
    [ -s "$TXT" ] || die "AssemblyAI transcription produced no text"
  else
    say "Extracting audio (streaming from CivicClerk; a long meeting can take a few minutes) …"
    ffmpeg -y -nostdin -loglevel error -i "$MP4" -vn -ac 1 -ar 16000 -c:a pcm_s16le "$WAV" \
      || die "ffmpeg audio extraction failed"

    say "Transcribing on-device (whisper.cpp) …"
    whisper-cli -m "$MODEL" -f "$WAV" -l en -otxt -osrt -ovtt -of "$DIR/$STEM" -pp \
      || die "whisper-cli failed"
    rm -f "$WAV"

    # Collapse runs of identical consecutive lines — whisper hallucinates repeats
    # over long silences (e.g. the closed-session gap). (Cloud diarization doesn't.)
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
  fi
  words=$(wc -w < "$TXT" | tr -d ' ')
  say "Transcript written: $TXT  (${words} words)"
else
  say "Transcript exists — skipping transcription (use --force to redo)."
fi

# ---- 4. enriched summary via the claude CLI ----
if [ "$NO_SUMMARY" = 1 ]; then
  say "Skipping summary (--no-summary). Transcript will still be published; run again"
  echo "   without --no-summary later to add the AI summary."
elif [ "$RESUMMARIZE" = 1 ] || [ ! -f "$SUMMARY" ]; then
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
        [ "$CLOUD" = 1 ] && printf 'NOTE: transcript lines are prefixed with diarized speaker labels (Speaker A, Speaker B, …) that are consistent per speaker but not mapped to names. Use them to follow turn-taking and to attribute discussion, inferring real names from roll-call and context.\n\n'
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
