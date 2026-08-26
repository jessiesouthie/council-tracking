"""Attribute speakers in the 2026-08-06 Truth-in-Taxation hearing (event 747).

Why this exists instead of a re-transcription
---------------------------------------------
AssemblyAI's diarization collapsed on this recording — 4h41m, ~65 speakers — and
returned a single utterance, so the .txt was rebuilt one line per .srt cue with no
labels at all. Re-running it costs several GB of download, bills the API again and
will probably collapse the same way; a retry that half-works is worse still,
because 26 letters spread over 65 people invents attribution the audio can't
support.

The recording does not need acoustic diarization to be attributed, because this
particular meeting labels its own speakers out loud:

  * during public comment the chair reads each name off the sign-in list before
    that person speaks ("Betty Peralta." → "Good evening. My name is Betty
    Peralta…"), and most speakers then introduce themselves as well;
  * during the council responses the chair hands off by name ("Councilmember
    Wright, go ahead.");
  * the roll call names every member individually.

So attribution here is textual and auditable: every label traces to a phrase in
the transcript, recorded as `basis` below. That is a different and *narrower*
claim than diarization — it says "the chair called this name and a new voice
began", not "these waveforms match". Where the recording gives no such handle —
the open cross-talk after 03:20 — the lines are deliberately left unlabeled
rather than guessed, which is the same rule the meeting summaries follow.

Output is one `Name: text` line per cue, the shape `build_meeting_pages.py`
already renders (`SPEAKER_LINE` accepts real names, not just "Speaker A").
`diarized` stays false in the index, because it is: these are not turn labels
from the recognizer.

Run:  python3 -m ingest.attribute_747          # rewrite the .txt
      python3 -m ingest.attribute_747 --report # coverage only, write nothing
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STEM = ROOT / "data" / "transcripts" / "city-council" / "2026-08-06__747"

CHAIR = "Mayor Jared Gray"

# ---------------------------------------------------------------------------
# The anchor table
# ---------------------------------------------------------------------------
# Each row is (first cue of the turn, speaker, basis). Detected by scanning for
# the chair's name-calls and hand-checked one by one against the two cues that
# follow — which is why absent speakers, whose written comments the chair reads
# aloud, are not in this table: those words are the author's but the voice is
# the chair's, and this file labels voices.

PUBLIC_COMMENT: list[tuple[int, str, str]] = [
    (135, "Melissa Bradley", "called from the list at 134; speaks immediately"),
    (191, "Melissa Bradley", "reads her husband's statement — 'my wife Melissa will read the statement'; the words are Adam Bradley's"),
    (244, "Duncan Searcy", "called at 243; answers the no-clapping ruling in his own voice"),
    (297, "Charles Peterson", "called at 296; 'as a former federal government program analyst'"),
    (338, "Dave Bresheenahan", "called at 337; name as read from the list, spelling unverified"),
    (386, "Brian Free", "called at 385"),
    (442, "Matthew Endicott", "called at 441 and self-identifies: 'My name is Matt Endicott'"),
    (482, "Andrew Burke", "called at 481 and self-identifies: \"My name's Andrew Burke\""),
    (562, "Anthony Wood", "called at 561; self-identifies as 'Anthony'"),
    (607, "Bruno Hunsaker", "called at 606"),
    (655, "Nathan Thornburg", "called at 654 and self-identifies"),
    (699, "Matt Everett", "called at 698"),
    (773, "Betty Peralta", "called at 772 and self-identifies"),
    (840, "Josh Shadd", "called at 839 and self-identifies"),
    (873, "Mark Espinoza", "called at 872"),
    (921, "Jeff Ruth", "called at 920"),
    (964, "Denver Dorrell", "called at 963"),
    (1002, "Mark Cookston", "called at 1001; corrects the pronunciation himself"),
    (1048, "Tracy Perretta", "called at 1047"),
    (1096, "Steve Pereda", "called at 1095; 'My wife just got done speaking'"),
    (1138, "Charlotte Dukos", "called at 1137; name as read, rendered 'Ducos' elsewhere"),
    (1185, "Michael Valentini", "called at 1184 and self-identifies"),
    (1234, "Maria Perez", "called at 1233"),
    (1245, "Walter Peterson", "called at 1244 and self-identifies"),
    (1262, "Jacob Moore", "called at 1261 and self-identifies"),
    (1310, "Jason Rasmussen", "called at 1309 and self-identifies"),
    (1343, "Joy Rasmussen", "called at 1342"),
    (1394, "Michael Carey", "called at 1393; reads a prepared statement"),
    (1488, "Steve Campbell", "called at 1487; '20-year resident'"),
    (1506, "Cedric DeChermond", "called at 1505 as 'Shedrick D. Shermond' and corrected on the record: 'for the record, Cedric DeChermond'"),
    (1618, "Kristen Meyer", "called at 1617 and self-identifies"),
    (1652, "Cheryl Carr", "called at 1651 and self-identifies"),
    (1688, "Roger Laird", "called at 1687; 'my day started at 2:30 this morning'"),
    (1778, "Megan Streeter", "called at 1777; 'I'm a delegate and precinct chair'"),
    (1817, "Tony Castillo", "called at 1816; 'We own a roofing company'"),
    (1867, "Joanna Brown", "called at 1866 and self-identifies"),
    (1934, "Carly King", "the chair marks her absent at 1927, then finds her online at 1929-1933 and she speaks"),
    (2062, "Cam Pugh", "called at 2061"),
    (2094, "Carly King", "self-identifies: 'my name is Carly King'"),
    (2219, "Joe Richmond", "self-identifies: 'my name is Joe Richmond'"),
    (2347, "Chris Waldron", "self-identifies: 'my name is Chris Waldron'"),
    (2368, "Eric Nelson", "asked to state his name and does: 'My name is Eric Nelson'"),
    (2474, "Roxanne Richmond", "self-identifies at 2473; the chair confirms 'Go ahead, Roxanne'"),
    (2512, "Tyler Duclos", "the recorder flags him at 2510 and he self-identifies"),
    (2552, "James Russell", "asked to state his name at 2566 and does: 'for the record, James Russell'"),
]

# The chair hands off by name before each of these. The five opening responses
# are unambiguous; the later ones are the handoffs that survive in the
# cross-talk, and everything between them is left unlabeled on purpose.
DELIBERATION: list[tuple[int, str, str]] = [
    (2631, "Zac Huish", "handed off at 2630: 'I'm going to start on my right, which is Zach. Councilmember Dewish'"),
    (2676, "Rich Wood", "handed off at 2675: 'Councilmember Wood. Go ahead, start my timer.'"),
    (2715, "Craig Whiting", "handed off at 2714: 'Thank you. Councilmember Whiting.'"),
    (2754, "Brett Wright", "handed off at 2753: 'Okay, thank you. Councilmember Wright, go ahead.'"),
    (2800, "Melissa Clark", "handed off at 2799: 'go ahead, uh, Councilor McClark'"),
    (2841, CHAIR, "'now something a little different… council kind of knew that they were going to get a chance'"),
    (2852, "Ben Reeves", "the chair invites him at 2843-2847 — 'Our City Manager Ben Reeves… any comments that you'd like to make' — and he opens 'Thank you, Mayor'"),
    (2899, CHAIR, "'I can't see Sheriff Smith off the screen, but is he— there he is.'"),
    (2900, "Sheriff Smith", "invited at 2848; opens 'Thank you, Mayor' and speaks throughout as 'as the sheriff', 'as your sheriff'"),
    (2949, CHAIR, "retakes the floor — 'I will add a couple comments I forgot to do earlier' — then reports the QTS figures and the Pony Express commitment"),
    (3039, "Melissa Clark", "'Mayor, really quick' — the line-item budget answer the summary records as hers"),
    (3058, CHAIR, "answers her — 'everything that we've done has been in an open and public meeting'"),
    (3066, "Zac Huish", "handed off at 3065: 'Next, is that Council Member Huish? You go ahead.'"),
]

# Points where the chair takes the floor back for something long enough to be
# worth labeling, rather than the one-line "Thank you" between speakers.
CHAIR_TURNS: list[tuple[int, str, str]] = [
    (0, CHAIR, "opens the meeting and asks Wright for the prayer; asks 'Can you hear me okay, Lacey?', so this voice is not the recorder"),
    (2578, CHAIR, "'I'd like to read our proposed property tax impact schedule' — the summary records the mayor reading the schedule into the record"),
    (2616, CHAIR, "'I want to bring this back to the council and allow each of you to address…' — sets the three-minute limit for members"),
]

PRAYER: list[tuple[int, str, str]] = [
    (5, "Brett Wright", "announced at 4: \"I've asked Brett Wright, Council Member Wright, if he'd offer a prayer\""),
    (17, CHAIR, "resumes after the prayer to call the meeting to order"),
]

# The roll call names every member as they vote; each is a single cue.
ROLL_CALL_FROM = 4028

# After the last usable handoff the floor is open — members interrupt each
# other, staff answer by name, and the chair stops introducing anyone. Nothing
# in here is labeled except the roll call.
UNATTRIBUTED_FROM = 3169


def cues(path: Path) -> list[tuple[str, str]]:
    """(start timestamp, text) per .srt cue, in order."""
    out: list[tuple[str, str]] = []
    for block in path.read_text(encoding="utf-8").split("\n\n"):
        lines = block.strip().splitlines()
        timing = next((l for l in lines if "-->" in l), None)
        if not timing:
            continue
        said = " ".join(lines[lines.index(timing) + 1:]).strip()
        if said:
            out.append((timing.split("-->")[0].strip(), said))
    return out


SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# A cue boundary is not a speaker boundary: the recognizer breaks on pauses, so
# the chair's "Thank you. Betty Peralta." and the first words of Betty Peralta's
# comment routinely share one cue. Attribution therefore happens at sentence
# level, and these are the sentences that belong to whoever is running the
# meeting even when they land inside somebody else's cue.
CHAIR_FRAGMENT = re.compile(
    r"(add (?:it|him|her|them|his|the)\b.*\brecord"
    r"|include it into the record"
    r"|(?:do not|don't) see (?:him|her|them|any)"
    r"|not in attendance"
    r"|your time is up"
    r"|\d+ seconds remaining"
    r"|we(?:'ll| will) (?:get|go|move|come) (?:back|on|to)\b"
    r"|(?:go|come) ahead\b"
    r"|state your name for the record"
    r"|councilmember\s+\w+|council member\s+\w+"
    r")", re.I)

# A sentence that is nothing but a person's name is the chair reading the
# sign-in list.
BARE_NAME = re.compile(r"^[A-Z][a-z'\-]+(?:\s+(?:[A-Z]\.|[A-Z][a-z'\-]+)){1,2}[.?!]?$")


def sentences(rows: list[tuple[str, str]]) -> list[tuple[int, str]]:
    """Split the whole meeting into sentences, each tagged with its cue index."""
    out: list[tuple[int, str]] = []
    for i, (_, said) in enumerate(rows):
        for piece in SENTENCE_END.split(said):
            piece = piece.strip()
            if piece:
                out.append((i, piece))
    return out


def anchors() -> list[tuple[int, str, str]]:
    rows = PRAYER + CHAIR_TURNS + PUBLIC_COMMENT + DELIBERATION
    return sorted(rows, key=lambda r: r[0])


def start_of(sents: list[tuple[int, str]], cue: int) -> int:
    """First sentence index at or after `cue`, skipping the chair's hand-off.

    An anchor names the cue where a new voice starts, but the chair's call is
    usually still running at the top of it. Walk forward over any sentence that
    reads as the chair's — the called name on its own, a timer call, a note that
    somebody is absent — so the turn begins on the speaker's own first sentence.
    """
    idx = next((k for k, (c, _) in enumerate(sents) if c >= cue), len(sents) - 1)
    limit = idx + 6
    while idx < len(sents) - 1 and idx < limit:
        text = sents[idx][1]
        if BARE_NAME.match(text) or CHAIR_FRAGMENT.search(text):
            idx += 1
            continue
        break
    return idx


def turns(rows: list[tuple[str, str]]) -> list[tuple[str | None, int, str]]:
    """(speaker or None, cue index of the first sentence, text) per turn."""
    sents = sentences(rows)
    marks = [(start_of(sents, cue), who) for cue, who, _ in anchors()]
    # The floor opens up after the last usable hand-off; nothing past it is
    # labeled except by a later anchor.
    stop = start_of(sents, UNATTRIBUTED_FROM)

    who: list[str | None] = [None] * len(sents)
    for pos, (idx, speaker) in enumerate(marks):
        end = marks[pos + 1][0] if pos + 1 < len(marks) else len(sents)
        if idx >= stop:
            continue
        for k in range(idx, min(end, stop)):
            who[k] = speaker

    # Reclaim the tail: the call for the next speaker ("Your time is up. Thank
    # you. Betty Peralta.") is spoken by the chair, but lands at the end of the
    # outgoing speaker's last cue. Walk back from every boundary while the
    # sentences still read as the chair's.
    for idx, _ in marks:
        k = idx - 1
        while k >= 0 and idx - k <= 6:
            text = sents[k][1]
            if who[k] in (None, CHAIR) or not (
                    BARE_NAME.match(text) or CHAIR_FRAGMENT.search(text)):
                break
            who[k] = CHAIR
            k -= 1

    out: list[tuple[str | None, int, str]] = []
    for k, (cue, text) in enumerate(sents):
        if out and who[k] == out[-1][0]:
            out[-1] = (out[-1][0], out[-1][1], out[-1][2] + " " + text)
        else:
            out.append((who[k], cue, text))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true",
                    help="print coverage and write nothing")
    ap.add_argument("--show", action="store_true",
                    help="print every attributed turn with its first words")
    args = ap.parse_args()

    rows = cues(STEM.with_suffix(".srt"))
    grouped = turns(rows)

    words = sum(len(t.split()) for _, _, t in grouped)
    named = sum(len(t.split()) for w, _, t in grouped if w)
    voices = sorted({w for w, _, _ in grouped if w})

    print(f"{len(rows)} cues -> {len(grouped)} turns, {words:,} words")
    print(f"attributed: {named:,} words ({named / words:.0%}), {len(voices)} named voices")
    print(f"unattributed from cue {UNATTRIBUTED_FROM} "
          f"({rows[UNATTRIBUTED_FROM][0]}) - open deliberation")

    if args.show:
        for who, cue, text in grouped:
            if not who:
                continue
            print(f"\n[{rows[cue][0]}] {who} ({len(text.split())} w)")
            print(f"   {text[:100]}")
            print(f"   ...{text[-100:]}")
    if args.report or args.show:
        return

    out = [f"{who}: {text}" if who else text for who, _, text in grouped]
    STEM.with_suffix(".txt").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {STEM.with_suffix('.txt').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
