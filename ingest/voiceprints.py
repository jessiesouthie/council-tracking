"""Voiceprints: turn "Speaker A" into a name from the recordings themselves.

Why this exists
---------------
AssemblyAI's diarization tells us *that* the voice changed, never *who* it is.
Its labels are per-file and anonymous: the mayor is Speaker A one night and
Speaker J the next. Naming them has so far been hand work — reading the
transcript for the chair's name-calls and roll calls and writing a letter->name
map by hand (`<stem>.speakers.json`, and `ingest/attribute_747.py` for the one
meeting where diarization collapsed entirely).

That hand work has produced something valuable: six meetings where a letter is
tied to a real person, and therefore hours of audio for which we know the
speaker. This module turns that into a reusable acoustic model of the people who
show up over and over — the mayor, the five councilmembers, the city manager,
the attorney, the engineers — so a new meeting can be labeled by comparing
voices instead of by re-reading the transcript.

What this claims, and what it does not
--------------------------------------
This is a *different and narrower* claim than the hand attribution it builds on.
`attribute_747.py` says "the chair called this name and a new voice began" — a
textual fact anyone can check against the transcript. This module says "these
waveforms match the waveforms of a person we have already identified", which is
a statistical claim with an error rate. The two must not be confused in the
published page, so every entry this writes carries `"method": "voiceprint"` and
a `basis` line that says plainly it is an acoustic match and quotes the numbers.

The rule the rest of the pipeline follows applies here too: where the evidence
runs out, say nothing. A label is written only when the best match clears an
absolute similarity floor AND beats the runner-up by a margin. Everything else
is left off the map, and the site keeps showing "Speaker A".

How it works
------------
1. Recover timings. The `.txt` carries speaker labels but no times; the `.srt`
   carries times but no labels. Their word streams are the same transcript, so
   walking them together restores a start/end for every labeled turn.
2. Cut audio. Rather than downloading multi-gigabyte recordings, ffmpeg seeks
   into the CivicClerk MP4 over HTTP byte ranges and pulls out a few seconds at
   a time — a 10-second slice out of a 5.5 GB file costs about five seconds.
3. Embed. SpeechBrain's ECAPA-TDNN speaker encoder maps each slice to a
   192-dimension vector where the same voice lands in the same place.
4. Enroll. For each known person, average their slices into one voiceprint,
   discarding outlier slices — diarizers do fold a second voice into a label
   now and then (2026-07-21 Speaker A is the documented case), and a trimmed
   average keeps one bad slice from poisoning a person.
5. Identify. For a new meeting, embed each letter the same way and score it
   against every enrolled voiceprint by cosine similarity.

Commands
--------
    python -m ingest.voiceprints turns    <stem>     # recovered turn timings
    python -m ingest.voiceprints identities          # review the name aliases
    python -m ingest.voiceprints enroll              # build the voiceprint library
    python -m ingest.voiceprints verify              # hold-one-out accuracy
    python -m ingest.voiceprints identify <stem>     # write <stem>.speakers.json

Requirements: torch, torchaudio, speechbrain, soundfile, numpy — heavy, and
deliberately NOT in ingest/requirements.txt so the nightly transcription job and
the rest of the pipeline stay light. Install them in their own environment:

    python3 -m venv .venv-voice
    .venv-voice/bin/python -m pip install torch torchaudio speechbrain soundfile numpy
    .venv-voice/bin/python -m ingest.voiceprints enroll
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPTS = ROOT / "data" / "transcripts"
VOICEPRINTS = ROOT / "data" / "voiceprints"
LIBRARY = VOICEPRINTS / "library.json"
IDENTITIES = VOICEPRINTS / "identities.json"

API_BASE = "https://eaglemountainut.api.civicclerk.com/v1"
ENCODER = "speechbrain/spkrec-ecapa-voxceleb"

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
# Slices shorter than this are too little voice for a stable embedding; ECAPA
# wants a couple of seconds of speech and these are noisy rooms.
MIN_TURN_SEC = 4.0
# Cap on how much of one turn we use. Beyond ~12s a slice adds little and costs
# a proportional amount of download.
MAX_SLICE_SEC = 12.0
# Trimmed off each end of a turn, because the recovered boundaries are only as
# good as the alignment and a diarizer's own edges tend to bleed.
EDGE_TRIM_SEC = 0.4
# How many slices to take per speaker — one number for enrolling, identifying
# and verifying alike. Sampling a speaker differently in enrollment than in
# identification would mean `verify` measures a setup that never actually runs,
# and it would split the slice cache in two: the same speaker cut twice, once
# per limit. One constant keeps the measurement honest and the cache shared.
SLICES_PER_SPEAKER = 14
# A person must reach this many usable slices before they get a voiceprint at
# all — below it the average is a portrait of one sentence, not of a voice.
MIN_SLICES_TO_ENROLL = 6
# Drop the slices least like the rest of their own group, then re-average.
TRIM_FRACTION = 0.25

# Decision rule. Both must hold or nothing is written. These defaults are
# conservative starting points; `verify` measures what they actually buy on this
# corpus and prints the accuracy at the operating point.
MATCH_FLOOR = 0.50      # absolute cosine to the winning voiceprint
MATCH_MARGIN = 0.08     # how far the winner must beat the runner-up


# ---------------------------------------------------------------------------
# Transcript timing recovery
# ---------------------------------------------------------------------------

SPEAKER_RE = re.compile(r"^Speaker ([A-Z]+):\s*(.*)$")
CUE_RE = re.compile(
    r"\n(\d+)\n(\d\d):(\d\d):(\d\d)[,.](\d+)\s*-->\s*"
    r"(\d\d):(\d\d):(\d\d)[,.](\d+)\n(.*?)(?=\n\n|\Z)",
    re.S,
)


@dataclass
class Turn:
    """One labeled stretch of speech, with times recovered from the .srt."""

    speaker: str
    start: float
    end: float
    words: int

    @property
    def duration(self) -> float:
        return self.end - self.start


def _norm_word(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower())


def read_cues(srt_path: Path) -> list[tuple[float, float, str]]:
    """Parse an .srt into (start_seconds, end_seconds, text) tuples."""
    raw = "\n" + srt_path.read_text(encoding="utf-8", errors="ignore")
    cues = []
    for m in CUE_RE.finditer(raw):
        g = m.groups()
        start = int(g[1]) * 3600 + int(g[2]) * 60 + int(g[3]) + int(g[4]) / 1000.0
        end = int(g[5]) * 3600 + int(g[6]) * 60 + int(g[7]) + int(g[8]) / 1000.0
        cues.append((start, end, g[9].replace("\n", " ").strip()))
    return cues


def recover_turns(stem_dir: Path, stem: str) -> list[Turn]:
    """Restore a start/end time for every "Speaker X:" line in the .txt.

    The .txt and the .srt are the same words — AssemblyAI writes the .txt from
    its diarized utterances and the .srt from its cues — but neither file alone
    has both the speaker and the clock. So flatten the .srt into a word stream
    that remembers which cue each word came from, then walk the .txt's turns
    through it in order, taking as many words as each turn claims.

    The two streams are not exactly equal (a few words in tens of thousands
    differ in how they were split), so after each turn we resync: search a small
    window around the expected end for the position whose preceding words best
    match the turn's own tail. Drift stays local instead of accumulating.
    """
    txt_path = stem_dir / f"{stem}.txt"
    srt_path = stem_dir / f"{stem}.srt"
    if not txt_path.exists() or not srt_path.exists():
        return []

    cues = read_cues(srt_path)
    if not cues:
        return []

    # Flat word stream: (normalized word, index of the cue it belongs to).
    stream: list[tuple[str, int]] = []
    for ci, (_s, _e, text) in enumerate(cues):
        for w in text.split():
            nw = _norm_word(w)
            if nw:
                stream.append((nw, ci))

    turns: list[Turn] = []
    pos = 0
    for line in txt_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = SPEAKER_RE.match(line)
        if not m:
            continue
        speaker, said = m.group(1), m.group(2)
        words = [w for w in (_norm_word(x) for x in said.split()) if w]
        if not words:
            continue

        start_pos = pos
        expected = pos + len(words)

        # Resync: try end positions near `expected` and keep the one whose last
        # few stream words best match this turn's last few words.
        tail = words[-4:]
        best_end, best_score = expected, -1
        for cand in range(max(start_pos + 1, expected - 10), min(len(stream), expected + 11)):
            window = [w for w, _ in stream[max(0, cand - len(tail)):cand]]
            score = sum(1 for a, b in zip(window, tail) if a == b)
            if score > best_score:
                best_score, best_end = score, cand
        end_pos = min(best_end, len(stream))
        if end_pos <= start_pos:
            end_pos = min(start_pos + len(words), len(stream))

        if start_pos < len(stream):
            first_cue = stream[start_pos][1]
            last_cue = stream[min(end_pos, len(stream)) - 1][1]
            turns.append(
                Turn(
                    speaker=speaker,
                    start=cues[first_cue][0],
                    end=cues[last_cue][1],
                    words=len(words),
                )
            )
        pos = end_pos
        if pos >= len(stream):
            break

    return turns


# ---------------------------------------------------------------------------
# Audio access
# ---------------------------------------------------------------------------


def resolve_media(event_id: str) -> str:
    """Ask CivicClerk for a playable URL, the way transcribe_meeting.sh does.

    Newer events return a storage-relative path rather than a link, and only the
    media-summary endpoint knows how to turn it into something ffmpeg can open.
    """
    with urllib.request.urlopen(
        f"{API_BASE}/Events?$filter=id%20eq%20{event_id}", timeout=60
    ) as r:
        events = json.loads(r.read()).get("value") or []
    if not events:
        raise SystemExit(f"event {event_id} not found on the portal")
    e = events[0]
    mp4 = e.get("mediaSourcePathMp4") or e.get("mediaStreamPath") or ""
    if mp4.startswith("http"):
        return mp4
    with urllib.request.urlopen(
        f"{API_BASE}/EventsMedia/GetEventMediaSummary(eventId={event_id})", timeout=60
    ) as r:
        url = json.loads(r.read()).get("videoUrl")
    if not url:
        raise SystemExit(f"event {event_id} has no playable recording")
    return url


def cut(source: str, start: float, duration: float, out: Path) -> bool:
    """Extract one mono 16 kHz slice with ffmpeg.

    `-ss` goes before `-i` on purpose: that makes ffmpeg seek before it starts
    decoding, which over HTTP becomes a byte-range request instead of a read of
    everything up to that point. It is the whole reason enrollment can sample
    across a five-hour meeting without downloading it.
    """
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
        "-ss", f"{start:.2f}", "-i", source, "-t", f"{duration:.2f}",
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return out.exists() and out.stat().st_size > 8000  # ~0.25s of audio


def pick_slices(turns: list[Turn], limit: int) -> list[tuple[float, float]]:
    """Choose which turns to sample for one speaker.

    Longest-first would take every slice from whichever agenda item this person
    dominated, so their voiceprint would encode that stretch of room noise as
    much as their voice. Instead, split the meeting into as many buckets as we
    want slices and take the longest usable turn from each, which spreads the
    sample across the night before it falls back to length alone.
    """
    usable = [t for t in turns if t.duration >= MIN_TURN_SEC]
    if not usable:
        return []
    usable.sort(key=lambda t: t.start)
    span_start, span_end = usable[0].start, usable[-1].end
    span = max(span_end - span_start, 1.0)

    buckets: dict[int, Turn] = {}
    for t in usable:
        b = min(limit - 1, int((t.start - span_start) / span * limit))
        if b not in buckets or t.duration > buckets[b].duration:
            buckets[b] = t

    chosen = list(buckets.values())
    if len(chosen) < limit:  # top up with the longest turns not already taken
        rest = sorted(
            (t for t in usable if t not in chosen),
            key=lambda t: t.duration,
            reverse=True,
        )
        chosen.extend(rest[: limit - len(chosen)])

    out = []
    for t in chosen[:limit]:
        start = t.start + EDGE_TRIM_SEC
        dur = min(t.duration - 2 * EDGE_TRIM_SEC, MAX_SLICE_SEC)
        if dur >= MIN_TURN_SEC - 2 * EDGE_TRIM_SEC:
            out.append((start, dur))
    return out


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

_encoder = None


def encoder():
    """Load ECAPA-TDNN once. Imported lazily so the light commands stay light."""
    global _encoder
    if _encoder is None:
        from speechbrain.inference.speaker import EncoderClassifier

        _encoder = EncoderClassifier.from_hparams(
            source=ENCODER,
            savedir=str(VOICEPRINTS / ".model"),
            run_opts={"device": "cpu"},
        )
    return _encoder


def embed(paths: list[Path]):
    """Embed each wav to a unit-length 192-d vector. Returns (vectors, kept)."""
    import numpy as np
    import soundfile as sf
    import torch

    vecs, kept = [], []
    enc = encoder()
    for p in paths:
        try:
            wav, sr = sf.read(str(p), dtype="float32")
        except Exception:
            continue
        if sr != 16000 or wav.size < 16000:
            continue
        with torch.no_grad():
            v = enc.encode_batch(torch.from_numpy(wav).unsqueeze(0))
        v = v.squeeze().cpu().numpy().astype("float64")
        n = np.linalg.norm(v)
        if n == 0:
            continue
        vecs.append(v / n)
        kept.append(p)
    return (np.vstack(vecs) if vecs else np.zeros((0, 192))), kept


def trimmed_centroid(vectors):
    """Average the vectors, after dropping the ones least like the group.

    Diarization is not clean: 2026-07-21's own speaker map records that the
    recognizer folded a mortgage broker's remarks into the mayor pro tem's
    label. One such slice can pull a centroid far enough to cost a match, so
    take a first average, score every slice against it, drop the worst quarter,
    and average what is left.
    """
    import numpy as np

    if len(vectors) == 0:
        return None
    if len(vectors) <= 3:
        c = vectors.mean(axis=0)
        return c / max(np.linalg.norm(c), 1e-9)

    c = vectors.mean(axis=0)
    c /= max(np.linalg.norm(c), 1e-9)
    sims = vectors @ c
    keep_n = max(3, int(round(len(vectors) * (1 - TRIM_FRACTION))))
    keep = np.argsort(-sims)[:keep_n]
    c2 = vectors[keep].mean(axis=0)
    return c2 / max(np.linalg.norm(c2), 1e-9)


# ---------------------------------------------------------------------------
# Identities: the label strings in speakers.json -> one canonical person
# ---------------------------------------------------------------------------

TITLE_RE = re.compile(
    r"^(mayor pro tem|mayor|councilmember|council member|chief deputy|deputy|"
    r"chief|mr\.|mrs\.|ms\.|dr\.|miss)\s+",
    re.I,
)


def canonical(label: str) -> tuple[str, str]:
    """Split a speakers.json name into (bare name, role note).

    "Councilmember Melissa Clark (mayor pro tem)" -> ("Melissa Clark", "mayor pro tem")
    """
    label = label.strip()
    role = ""
    m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", label)
    if m:
        label, role = m.group(1).strip(), m.group(2).strip()
    prev = None
    while prev != label:
        prev = label
        label = TITLE_RE.sub("", label).strip()
    label = re.sub(r'\s*["“”][^"“”]*["“”]\s*', " ", label)
    return re.sub(r"\s+", " ", label).strip(), role


def labeled_meetings(body: str = "city-council") -> list[tuple[str, dict]]:
    """Every meeting that has a hand-written letter->name map."""
    d = TRANSCRIPTS / body
    out = []
    for f in sorted(d.glob("*.speakers.json")):
        stem = f.name[: -len(".speakers.json")]
        try:
            out.append((stem, json.loads(f.read_text(encoding="utf-8"))))
        except json.JSONDecodeError:
            print(f"  skip (bad JSON): {f.name}")
    return out


def build_identities(body: str = "city-council") -> dict:
    """Group the hand-written labels into people, and flag what needs a human.

    Keyed on the exact label string as it appears in the speakers maps, because
    the role in parentheses is often the only thing separating two people. The
    corpus contains the case that proves it: "Brad (Parks / Public Works)" and
    "Brad Patterson (bond counsel, Gilmore …)" are two different men, and an
    earlier version of this function that keyed on the bare first name merged
    them into one voiceprint. Any rule clever enough to split those is clever
    enough to join something it shouldn't.

    So: a label whose name has two or more words is taken at face value —
    "Ben Reeves (City Manager)" is Ben Reeves. A label with only a first name is
    never assigned automatically, however obvious it looks. It is written into
    identities.json with `"person": null` and a note listing the full names it
    could plausibly be, for a person to resolve once. Until someone does, those
    turns are simply not enrolled, which costs a little coverage and cannot
    invent a speaker.
    """
    seen: dict[str, list[str]] = {}
    for stem, mapping in labeled_meetings(body):
        for letter, info in mapping.items():
            label = (info.get("name") or "").strip()
            if not label:
                continue
            seen.setdefault(label, []).append(f"{stem}:{letter}")

    # Every full name anywhere in the corpus, indexed by first name, so a
    # single-name label can at least be told what it might be.
    full = set()
    for label in seen:
        name, _role = canonical(label)
        if len(name.split()) >= 2:
            full.add(name)
    first_index: dict[str, list[str]] = {}
    for n in full:
        first_index.setdefault(n.split()[0].lower(), []).append(n)

    identities: dict[str, dict] = {}
    for label, occurrences in sorted(seen.items()):
        name, role = canonical(label)
        entry: dict = {"occurrences": sorted(occurrences)}
        if len(name.split()) >= 2:
            entry["person"] = name
        else:
            candidates = sorted(first_index.get(name.lower(), []))
            entry["person"] = None
            entry["note"] = (
                "only a first name in the transcript"
                + (f"; could be {', '.join(candidates)}" if candidates else "")
                + " — set \"person\" to enroll these turns, or leave null to skip them"
            )
        identities[label] = entry
    return identities


def load_identities(body: str = "city-council") -> dict:
    """Read identities.json, creating it from the corpus on first run."""
    if IDENTITIES.exists():
        stored = json.loads(IDENTITIES.read_text(encoding="utf-8"))
        fresh = build_identities(body)
        # Keep any hand-set "person"; pick up labels added since the file was written.
        for name, entry in fresh.items():
            if name in stored:
                entry["person"] = stored[name].get("person")
                if stored[name].get("note"):
                    entry["note"] = stored[name]["note"]
            stored[name] = entry
        return stored
    VOICEPRINTS.mkdir(parents=True, exist_ok=True)
    fresh = build_identities(body)
    IDENTITIES.write_text(json.dumps(fresh, indent=2) + "\n", encoding="utf-8")
    return fresh


# ---------------------------------------------------------------------------
# Sampling a meeting
# ---------------------------------------------------------------------------


def sample_meeting(
    stem: str,
    body: str,
    limit: int,
    source: str | None = None,
    jobs: int = 6,
    only: set[str] | None = None,
    cache: bool = True,
) -> dict[str, "object"]:
    """Cut and embed up to `limit` slices for every speaker letter in a meeting.

    Returns {letter: (vectors, [(start, duration), ...])}.

    The cuts run in a thread pool because each one is a separate ffmpeg that
    spends nearly all its time waiting on an HTTP range request, not on the CPU.

    Embeddings are cached per meeting under data/voiceprints/cache/ and flushed
    as each speaker finishes, because this is the slow, network-bound half of
    the job — a full enrollment is well over a thousand ffmpeg seeks into
    multi-gigabyte recordings. Without the cache, a run stopped near the end
    throws away everything it did; with it, the work already done is kept and a
    re-run only cuts what is missing. The cache key records the slicing
    parameters, so changing how slices are chosen invalidates it rather than
    silently mixing two sampling schemes in one voiceprint.
    """
    import numpy as np

    stem_dir = TRANSCRIPTS / body
    event_id = stem.split("__")[-1]
    turns = recover_turns(stem_dir, stem)
    if not turns:
        print(f"  {stem}: no diarized turns (not a cloud transcript?) — skipped")
        return {}

    by_speaker: dict[str, list[Turn]] = {}
    for t in turns:
        by_speaker.setdefault(t.speaker, []).append(t)
    if only is not None:
        by_speaker = {k: v for k, v in by_speaker.items() if k in only}
    if not by_speaker:
        return {}

    cache_file = VOICEPRINTS / "cache" / f"{stem}.json"
    ckey = (f"limit={limit};min={MIN_TURN_SEC};max={MAX_SLICE_SEC};"
            f"trim={EDGE_TRIM_SEC};encoder={ENCODER}")
    store: dict = {"key": ckey, "speakers": {}}
    if cache and cache_file.exists():
        try:
            loaded = json.loads(cache_file.read_text(encoding="utf-8"))
            if loaded.get("key") == ckey:
                store = loaded
        except json.JSONDecodeError:
            pass
    have: dict = store["speakers"]

    def flush():
        if not cache:
            return
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(store), encoding="utf-8")

    needed = [l for l in sorted(by_speaker) if l not in have]
    reused = [l for l in sorted(by_speaker) if l in have]

    if needed:
        src = source or resolve_media(event_id)
        plan: list[tuple[str, int, float, float]] = []
        for letter in needed:
            for i, (start, dur) in enumerate(pick_slices(by_speaker[letter], limit)):
                plan.append((letter, i, start, dur))
        if not plan:
            print(f"  {stem}: no turns long enough to sample")
        else:
            note = f" ({len(reused)} speaker(s) already cached)" if reused else ""
            print(f"  {stem}: cutting {len(plan)} slices from {len(needed)} "
                  f"speakers{note} …", flush=True)
            tmp = Path(tempfile.mkdtemp(prefix="voiceprints-"))
            try:
                def work(job):
                    letter, i, start, dur = job
                    out = tmp / f"{letter}_{i:03d}.wav"
                    return (letter, start, dur, out) if cut(src, start, dur, out) else None

                with ThreadPoolExecutor(max_workers=jobs) as pool:
                    cuts = [c for c in pool.map(work, plan) if c]

                done = 0
                for letter in needed:
                    mine = [c for c in cuts if c[0] == letter]
                    if not mine:
                        continue
                    vecs, kept = embed([c[3] for c in mine])
                    keptset = set(kept)
                    spans = [(c[1], c[2]) for c in mine if c[3] in keptset]
                    if len(vecs):
                        have[letter] = {
                            "vectors": [[round(float(x), 6) for x in v] for v in vecs],
                            "spans": [[round(a, 2), round(b, 2)] for a, b in spans],
                        }
                        done += len(vecs)
                        # Flush per speaker: a run killed here keeps what it has.
                        flush()
                unusable = len(plan) - done
                print(f"    embedded {done} slices"
                      + (f" ({unusable} unusable)" if unusable > 0 else ""), flush=True)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    elif reused:
        print(f"  {stem}: all {len(reused)} speaker(s) already cached", flush=True)

    out: dict[str, object] = {}
    for letter in sorted(by_speaker):
        e = have.get(letter)
        if not e:
            continue
        out[letter] = (
            np.array(e["vectors"], dtype="float64"),
            [tuple(sp) for sp in e.get("spans", [])],
        )
    return out


# ---------------------------------------------------------------------------
# enroll
# ---------------------------------------------------------------------------


def cmd_enroll(args) -> int:
    """Build the voiceprint library from every hand-labeled meeting."""
    import numpy as np

    identities = load_identities(args.body)
    meetings = labeled_meetings(args.body)
    if not meetings:
        print("no hand-labeled meetings found — nothing to enroll from")
        return 1

    print(f"Enrolling from {len(meetings)} labeled meeting(s).")
    # person -> list of (stem, vectors)
    collected: dict[str, list[tuple[str, object]]] = {}

    for stem, mapping in meetings:
        if args.only_meeting and stem not in args.only_meeting:
            continue
        # Which letters in this meeting resolve to a person we can name?
        wanted: dict[str, str] = {}
        for letter, info in mapping.items():
            label = (info.get("name") or "").strip()
            person = (identities.get(label) or {}).get("person")
            if person:
                wanted[letter] = person
        if not wanted:
            continue
        sampled = sample_meeting(stem, args.body, SLICES_PER_SPEAKER,
                                jobs=args.jobs, only=set(wanted))
        for letter, (vecs, _spans) in sampled.items():
            collected.setdefault(wanted[letter], []).append((stem, vecs))

    VOICEPRINTS.mkdir(parents=True, exist_ok=True)
    library = {"encoder": ENCODER, "people": {}}
    skipped = []
    for person, chunks in sorted(collected.items()):
        vecs = np.vstack([v for _s, v in chunks])
        if len(vecs) < MIN_SLICES_TO_ENROLL:
            skipped.append((person, len(vecs), sorted({s for s, _ in chunks})))
            continue
        c = trimmed_centroid(vecs)
        library["people"][person] = {
            "centroid": [round(float(x), 6) for x in c],
            "slices": int(len(vecs)),
            "meetings": sorted({s for s, _ in chunks}),
            # Per-meeting centroids are what `verify` holds out, and they also
            # show whether a person sounds consistent from night to night.
            "per_meeting": {
                s: [round(float(x), 6) for x in trimmed_centroid(v)]
                for s, v in chunks
                if len(v) >= 3
            },
        }
    LIBRARY.write_text(json.dumps(library, indent=1) + "\n", encoding="utf-8")

    print(f"\nEnrolled {len(library['people'])} people → {LIBRARY.relative_to(ROOT)}")
    for person, e in sorted(library["people"].items()):
        print(f"  {person:34} {e['slices']:3d} slices  across {len(e['meetings'])} meeting(s)")
    if skipped:
        print(f"\nToo little audio to enroll (under {MIN_SLICES_TO_ENROLL} slices):")
        for person, n, stems in skipped:
            print(f"  {person:34} {n:3d} slices  ({', '.join(stems)})")
    unresolved = [n for n, e in identities.items() if not e.get("person")]
    if unresolved:
        print(f"\n{len(unresolved)} label(s) left unassigned in "
              f"{IDENTITIES.relative_to(ROOT)} — set \"person\" to include them:")
        for n in unresolved:
            print(f"  {n}")
    return 0


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def load_library() -> dict:
    if not LIBRARY.exists():
        raise SystemExit(
            f"no voiceprint library at {LIBRARY.relative_to(ROOT)} — run "
            "`python -m ingest.voiceprints enroll` first"
        )
    return json.loads(LIBRARY.read_text(encoding="utf-8"))


def score(vectors, library: dict, exclude_meeting: str | None = None):
    """Score one speaker's slices against every enrolled person.

    Returns [(person, similarity), ...] best first. The score is the cosine
    between this speaker's trimmed centroid and the person's, so a single odd
    slice on either side cannot decide a name on its own.

    `exclude_meeting` drops that meeting's contribution from each voiceprint,
    which is what makes `verify` an honest test rather than a lookup of an
    answer the library already contains.
    """
    import numpy as np

    c = trimmed_centroid(vectors)
    if c is None:
        return []
    out = []
    for person, e in library["people"].items():
        if exclude_meeting:
            others = {k: v for k, v in e.get("per_meeting", {}).items()
                      if k != exclude_meeting}
            if not others:
                continue
            ref = np.array(list(others.values()), dtype="float64").mean(axis=0)
            ref /= max(np.linalg.norm(ref), 1e-9)
        else:
            ref = np.array(e["centroid"], dtype="float64")
        out.append((person, float(c @ ref)))
    out.sort(key=lambda x: -x[1])
    return out


def decide(ranked, floor: float, margin: float):
    """Apply the two-part rule. Returns (person or None, best, runner_up)."""
    if not ranked:
        return None, 0.0, 0.0
    best_person, best = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    if best >= floor and (best - second) >= margin:
        return best_person, best, second
    return None, best, second


# ---------------------------------------------------------------------------
# verify — hold-one-out, the only honest measure of whether this works
# ---------------------------------------------------------------------------


def cmd_verify(args) -> int:
    """Re-identify every hand-labeled speaker using voiceprints built without
    that speaker's own meeting, and report how often the answer is right.

    This is the number that decides whether the thresholds are set sensibly. A
    match scored against a voiceprint that includes the very audio being matched
    would be near-perfect and would mean nothing.
    """
    library = load_library()
    identities = load_identities(args.body)
    trials = []
    for stem, mapping in labeled_meetings(args.body):
        truth = {}
        for letter, info in mapping.items():
            label = (info.get("name") or "").strip()
            person = (identities.get(label) or {}).get("person")
            if person and person in library["people"]:
                truth[letter] = person
        if not truth:
            continue

        sampled = sample_meeting(stem, args.body, SLICES_PER_SPEAKER,
                                 jobs=args.jobs, only=set(truth),
                                 cache=not args.refresh)

        for letter, person in sorted(truth.items()):
            if letter not in sampled:
                continue
            ranked = score(sampled[letter][0], library, exclude_meeting=stem)
            if not ranked:
                continue
            guess, best, second = decide(ranked, args.floor, args.margin)
            trials.append({
                "stem": stem, "letter": letter, "truth": person,
                "top": ranked[0][0], "best": best, "second": second,
                "guess": guess,
                # Could this trial have been got right at all? A person enrolled
                # from one meeting has their only voiceprint removed when that
                # meeting is held out, so there is no right answer left to find.
                # Counting those against recall would understate the method; not
                # separating them would overstate what it can do on a stranger.
                "answerable": any(p == person for p, _ in ranked),
            })

    if not trials:
        print("no trials — enroll first, or the library has nobody in it")
        return 1

    named = [t for t in trials if t["guess"]]
    correct = [t for t in named if t["guess"] == t["truth"]]
    wrong = [t for t in named if t["guess"] != t["truth"]]
    silent = [t for t in trials if not t["guess"]]
    top1 = [t for t in trials if t["top"] == t["truth"]]

    print(f"\nHold-one-out over {len(trials)} labeled speakers "
          f"(floor {args.floor}, margin {args.margin})")
    print(f"  closest voiceprint is the right person : {len(top1)}/{len(trials)}"
          f"  ({100 * len(top1) / len(trials):.0f}%)")
    print(f"  labeled by the rule                    : {len(named)}"
          f"  ({100 * len(named) / len(trials):.0f}% of speakers)")
    if named:
        print(f"    of those, correct                    : {len(correct)}/{len(named)}"
              f"  ({100 * len(correct) / len(named):.0f}% precision)")
    print(f"  left unlabeled by the rule             : {len(silent)}")

    # Split out the trials that were impossible by construction, so the coverage
    # number means something. A speaker whose person was only ever labeled in
    # this one meeting has no voiceprint left once it is held out; the honest
    # question is how often the method finds a person it has actually met.
    answerable = [t for t in trials if t["answerable"]]
    impossible = [t for t in trials if not t["answerable"]]
    if answerable:
        found = [t for t in answerable if t["guess"] == t["truth"]]
        print(f"\n  Of the {len(answerable)} speakers whose person is enrolled from some")
        print(f"  OTHER meeting — the only ones findable at all:")
        print(f"    correctly named                      : {len(found)}/{len(answerable)}"
              f"  ({100 * len(found) / len(answerable):.0f}% recall)")
        missed = [t for t in answerable if not t["guess"]]
        if missed:
            print(f"    left unlabeled (cautious, not wrong) : {len(missed)}")
            for t in missed:
                print(f"      {t['stem']} {t['letter']}: {t['truth']} "
                      f"(best {t['best']:.3f}, next {t['second']:.3f})")
    if impossible:
        print(f"\n  {len(impossible)} speaker(s) appear in only one labeled meeting, so")
        print(f"  holding it out leaves nothing to match. The rule declined "
              f"{sum(1 for t in impossible if not t['guess'])} of them"
              f" and misnamed {sum(1 for t in impossible if t['guess'])}.")

    if wrong:
        print("\n  WRONG (these are what the thresholds exist to prevent):")
        for t in wrong:
            print(f"    {t['stem']} {t['letter']}: said {t['guess']} ({t['best']:.3f}), "
                  f"actually {t['truth']}")

    if args.sweep:
        print("\n  Operating points (floor / margin -> labeled, precision):")
        for floor in (0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
            for margin in (0.00, 0.05, 0.08, 0.12):
                n = [t for t in trials
                     if t["best"] >= floor and (t["best"] - t["second"]) >= margin]
                ok = [t for t in n if t["top"] == t["truth"]]
                if n:
                    print(f"    {floor:.2f} / {margin:.2f}  ->  {len(n):3d} labeled, "
                          f"{100 * len(ok) / len(n):5.1f}% correct")
    return 0


# ---------------------------------------------------------------------------
# identify — the point of the whole thing
# ---------------------------------------------------------------------------


def cmd_identify(args) -> int:
    library = load_library()
    stem = args.stem
    stem_dir = TRANSCRIPTS / args.body
    out_path = stem_dir / f"{stem}.speakers.json"

    if out_path.exists() and not (args.force or args.report):
        print(f"{out_path.relative_to(ROOT)} already exists — it may be hand-written.\n"
              f"Re-run with --report to see what the voiceprints would say, or "
              f"--force to overwrite it.")
        return 1

    sampled = sample_meeting(stem, args.body, SLICES_PER_SPEAKER,
                             source=args.audio, jobs=args.jobs)
    if not sampled:
        return 1

    speakers: dict[str, dict] = {}
    unmatched: list[tuple[str, str, float, float]] = []
    for letter, (vecs, spans) in sorted(sampled.items()):
        ranked = score(vecs, library)
        person, best, second = decide(ranked, args.floor, args.margin)
        runner = ranked[1][0] if len(ranked) > 1 else "nobody"
        if not person:
            unmatched.append((letter, ranked[0][0] if ranked else "-", best, second))
            continue
        entry = library["people"][person]
        n_meet = len(entry["meetings"])
        speakers[letter] = {
            "name": person,
            "basis": (
                f"Voice match. This label's audio scores {best:.2f} cosine similarity "
                f"against the voiceprint for {person}, built from {entry['slices']} "
                f"sampled turns across "
                + (f"{n_meet} other meetings" if n_meet != 1 else "one other meeting")
                + f", versus {second:.2f} for the next-closest person, {runner}. "
                f"Sampled from {len(spans)} "
                + ("turns" if len(spans) != 1 else "turn")
                + " spread through this meeting. This is an acoustic match only — "
                f"nothing said in the transcript was read to reach it."
            ),
            "method": "voiceprint",
            "confidence": round(best, 3),
            "runner_up": {"name": runner, "similarity": round(second, 3)},
        }

    print(f"\n{stem}: named {len(speakers)} of {len(sampled)} speaker labels")
    for letter, e in sorted(speakers.items()):
        print(f"  Speaker {letter:2} -> {e['name']:32} ({e['confidence']:.3f}, "
              f"next {e['runner_up']['similarity']:.3f})")
    if unmatched:
        print(f"  left unlabeled ({len(unmatched)}) — below floor {args.floor} "
              f"or margin {args.margin}:")
        for letter, top, best, second in unmatched:
            print(f"    Speaker {letter:2}    closest was {top} at {best:.3f} "
                  f"(next {second:.3f})")

    if args.report:
        print("\n(--report: nothing written)")
        return 0
    if not speakers:
        print("\nNothing cleared the threshold — no file written, which is the "
              "right outcome rather than a map of guesses.")
        return 0
    out_path.write_text(json.dumps(speakers, indent=1) + "\n", encoding="utf-8")
    print(f"\nwrote {out_path.relative_to(ROOT)}")
    print("Publish it with:  python3 -m ingest.build_transcripts")
    return 0


# ---------------------------------------------------------------------------
# small read-only commands
# ---------------------------------------------------------------------------


def cmd_audit(args) -> int:
    """Check the hand-written speaker maps against the voices in the recordings.

    Two things are worth knowing and neither is visible by reading a transcript:

    * A person labeled in two meetings whose two voices do not match. One of the
      two labels is wrong, or a diarizer folded a second person into one of them.
    * Two different people whose voices do match. Usually the same person under
      two names — a first name in one meeting and a full name in another — but
      sometimes a genuine mislabel.

    The thresholds come from this corpus rather than from a paper: matched pairs
    of the same person across meetings and every mismatched pair of different
    people are both printed at the end, so the gap they sit in is visible and a
    reader can judge whether the flags are set in a sensible place.

    This audits the human work with the machine, which is the opposite direction
    from the rest of this module, and it is the direction with the better record:
    the voices cannot be talked into agreeing with the label.
    """
    import numpy as np

    library = load_library()
    people = library["people"]

    same_pairs, diff_pairs = [], []
    for person, e in people.items():
        pm = {k: np.array(v, dtype="float64") for k, v in e.get("per_meeting", {}).items()}
        ks = sorted(pm)
        for i in range(len(ks)):
            for j in range(i + 1, len(ks)):
                same_pairs.append((float(pm[ks[i]] @ pm[ks[j]]), person, ks[i], ks[j]))
    names = sorted(people)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            pa = {k: np.array(v, dtype="float64")
                  for k, v in people[a].get("per_meeting", {}).items()}
            pb = {k: np.array(v, dtype="float64")
                  for k, v in people[b].get("per_meeting", {}).items()}
            best = None
            for ka, va in pa.items():
                for kb, vb in pb.items():
                    sim = float(va @ vb)
                    if best is None or sim > best[0]:
                        best = (sim, ka, kb)
            if best:
                diff_pairs.append((best[0], a, b, best[1], best[2]))

    contradictions = sorted(p for p in same_pairs if p[0] < args.split_below)
    collisions = sorted((p for p in diff_pairs if p[0] > args.merge_above), reverse=True)

    print("Auditing the hand-written speaker maps against the recordings.\n")
    if contradictions:
        print(f"Called one person, but the voices differ (under {args.split_below}):")
        for sim, person, m1, m2 in contradictions:
            print(f"  {sim:.3f}  {person}")
            print(f"          {m1}  vs  {m2}")
            print( "          -> one of these two labels is wrong, or a diarizer folded")
            print( "             a second voice into one of them. This person's voiceprint")
            print( "             is an average of two people until it is resolved.")
    else:
        print("No contradictions: every person labeled in two or more meetings")
        print("sounds like themselves each time.")

    print()
    if collisions:
        print(f"Called two people, but the voices match (over {args.merge_above}):")
        for sim, a, b, m1, m2 in collisions:
            print(f"  {sim:.3f}  {a}  ==  {b}")
            print(f"          {m1}  vs  {m2}")
    else:
        print(f"No collisions: no two enrolled people sound alike above "
              f"{args.merge_above}.")

    if same_pairs and diff_pairs:
        sp = np.array([p[0] for p in same_pairs])
        dp = np.array([p[0] for p in diff_pairs])
        print(f"\nWhere the thresholds sit on this corpus:")
        print(f"  same person, two meetings (n={len(sp)}):  "
              f"mean {sp.mean():.3f}  5th pct {np.percentile(sp, 5):.3f}  min {sp.min():.3f}")
        print(f"  two different people      (n={len(dp)}):  "
              f"mean {dp.mean():.3f}  95th pct {np.percentile(dp, 95):.3f}  max {dp.max():.3f}")
    return 0 if not (contradictions or collisions) else 2


def cmd_turns(args) -> int:
    turns = recover_turns(TRANSCRIPTS / args.body, args.stem)
    if not turns:
        print("no diarized turns found")
        return 1
    by: dict[str, list[Turn]] = {}
    for t in turns:
        by.setdefault(t.speaker, []).append(t)
    total = sum(t.duration for t in turns)
    print(f"{args.stem}: {len(turns)} turns, {len(by)} speakers, "
          f"{total / 60:.0f} min of labeled speech")
    print(f"{'spk':>4} {'turns':>6} {'usable':>7} {'speech':>9}  sample windows")
    for letter, ts in sorted(by.items(), key=lambda kv: -sum(t.duration for t in kv[1])):
        usable = [t for t in ts if t.duration >= MIN_TURN_SEC]
        secs = sum(t.duration for t in ts)
        picks = pick_slices(ts, SLICES_PER_SPEAKER)
        print(f"{letter:>4} {len(ts):>6} {len(usable):>7} {secs / 60:>7.1f}m  "
              f"{len(picks)} slices")
    return 0


def cmd_identities(args) -> int:
    identities = load_identities(args.body)
    VOICEPRINTS.mkdir(parents=True, exist_ok=True)
    IDENTITIES.write_text(json.dumps(identities, indent=2) + "\n", encoding="utf-8")
    resolved = {n: e for n, e in identities.items() if e.get("person")}
    open_ = {n: e for n, e in identities.items() if not e.get("person")}
    people: dict[str, list[str]] = {}
    for name, e in resolved.items():
        people.setdefault(e["person"], []).extend(e["occurrences"])
    print(f"{len(people)} people across {len(identities)} distinct labels "
          f"→ {IDENTITIES.relative_to(ROOT)}\n")
    for person, occ in sorted(people.items(), key=lambda kv: -len(kv[1])):
        print(f"  {person:34} {len(occ)} appearance(s)")
    if open_:
        print(f"\n  unassigned ({len(open_)}) — edit \"person\" in "
              f"{IDENTITIES.name} to enroll these:")
        for n, e in sorted(open_.items()):
            print(f"    {n:32} {e.get('note', '')}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m ingest.voiceprints",
        description="Name diarized speakers by matching voices against people "
                    "already identified by hand in earlier meetings.",
    )
    p.add_argument("--body", default="city-council")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("turns", help="show recovered turn timings for one meeting")
    t.add_argument("stem")
    t.set_defaults(func=cmd_turns)

    i = sub.add_parser("identities", help="review how labels group into people")
    i.set_defaults(func=cmd_identities)

    a = sub.add_parser("audit", help="check the hand-written maps against the voices")
    a.add_argument("--split-below", type=float, default=0.50,
                   help="flag one person whose two meetings sound less alike than this")
    a.add_argument("--merge-above", type=float, default=0.65,
                   help="flag two people who sound more alike than this")
    a.set_defaults(func=cmd_audit)

    e = sub.add_parser("enroll", help="build the voiceprint library")
    e.add_argument("--jobs", type=int, default=6)
    e.add_argument("--only-meeting", nargs="*", default=None)
    e.set_defaults(func=cmd_enroll)

    v = sub.add_parser("verify", help="hold-one-out accuracy of the library")
    v.add_argument("--jobs", type=int, default=6)
    v.add_argument("--floor", type=float, default=MATCH_FLOOR)
    v.add_argument("--margin", type=float, default=MATCH_MARGIN)
    v.add_argument("--sweep", action="store_true", help="show other thresholds")
    v.add_argument("--refresh", action="store_true", help="ignore the sample cache")
    v.set_defaults(func=cmd_verify)

    d = sub.add_parser("identify", help="write <stem>.speakers.json for a meeting")
    d.add_argument("stem")
    d.add_argument("--jobs", type=int, default=6)
    d.add_argument("--floor", type=float, default=MATCH_FLOOR)
    d.add_argument("--margin", type=float, default=MATCH_MARGIN)
    d.add_argument("--audio", help="local media file instead of the portal recording")
    d.add_argument("--report", action="store_true", help="print, write nothing")
    d.add_argument("--force", action="store_true", help="overwrite an existing map")
    d.set_defaults(func=cmd_identify)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
