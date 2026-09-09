# Transcript Pipeline Controls

**What guards the quality of a machine transcript and an AI meeting summary before either one reaches a reader.**

Civic Roll Call turns Eagle Mountain's meeting recordings into searchable transcripts and plain-English write-ups. A speech-to-text model mishears names, and a summarizer will invent a consequence for a motion if nothing stops it. The pipeline answers both in four stages.

Every control below carries a tag for what stands behind it, because the difference matters when you are deciding how far to trust a page. Code that halts the run is a guarantee. A rule written into a prompt is not.

| Tag | Meaning |
| --- | --- |
| `code` | The program enforces it. A violation fails the run or gets corrected before anything is written. |
| `prompt` | A rule the model is held to. It shapes the output and does not police it. |
| `human` | A person reviews or decides. The pipeline hands off rather than guessing. |
| `reader` | The limit is printed on the published page, so a reader sees it too. |

---

## 1. Getting the audio right

Audio comes from the city's own CivicClerk recording. Two paths transcribe it: Whisper on-device, or AssemblyAI with speaker diarization.

**A transcript cannot land on the wrong meeting** · `code`
The script resolves the meeting date and recording URL from the public API by event id, then writes every file under the stem `<date>__<eventId>`. Nothing downstream matches on a title or a guess about which night it was.
→ `scripts/transcribe_meeting.sh`

**Whisper's repeat hallucinations are collapsed** · `code`
Over long silences, such as the gap while the council sits in closed session, Whisper emits the same line again and again. Runs of identical consecutive lines are dropped before anything reads the file.
→ `scripts/transcribe_meeting.sh`, post-transcription pass

**An empty transcript stops the run** · `code`
`[ -s "$TXT" ] || die` after the cloud path, and a word count printed on the way out. A recording that produced no text fails loudly rather than publishing a blank page.
→ `scripts/transcribe_meeting.sh`

**Every sentence keeps its position in the recording** · `code`
Timestamped `.srt` and `.vtt` files sit beside the plain text, so any claim traces back to a spot in the audio. The publisher derives the meeting length from the last cue, which is how a truncated transcript shows itself: the duration stops short of the meeting.
→ `ingest/build_transcripts.py` · `_duration_from_srt`

**Speaker labels are detected, never assumed** · `code` `reader`
The publisher scans for `Speaker X:` prefixes and records a per-meeting `diarized` flag. The site renders a labeled transcript and an unlabeled one differently, so the page never implies an attribution the recognizer did not make.
→ `ingest/build_transcripts.py`

### When diarization failed: the 6 August tax hearing

AssemblyAI collapsed on that recording, four hours and forty-one minutes with around sixty-five speakers, and returned a single speaker turn. Re-running it would have cost several gigabytes and another bill for a result likely to collapse the same way. A retry that half-worked would have been worse, since twenty-six letters spread across sixty-five people manufactures attribution the audio cannot support.

The meeting labels its own speakers out loud: the chair reads each name off the sign-in list before that person speaks. So the text was rebuilt one line per subtitle cue, and each speaker was attributed from the chair's name-calls, with the evidence recorded next to every label, for instance *"called from the list at 134; speaks immediately"*. The stretch of open cross-talk after the three-hour mark carries no labels at all, because the recording offers no handle there. The `diarized` flag stays false, since these are textual inferences and not recognizer output.

→ `ingest/attribute_747.py`

---

## 2. Keeping the summary inside the evidence

The prose meeting summary is written by a model. Its instructions constrain the output rather than asking for quality, and the pipeline treats a failed summary as unfinished work.

**A second, independent source** · `code`
The same run pulls the official agenda from the API and hands it to the model alongside the transcript, so agenda references and item titles check against a document the model did not produce.
→ `scripts/transcribe_meeting.sh`, step 5

**Named roster corrections** · `prompt` `code`
The prompt lists the body's own members by name and fixes the recurring mishearings. City Council's block is hand-written, because the trap that Zac Hilton works in parks and recreation and is a different person from Councilmember Zac Huish is a fact about two people that no members file can express. Every other body builds its block from `data/meta/<members file>`, filtered to whoever was serving on the date of the meeting — without that filter a 2026 Planning Commission summary would be handed Craig Whiting, who sat on that commission through December 2025 and joined the City Council the following month. Because the list is edited by hand and goes stale between an appointment and someone noticing, the block also tells the model the roster is a reference and the transcript is the authority on who was in the room.
→ `ingest/roster_prompt.py`

**Unclear speech gets an unnamed speaker** · `prompt`
Discussion is attributed to a member only where the transcript makes it clear through an explicit name or roll-call context. Otherwise the summary says "a councilmember" and leaves it there.

**A vote count appears only when the transcript states one** · `prompt`
The decisions table gives a tally as "5-0" when the audio records one and "Voice vote" when it does not. A meeting where nothing came to a vote replaces the table with a single line saying so, so silence never reads as an oversight.

**The summary has to publish its own error rate** · `prompt` `reader`
Every write-up ends with a section called *About this transcript*, covering likely misheard proper nouns, the limits of speaker attribution, and spots where the audio was unclear. The known weaknesses ship with the document.

**A timestamp past the end of the recording is caught** · `code` `human`
The Meeting map gives each item an elapsed position, and the model has no clock: it estimates from how far through the transcript it is, and on a long meeting that estimate runs late. Seven of the archive's twenty-two summaries place items after the audio stops, the worst by 1h38m. Two things now hold. The prompt is told the recording's true length, measured from the last `.srt` cue, before it writes anything. Afterwards `check_summary` re-reads the map against that same length and flags any mark beyond it. The check reports and never edits: a drifting timestamp is a symptom, and the response is a person reading the meeting, not a script rewriting the model's prose. Reading the column is itself the hard part, since `1:14` means an hour and 14 minutes in a four-hour council meeting and 74 seconds in a twenty-minute special session, and one board's map switches between the two mid-column — so the scale is chosen per summary as whichever reading keeps the map running forwards.
→ `ingest/check_summary.py` · `scripts/transcribe_meeting.sh`, step 5b

**A half-written summary is never published** · `code`
Output goes to a temporary file and is promoted only when the call succeeded and produced text. On failure the run deletes it and exits non-zero, leaving no summary on disk.
→ `scripts/transcribe_meeting.sh`

**Failures come back tomorrow** · `code`
The work queue treats the summary file, not the transcript, as the marker that a meeting is finished. A meeting whose summary failed stays pending, and the next run skips the expensive transcription and fills in what is missing.
→ `ingest/pending_transcripts.py`

**An unattended run cannot hang or stop to ask** · `code`
The scheduled job caps the summary call with a thirty-minute timeout and runs it non-interactively, so a stalled request costs one meeting instead of the whole batch.
→ `.github/workflows/transcribe-meetings.yml`

---

## 3. Not corrupting the record over time

A separate tool writes the plain-English headline, summary and resident impact for each individual motion. Its controls guard the archive against drift as much as they guard any single call.

**Summaries are keyed to content, not to an id** · `code`
Motion ids are a running counter assigned in filename order, so back-filling an older document renumbers everything after it. Keying the cache by id would reattach summaries to the wrong motions without a trace. Each entry is keyed by a hash of the motion text instead, which also means a re-summarize triggers when the parser starts reading the source differently.
→ `ingest/summarize_motions.py` · `motion_key`

**Hand corrections survive every later run** · `code` `human`
A cached entry is never overwritten without an explicit `--force`. A separate staleness check tops up entries missing a field added later, and leaves edited text alone.
→ `ingest/summarize_motions.py` · `is_stale`

**Model output is validated on the way in** · `code`
Fields are length-clamped, and the notable-or-routine flag falls back to "routine" for anything that is not one of those two words. A typo or an invented third tier cannot promote a housekeeping item onto the front of the site. A motion that comes back with no headline and no summary is dropped rather than stored blank.
→ `ingest/summarize_motions.py` · `clean_significance`

**Invented consequences are the named failure mode** · `prompt`
Most motions are procedural and a resident feels nothing. The prompt requires the tool to say that plainly, in different words each time, instead of manufacturing an effect. It also forbids restating the vote, which the page already shows.

**Generated text goes through a person** · `human`
The weekly automated ingest never calls this tool. Motion summaries are generated locally, written to a cache file and committed, so the text arrives as a reviewable diff before it ships. The cache is also checkpointed after every call, so an interrupted backfill loses one meeting rather than a hundred.
→ `data/meta/motion_summaries.json`

---

## 4. Leaving an audit trail

The controls that matter most are the ones that let a reader, or a critic, check the site against the source.

**No model touches a vote** · `code`
Motions, roll calls and outcomes come from a deterministic parser over the official minutes, with footer detection, an exclusion rule for adjournment and closed-session motions, and a plausibility test on roll-call lines that rejects a sentence like "Councilmember Wright seconded the motion". Where an outcome cannot be found the parser writes `(outcome not found)` instead of guessing.
→ `ingest/parser.py`

**The parsed fact and the generated prose never compete** · `prompt`
The page renders the machine-parsed tally next to the plain-English text, and the summarizer is forbidden from restating either the vote or the outcome. One number, from one source.

**The parser is diffed against a frozen baseline** · `code`
A parity script compares the current parser row for row against a stored baseline across the whole document corpus, so a regex change that quietly drops motions shows up as a row count that no longer matches.
→ `legacy/_parity_check.py`

**Every artifact is under version control** · `human`
Parsed motion JSON, transcripts, summaries and the summary cache all live in git. Any regeneration arrives as a line-by-line diff rather than an opaque overwrite, which is what makes a bad change visible before it is published.

**Claim checking ranks sources and stops** · `human`
The rumor-checking tool searches the site's own corpus and returns ranked candidate records with deep links. It returns no verdict. Deciding what a claim asserts, and whether the record bears it out, stays an editorial act.
→ `ingest/check_claim.py`

**The reader is told what they are reading** · `reader`
Each meeting page carries the line: *Written up automatically from the audio. This is not the official record. Speakers are inferred from roll call, so attribution can be wrong.* Timeline positions are labeled as approximate spots in the recording rather than clock times.
→ `docs/meetings.html`

**The site's assistant ranks transcripts below the record** · `prompt` `code`
The question-answering widget is instructed never to assert a decision, a vote count or a dollar figure on a transcript's authority alone, since a transcript records what someone said and not what the body decided. It also filters its listed sources down to the ones its answer used.
→ `worker/src/index.js`

---

## 5. Naming the voices

Diarization tells us that the voice changed, never whose it is. AssemblyAI's labels are anonymous and per-recording: the mayor is Speaker A one night and Speaker J the next. Naming them was hand work — reading the transcript for the chair's name-calls, roll calls and self-introductions and writing a letter-to-name map by hand.

Six meetings of that hand work turned out to be a training set. It ties a label to a real person, and therefore ties hours of audio to a known speaker, which is enough to build an acoustic model of the people who appear again and again and to name them in meetings nobody has annotated. The controls below exist because that is a weaker kind of claim than the hand work it is built on, and the difference has to survive all the way to the page.

**A name is never written on similarity alone** · `code`
A label is assigned only when the best-matching voiceprint clears an absolute similarity floor *and* beats the runner-up by a margin. Either test failing leaves the speaker unnamed and the site keeps showing "Speaker A". Naming most speakers is not the goal; not naming the wrong one is.
→ `ingest/voiceprints.py` · `decide`

**A voice we have never met is called unknown, not called the nearest name** · `code`
The floor and margin above answer *which enrolled voice is nearest*. They cannot answer *is this anyone we know*, and on 3 September 2026 the difference published a real resident under another real resident's name. A speaker with no voiceprint sits far from everyone, so their nearest match wins by a very wide margin — and the margin test, built to separate two similar enrolled voices, reads that width as confidence and waves the stranger through. Three further gates now have to hold: the winner must be an outlier against the whole cohort rather than merely ahead of second place, must be enrolled from more than one meeting, and must match that person on at least two of their own nights. Fourteen of the archive's twenty-nine enrolled people fail the second gate, and no voice is named from their prints.
→ `ingest/voiceprints.py` · `decide`

**A diarizer label holding two people is never enrolled** · `code`
Averaging two speakers into one voiceprint produces a centroid belonging to neither, which then sits abnormally close to every voice at once and becomes a magnet for strangers — this is what scored 0.796 against the wrong person. Before enrolling, each label is split in two and checked: if both halves are internally alike while being unalike to each other, it holds more than one voice and is refused, with the slice counts printed for a human to split. This is not the same as a single speaker recorded under varying conditions, who is diffuse but not bimodal; the mayor's own label averages 0.42 across a long night and must not be thrown away for it. On first run it caught two labels, both from 21 July, cross-contaminated with each other: the animal-welfare presenter whose own words are "I'll turn it over to Bailey", and Bailey's label, which had additionally collected a land-disposal applicant three hours later.
→ `ingest/voiceprints.py` · `label_is_split` · `cmd_enroll`

**The library never studies from its own guesses** · `code`
Enrollment reads only attribution grounded in the transcript, where a name was actually spoken. Entries this module wrote itself carry `"method": "voiceprint"` and are dropped before enrolling, because training on a guess lets one bad match teach the library to make it again — the machine grading its own homework and then studying from it.
→ `ingest/voiceprints.py` · `labeled_meetings`

**A transcript identification outranks a voice match** · `code`
Where a meeting names its own speaker, that beats anything acoustic. `identify` keeps every hand-written entry in a map it rewrites and fills in only the letters they leave open, so re-running it can never overwrite a name taken from the transcript.
→ `ingest/voiceprints.py` · `cmd_identify`

**The library publishes its own error rate — including on strangers** · `code`
`verify` re-identifies every hand-labeled speaker against voiceprints rebuilt *without* that speaker's own meeting, so it can never look up an answer it already holds. On the current corpus of 71 labeled speakers: 56 of 56 named correctly, 98% recall on those findable at all, and all 14 who appear in only one meeting correctly declined rather than guessed.

That number was never the dangerous one. It is a *closed-set* measure — a speaker only enters the trial set once they are already in the library, so no trial could ever fail the way this pipeline actually failed. `verify` now also runs every labeled voice a second time against a library with their person removed outright, making all 71 of them strangers for whom the only correct answer is silence. The rule declines 71 of 71; the floor-and-margin pair alone named 3 of them, and named them confidently. The same command sweeps other thresholds, so the cost of loosening them is visible rather than assumed.
→ `ingest/voiceprints.py` · `cmd_verify`

**A first name is never merged into a full name automatically** · `code` `human`
Labels are keyed on the exact string in the speaker map, because the role in parentheses is often the only thing separating two people. The corpus contains the case that proves it: "Brad (Parks / Public Works)" and "Brad Patterson (bond counsel)" are two different men, and an earlier version of the grouping code merged them into one voiceprint. Single-first-name labels are now written to `identities.json` with no person attached and a note listing who they might be, for someone to resolve once, in a file that can be read.
→ `ingest/voiceprints.py` · `build_identities` · `data/voiceprints/identities.json`

**The machine audits the human** · `code`
`audit` runs the comparison in the opposite direction from everything else here: it checks the hand-written maps against the recordings, flagging any person whose two meetings do not sound alike and any two people who do. It earned its place on first run, catching a speaker on 4 August labeled as one consultant whose voice matches the other at 0.92, on a scale where no two different people in the archive reach 0.56. The label had been inferred from the agenda's list of who was scheduled to present; the voice disagreed, and the voice was right.
→ `ingest/voiceprints.py` · `cmd_audit`

**The page says which kind of claim it is** · `reader`
A transcript identification quotes the meeting naming its own speaker, and a reader can check it against the transcript printed below. A voice match cannot be checked on that page at all. Every voiceprint entry carries `"method": "voiceprint"` and a basis line giving both similarity scores and the runner-up by name, and the "Who is speaking" heading changes its wording depending on which kinds of identification a meeting actually contains.
→ `ingest/build_meeting_pages.py` · `ingest/voiceprints.py` · `cmd_identify`

**Naming a speaker can never cost a meeting** · `code`
Voice identification runs as step 4 of the transcription script and is optional at every level: it is skipped with one printed line when the environment is absent, when no library has been built, when the transcript has no speaker labels at all, or when a map already exists. A failure inside it is caught and reported without failing the run. The nightly job builds the environment only on runs that have a meeting to publish, caches it between runs, and marks that step `continue-on-error`, so a night when the install breaks still publishes transcripts and summaries with lettered speakers.
→ `scripts/transcribe_meeting.sh`, step 4 · `.github/workflows/transcribe-meetings.yml`

**A stopped run cannot corrupt the library** · `code`
Embeddings are cached per speaker as they finish and the library is written only once, at the end, from the full set. An interrupted enrollment leaves the previous library intact and resumes from the cache rather than re-cutting a thousand slices. The cache key records the sampling parameters, so changing how slices are chosen invalidates it instead of quietly mixing two schemes inside one voiceprint.
→ `ingest/voiceprints.py` · `sample_meeting`

---

## What none of this covers

Three limits, stated here rather than left for someone else to find.

- **The summary is only half cross-checked against the record.** `check_summary` compares the Meeting map against the recording's length, which is the cheap half. A vote tally the model gets wrong in the decisions table is still caught by a reader, not by the pipeline. Both sides exist in machine-readable form for the same meeting, so that check is buildable in the same place.
- **No test suite.** The parity script is a one-shot comparison tied to a stored baseline file, and nothing runs on every change.
- **Transcript accuracy is controlled but never measured.** Model choice, the repeat-collapse pass and diarization detection all shape the output. No word error rate has been sampled against a hand-checked meeting, so the site can describe how the transcripts are made and not how accurate they are. Speaker naming is now the exception: `voiceprints verify` measures it against held-out meetings, and the number is quoted above.

The first gap is still the one worth closing next, and `ingest/check_summary.py` is now the place to close it. The parsed motions and the summary's decisions table both describe the same meeting in structured form, so a check alongside the timing one could flag any meeting where the two disagree on the number of motions or on a tally, and put that meeting in front of a person.

Worth naming what the timing check taught, because it generalises. The first version read every elapsed mark as hours and minutes and reported ten of twenty-two summaries as broken. Three of those were the checker's error, not the summary's: a twenty-minute session's map counts in minutes and seconds, and reading it as hours manufactured a nineteen-hour meeting. A check that cries wolf on a third of its findings trains a reader to skim past all of them, which is worse than no check, so the scale is now inferred from the recording rather than assumed.

---

Civic Roll Call · civicrollcall.com
Eagle Mountain City Council coverage · 1 September 2026
