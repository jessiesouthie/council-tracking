# Claims intake

Where a rumour becomes a queue entry, and a queue entry becomes a card on
[claims.html](../../docs/claims.html).

## Why this is manual

There is no supported way to pull posts out of Facebook groups. The Graph API's
Page Public Content Access is gated behind app review for approved use cases,
the Groups API was cut back in 2022, and scraping a group breaks the terms of
service and dies on every layout change besides. So intake is a person reading
the groups they already read, and pasting a line into `inbox.jsonl`.

That is not the expensive part. The expensive part is finding the meeting,
motion or figure that settles a claim, and `ingest/check_claim.py` does that
against the corpus the site already builds.

## The loop

1. **Capture.** Append one JSON object per line to `inbox.jsonl`:

   ```json
   {"claim": "The council raised property taxes 220 percent.", "seen": "city Facebook group", "first_seen": "2026-08-12", "note": "on a yard sign in the photo too"}
   ```

   Fields: `claim` (required — the assertion in one sentence, in the words it is
   circulating in), `seen` (where, in general terms), `first_seen` (ISO date),
   `note` (anything you want the checker to know), `published_as` (the `id` in
   `docs/data.claims.json` once it ships — that is what marks a row done).

   **No names, no screenshots, no links to individuals' posts.** The claim is
   the thing being checked; the person repeating it is not. Officials speaking
   in an open meeting are the exception, and they are quoted from the recording
   rather than from a post.

2. **Triage.** `python -m ingest.check_claim --inbox` ranks the corpus against
   every unpublished row and prints candidate citations with deep links. One
   claim at a time: `python -m ingest.check_claim "the city has its own police"`.
   Claims about what the city has or hasn't approved are answered out of the
   motions — `--kind motion` — because a recorded vote settles them and a
   transcript only describes them.

3. **Read the source.** The script returns candidates, not verdicts. Open the
   meeting, read around the passage, and check that the quote says what the
   excerpt suggests.

4. **Write the entry** in `docs/data.claims.json`, following the rules that file
   states in `how` — every assertion sourced, the claim quoted and the claimant
   unnamed, a `checked` date on every card.

5. **Close the row** by setting `published_as` to the new entry's `id`, then
   rebuild: `python -m ingest.build_agent_corpus` so the site's Q&A agent can
   answer with the check, and `python -m ingest.build_sitemap`.

## Where claims come from besides Facebook

**Public comment, mined automatically.** `python -m ingest.mine_claims` reads the
transcripts for claims already in circulation and proposes them. It does not look
for claims — it looks for the attribution people put in front of one: "I've heard
that…", "I read on Facebook that…", "the rumour is…". A speaker marking a
statement as second-hand is reporting something going around, which is what this
page checks; a speaker stating an opinion is not.

    python -m ingest.mine_claims --since 2026-01-01     # look
    python -m ingest.mine_claims --write                # queue what it found

Roughly half of what comes back is a real candidate — precision is tuned ahead of
recall, because a queue that is mostly noise does not get read. Mined rows land
with `needs_wording: true` and the passage as spoken: turning a two-minute comment
into a one-sentence claim is an editorial act and belongs to whoever checks it.
Nothing it captures is a name — half the transcripts are not diarized and the
rest say "Speaker J", which suits the page's own rule.

This is the best feed available, better than the Facebook groups: someone
believed the thing strongly enough to drive to a meeting and say it on a
recording, and the passage is already published, timestamped and linkable.

Two other sources:

- The city's own messaging. Two of the entries on the page are the city's
  claims, checked the same way.
- The site's Q&A agent. What people ask it is a free feed of what they believe;
  a question phrased as "is it true that…" is a claim that has reached someone.
