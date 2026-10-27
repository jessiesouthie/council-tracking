# Civic Roll Call

The code and data behind [civicrollcall.com](https://civicrollcall.com), a public record of Eagle Mountain, Utah's city council and its boards: every motion and vote since 2019, meeting transcripts, plain-English summaries, budget and tax pages, and fact checks.

The site is static. `docs/` is exactly what ships, with no build step. Python scripts in `ingest/` pull from the city's CivicClerk portal and write finished HTML and JSON into `docs/`. GitHub Actions runs those scripts on a schedule and commits the results.

**Only `docs/` is published.** Anything else in this repo is checked into git (and visible to anyone with repo access), but it never appears on the site.

---

## Folder layout

| Path | What's in it |
|---|---|
| `docs/` | **The published site.** HTML pages, `data*.json` datasets, `meetings/` (one page per meeting), `members/`, `transcripts/`, `claims/`, fonts, icons, the service worker, and the `*.test.mjs` tests. `CNAME` points the domain here. |
| `ingest/` | The Python pipeline. Each `build_*.py` writes one part of `docs/`. See [Ingest scripts](#ingest-scripts). |
| `scripts/transcribe_meeting.sh` | One command that transcribes a meeting, names speakers, writes the summary, and publishes it. |
| `data/raw/` | Approved minutes PDFs from CivicClerk, named `<date>__<eventId>.pdf`. City council sits at the top level and other bodies get subfolders. |
| `data/parsed/` | One JSON per minuted meeting, parsed from `data/raw/`. |
| `data/transcripts/<body>/` | `.txt`, `.srt`, `.vtt`, `.speakers.json` (speaker label → name, with reasoning) and `.summary.md` for each recorded meeting. |
| `data/meta/` | Caches and lookups: motion and meeting summaries, member rosters, event names, topic tags. |
| `data/voiceprints/` | The voice library used to name speakers (`library.json`, `identities.json`). `.model/` and `cache/` are gitignored and can be rebuilt. |
| `data/claims/` | The fact-check intake queue (`inbox.jsonl`). See `data/claims/README.md`. |
| `data/agendas/` | Scratch agenda downloads (gitignored). |
| `data/pdfs/` | Loose minutes files dropped in by hand. |
| `worker/` | The Cloudflare Worker behind the "Ask about this site" widget. It holds the Anthropic key the static site can't. See `worker/README.md`. |
| `legacy/` | The original single-file extractor, kept for parity checks. |
| `output/` | Spreadsheet exports (`*.xlsx` is gitignored). |
| `.github/workflows/` | The two scheduled jobs. See [Automation](#automation). |
| `render.yaml` | A Render static-site blueprint for `docs/`, kept as an alternative host. |

### Private working notes (not on the site)

- `council-member-profiles.md`: notes on each member, built from transcripts and votes. It's in the repo but not in `docs/`.
- `pipeline-quality-controls.md`: what checks a transcript and summary before they're published.
- `~/Documents/Council Notes/`: notes kept **outside** this repo so they're never committed, e.g. `mayor-gray-tracker.md`.

Don't move any of these into `docs/` unless you mean to publish them.

---

## Setup

Requires Python 3.12+, Node (for tests), `ffmpeg` and `whisper-cpp` (`brew install ffmpeg whisper-cpp`), and the `claude` CLI for summaries.

```sh
python3 -m venv .venv
.venv/bin/pip install -r ingest/requirements.txt
```

**Voice environment.** Speaker naming needs a heavier environment from `ingest/requirements-voice.txt`. This repo sits in `~/Documents`, which syncs to iCloud, and reads from `.venv-voice/` there can hang indefinitely. Use the copy outside iCloud instead:

```sh
python3 -m venv ~/.cache/council-tracking/venv-voice
~/.cache/council-tracking/venv-voice/bin/pip install -r ingest/requirements-voice.txt
export COUNCIL_VOICE_PYTHON=~/.cache/council-tracking/venv-voice/bin/python
```

**Keys and config** (in `~/.config/council-tracking/`, never in the repo):

- `assemblyai.key`: for `--cloud` transcription (or set `$ASSEMBLYAI_API_KEY`)
- `mail.env`: `COUNCIL_MAIL_TO` and SMTP credentials for summary emails (`chmod 600`; see `scripts/mail.env.example` and `ingest/mail_summary.py`)

---

## After a meeting: publishing a transcript

The morning-after workflow normally does this. When it doesn't run, or a meeting needs fixing, do it by hand:

1. **Find the event id** from the CivicClerk portal URL, or with `python -m ingest.pending_transcripts`.
2. **Transcribe, summarize, publish:**
   ```sh
   COUNCIL_VOICE_PYTHON=~/.cache/council-tracking/venv-voice/bin/python \
     scripts/transcribe_meeting.sh <EVENT_ID> --cloud
   ```
   Add `--body planning-commission` (etc.) for other bodies. The script can be re-run safely: finished steps are skipped. `--force` re-transcribes, `--resummarize` rewrites the summary, `--no-summary` skips Claude, and `--no-email` stays quiet. The full flag list is at the top of the script.
3. **Rebuild the site artifacts:**
   ```sh
   python -m ingest.build_agent_corpus --all-bodies
   python -m ingest.build_meeting_pages
   python -m ingest.build_sitemap
   ```
4. **Check by hand:**
   - Roll-call votes in the summary match the transcript.
   - Staff surnames are filled in. Use the `.speakers.json` and earlier summaries.
   - Speaker labels look right. Diarization sometimes merges a public commenter into a council member's label.
5. **Commit and push.** The summary email goes out at step 2, before any of these fixes.

## Weekly: new minutes

```sh
python -m ingest.crawl --all-bodies          # download new approved minutes
python -m ingest.build_dataset --all-bodies  # parse → data/parsed/ and docs/data*.json
python -m ingest.build_transcripts
```

The full weekly sequence, in order, is in `.github/workflows/weekly-ingest.yml`.

---

## Automation

| Workflow | When | What it does |
|---|---|---|
| `weekly-ingest.yml` | Mondays 6am Mountain, or by hand | Crawls new minutes, rebuilds every dataset and page, checks that the nav is in sync, and commits. |
| `transcribe-meetings.yml` | Daily, 7am and 2pm Mountain, or by hand | Finds meetings that have a recording but no summary, runs `transcribe_meeting.sh --cloud`, rebuilds, commits, and emails. |

Secrets: `ASSEMBLYAI_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`) are required. `COUNCIL_MAIL_TO`, `COUNCIL_SMTP_USER` and `COUNCIL_SMTP_PASS` are optional. If the Claude token is missing, the transcribe workflow fails and meetings have to be run by hand.

---

## Ingest scripts

Run each one as `python -m ingest.<name>`. Most `build_*` scripts accept `--all-bodies` and/or `--prune`.

| Script | Writes / does |
|---|---|
| `crawl` | Downloads new approved minutes PDFs into `data/raw/` |
| `build_dataset` | Parses minutes → `data/parsed/` and `docs/data*.json` |
| `build_transcripts` | Publishes transcripts to `docs/transcripts/` |
| `build_meeting_pages` | One page per meeting in `docs/meetings/` |
| `build_member_pages` | One page per member in `docs/members/` |
| `build_agent_corpus` | `docs/agent-corpus.json` for the Q&A widget |
| `build_upcoming` / `summarize_agenda` | Next-meeting card and one-line agenda summaries |
| `build_agenda_only` | Meetings that left only an agenda (no minutes or recording) |
| `build_claim_pages` | One page per fact check, with link-preview image |
| `build_budget_book` / `build_budget_next` | FY25–26 adopted and FY26–27 budget data |
| `build_staffing` | City headcount vs. peer cities |
| `build_prerender` / `build_schema` | Static HTML and JSON-LD for crawlers |
| `build_sitemap` | `docs/sitemap.xml` |
| `build_nav` | Copies `ingest/nav.py`'s destination list everywhere it's used (`--check` to verify) |
| `build_fonts` | Self-hosts the Google Fonts |
| `summarize_motions` | Plain-English motion headlines → `data/meta/motion_summaries*.json` |
| `check_summary` | Cross-checks a generated summary against known facts |
| `check_claim` / `mine_claims` | Finds the record behind a claim, and proposes claims from transcripts |
| `voiceprints` | Enrolls and matches speaker voices |
| `pending_transcripts` | Lists meetings with a recording but no transcript or summary |
| `mail_summary` / `mail_agenda` | Emails summaries and agendas |

Shared modules: `civicclerk` (API client), `bodies` (registry of covered bodies), `normalize` (member names and topic tags), `roster_prompt`, `nav`, `chrome_v2`, `house_style.txt` (writing rules for model prompts).

---

## Tests

```sh
node --test docs/*.test.mjs
```

They check page copy against the record and the house style, plus nav, meetings, tax, staffing, projections and claims logic.
