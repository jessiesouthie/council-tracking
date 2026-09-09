"""
Build the KNOWN ROSTER block for a body's enriched-summary prompt.

scripts/transcribe_meeting.sh used to carry one hardcoded roster: the mayor and
the five councilmembers. That is correct for the only body the nightly job
transcribes and wrong for every other one — handed a Planning Commission
recording, the prompt named six people who were not in the room and invited the
model to attribute the evening's discussion to them.

City Council keeps its hand-written block verbatim. The trap it encodes, that
Zac Hilton works in parks and recreation and is not Councilmember Zac Huish, is
a fact about two people and not something a members file can express. Every
other body builds its block from data/meta/<members file>, using the same
aliases the minutes parser already resolves roll-call names with, filtered to
whoever was actually serving on the date of the meeting being summarised.

Run:
  python3 -m ingest.roster_prompt city-council
  python3 -m ingest.roster_prompt planning-commission --date 2026-09-08
"""

from __future__ import annotations

import argparse
import json
from datetime import date as _date

from . import bodies

# Verbatim, and deliberately not generated: see the module docstring.
COUNCIL = '''KNOWN ROSTER (correct these common Whisper mishearings):
- Mayor Jared Gray.
- Councilmembers: Melissa Clark, Brett Wright, Craig Whiting, Rich Wood, Zac Huish.
  "Hewish" in the transcript = Zac Huish. Roll-call surnames are reliable.
- Zac Hilton is a STAFF member (parks/rec), a different person from Councilmember Zac Huish.
Attribute discussion to a named member only when the transcript makes it clear
(explicit names, or roll-call context); otherwise say "a councilmember."'''


def _serving(member: dict, on: str) -> bool:
    """True when any of the member's terms covers the date `on` (YYYY-MM-DD).

    A null start or end is open-ended, which is how an unresearched term reads.
    The filter is what keeps a past commissioner out of a current prompt: Craig
    Whiting sat on the Planning Commission through December 2025 and joined the
    City Council in January 2026, so an unfiltered roster would hand a 2026
    Planning Commission summary a sitting councilmember to attribute to.
    """
    terms = member.get("terms") or [{}]
    for t in terms:
        start, end = t.get("start"), t.get("end")
        if (start is None or start <= on) and (end is None or end >= on):
            return True
    return False


def _members(body: dict, on: str) -> list[dict]:
    try:
        data = json.loads(bodies.members_path(body).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    named = [m for m in (data.get("members") or []) if m.get("name")]
    return [m for m in named if _serving(m, on)]


def roster_block(body_id: str, on: str | None = None) -> str:
    """The roster block for `body_id` as the body stood on `on` (default today)."""
    if body_id == "city-council":
        return COUNCIL

    on = on or _date.today().isoformat()
    body = bodies.get_body(body_id)
    label = body["label"]
    members = _members(body, on)

    names = [m["name"] for m in members]
    roles = [t["role"] for m in members for t in (m.get("terms") or []) if t.get("role")]

    # "Commissioner" -> "a commissioner". The fallback matters: a body whose
    # members file carries no role must still get a neutral noun rather than
    # inheriting "a councilmember" from the block this one replaced.
    noun = max(set(roles), key=roles.count).lower() if roles else "member"

    lines = ["KNOWN ROSTER (correct these common speech-to-text mishearings):"]
    if names:
        # Anyone whose role is not the body's usual one is marked. An alternate
        # declares conflicts and joins debate but is not called in most votes,
        # and a summary that does not know this reads the gap as a missing vote.
        shown = [f"{n} ({r})" if r != noun.title() else n
                 for n, r in zip(names, [(m.get("terms") or [{}])[0].get("role") or ""
                                         for m in members])]
        lines.append(f"- {label} members: {', '.join(shown)}.")
        lines.append("  Roll-call surnames are reliable.")
        for m in members:
            alts = [a for a in (m.get("aliases") or []) if a != m["name"]]
            if alts:
                lines.append(f"  {', '.join(alts)} in the transcript = {m['name']}.")
    else:
        # No members file yet. Say so, rather than leaving the model to fill the gap.
        lines.append("- No roster is on file for this body. Take every name from the"
                     " transcript itself and never supply one from outside it.")

    # Only warn a body off the council's names when it does not share them. The
    # Redevelopment Agency Board *is* the council sitting as the RDA, so those
    # names are correct there and the warning would be a lie.
    council = {m["name"] for m in _members(bodies.get_body("city-council"), on)}
    if names and not (council & set(names)):
        lines.append(f"- This is the {label}, NOT the City Council. The mayor and the")
        lines.append("  councilmembers are different people who are not members of")
        lines.append("  this body; attribute nothing to them unless the transcript")
        lines.append("  names them as present.")

    if names:
        # The list is a reference, not a register of who turned up. It is edited
        # by hand and goes stale between an appointment and someone noticing.
        lines.append("This list is a spelling and membership reference and may be out of")
        lines.append("date. The transcript is the authority on who was actually present:")
        lines.append("never place someone in the room because this list names them, and")
        lines.append("where the meeting names a member who is missing here, use the name")
        lines.append("the meeting used and note it under About this transcript.")
    lines.append("Attribute discussion to a named member only when the transcript makes it clear")
    lines.append(f'(explicit names, or roll-call context); otherwise say "a {noun}."')
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("body")
    ap.add_argument("--date", help="roster as it stood on this date (YYYY-MM-DD); "
                                   "defaults to today")
    args = ap.parse_args()
    try:
        print(roster_block(args.body, args.date))
    except KeyError as exc:
        ap.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
