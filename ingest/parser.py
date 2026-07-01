"""
Browser-side parser for council meeting minutes (.pdf / .txt).

Runs inside Pyodide. Exposes two entry points called from app.js:

    parse_document(filename: str, data: bytes) -> dict
    export_xlsx(results: list[dict]) -> bytes

The parsing logic (regexes, state machines, exclusion rules) is the same as
the legacy CLI in ../legacy/extract_meeting.py — only the I/O surface differs.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime

import pdfplumber
import pandas as pd

# Legacy line-level sheets: whole-word matches
_ORD = re.compile(r"\bordinance\b", re.IGNORECASE)
_RES = re.compile(r"\bresolution\b", re.IGNORECASE)

# 13.A. RESOLUTION – title…
_AG_RES_ORD = re.compile(
    r"^(\d+\.\s*[A-Z]\.)\s*(RESOLUTION|ORDINANCE)\s*[–-]\s*(.*)$",
    re.IGNORECASE,
)

_VOTE = re.compile(r"^(.+?)\s+(Yes|No|Abstain|Excused|Nay|Absent)\.?$", re.IGNORECASE)

_MOTION = re.compile(r"^MOTION:\s*(.*)$", re.IGNORECASE)

_OUTCOME = re.compile(r"^The motion\s+(.+?)(?:\.\s*)?$", re.IGNORECASE)

# Fallback for narrative minutes (e.g. transcript-derived) where the outcome is
# embedded mid-sentence in the motion body ("… seconded. The motion passed
# unanimously. …") rather than on its own trailing line after a roll call.
_OUTCOME_INLINE = re.compile(
    r"\bthe motion\s+(passed|failed|carried|did not pass|did not carry)([^.;]*)",
    re.IGNORECASE,
)


def _outcome_from_motion_text(motion_text: str) -> str:
    """Best-effort outcome pulled from the motion sentence itself. Only used
    when no explicit trailing outcome line was found."""
    m = _OUTCOME_INLINE.search(motion_text or "")
    if not m:
        return ""
    phrase = re.sub(r"\s+", " ", (m.group(1) + m.group(2)).strip())
    if phrase and not re.search(r"[.!]$", phrase):
        phrase += "."
    return phrase

STOP_TITLES = re.compile(
    r"^The recording of the (?:discussion|motion) can be found", re.IGNORECASE
)

_MINUTES_DASH_DATE = re.compile(
    r"C(?:ity )?ouncil\s+Minutes\s*[\s–\-—–]+\s*"
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[a-z]+ \d{1,2},? \d{4})",
    re.IGNORECASE,
)


def _date_string_to_iso(s: str) -> str:
    s = re.sub(r"(\S),(\S)", r"\1, \2", s.strip(), count=1) if s else ""
    s = s.replace(",", ", ") if s and s.count(",") == 1 and ", " not in s else s
    s = re.sub(r",\s+", ", ", s)
    for fmt in (
        "%B %d, %Y",
        "%B %d,%Y",
        "%b %d, %Y",
        "%b %d,%Y",
    ):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return ""


_MONTH_NAME_TO_NUM: dict[str, int] = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def meeting_date_from_text(text: str) -> str:
    if not text or not str(text).strip():
        return ""
    t = text.replace("­", " ").replace("\r\n", "\n")

    m = _MINUTES_DASH_DATE.search(t)
    if m:
        iso = _date_string_to_iso(m.group(1).strip())
        if iso:
            return iso

    m2 = re.search(
        r"(?i)CITY COUNCIL MEETING MINUTES\s*[\n\r]+"
        r"(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER) "
        r"(\d{1,2}),\s*\d",
        t,
    )
    if m2:
        mnum = _MONTH_NAME_TO_NUM.get(m2.group(1).lower())
        day = int(m2.group(2))
        if mnum is not None and 1 <= day <= 31:
            y = None
            my = re.search(
                r"(?:C(?:ity )?ouncil |City Council )?Minutes"
                r"\s*[\s–\-—–]+\s*"
                r"[\w\s,]+(\d{4})",
                t,
                re.IGNORECASE,
            )
            if my:
                yy = int(my.group(1))
                if 1990 <= yy <= 2100:
                    y = yy
            if y is None:
                for mmy in re.finditer(
                    r"\b(20[0-9][0-9]|19[0-9][0-9])\b", t[: 12000]
                ):
                    yy = int(mmy.group(1))
                    if 1990 <= yy <= 2100:
                        y = yy
                        break
            if y is not None:
                try:
                    return datetime(y, mnum, day).date().isoformat()
                except (ValueError, OSError):
                    pass
    return ""


def _is_footer_line(line: str) -> bool:
    return "Council Minutes" in line and "Page" in line and "of" in line


def _clears_business_item(line: str) -> bool:
    s = line.strip()
    u = s.upper()
    if u.startswith("ELECTED OFFICIALS PRESENT"):
        return True
    if re.match(r"^4\.\s*ADJOURN", s, re.IGNORECASE) or u.startswith("4. ADJOURN"):
        return True
    return False


@dataclass
class BusinessItem:
    ref: str
    item_type: str
    title: str
    page: int


@dataclass
class MotionBlock:
    source_file: str
    meeting_date: str
    page: int
    motion_text: str
    outcome: str
    item_ref: str
    business_type: str
    item_title: str
    member_votes: list[tuple[str, str]] = field(default_factory=list)

    def to_summary_row(self) -> dict:
        votes = "; ".join(
            f"{n.strip()}: {v}" for n, v in sorted(self.member_votes, key=lambda t: t[0])
        )
        return {
            "source_file": self.source_file,
            "meeting_date": self.meeting_date,
            "page": self.page,
            "agenda_ref": self.item_ref,
            "business_type": self.business_type,
            "item_title": self.item_title,
            "outcome": self.outcome,
            "motion": self.motion_text,
            "roll_call": votes,
        }

    def to_member_rows(self) -> list[dict]:
        ex = self.motion_text
        if len(ex) > 500:
            ex = ex[:500] + "…"
        rows: list[dict] = []
        for name, v in self.member_votes:
            rows.append(
                {
                    "source_file": self.source_file,
                    "meeting_date": self.meeting_date,
                    "page": self.page,
                    "agenda_ref": self.item_ref,
                    "business_type": self.business_type,
                    "item_title": self.item_title,
                    "outcome": self.outcome,
                    "motion_excerpt": ex,
                    "member": name.strip(),
                    "vote": v,
                }
            )
        return rows


def _txt_pages(data: bytes) -> list[str]:
    s = data.decode("utf-8", errors="replace")
    if "\f" in s:
        return s.split("\f")
    return [s]


def _full_text_for_date(filename: str, data: bytes) -> str:
    if filename.lower().endswith(".txt"):
        return data.decode("utf-8", errors="replace")
    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t:
                parts.append(t)
    return "\n".join(parts)


def _all_lines_pdf(data: bytes) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            t = page.extract_text()
            if not t:
                continue
            for raw in t.splitlines():
                s = raw.strip()
                if s:
                    lines.append((page_index, s))
    return lines


def _all_lines_txt(data: bytes) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for page_index, part in enumerate(_txt_pages(data), start=1):
        for raw in part.splitlines():
            s = raw.strip()
            if s:
                lines.append((page_index, s))
    return lines


def _all_lines_for(filename: str, data: bytes) -> list[tuple[int, str]]:
    name = filename.lower()
    if name.endswith(".txt"):
        return _all_lines_txt(data)
    if name.endswith(".pdf"):
        return _all_lines_pdf(data)
    raise ValueError(f"Unsupported file type (use .pdf or .txt): {filename}")


def _finish_res_ord_title(
    i_after_ag: int, lines: list[tuple[int, str]], m: "re.Match[str]"
) -> tuple[str, int]:
    parts: list[str] = [m.group(3).strip()] if m.group(3) else []
    j = i_after_ag
    n = len(lines)
    while j < n:
        _, s = lines[j]
        if _is_footer_line(s):
            j += 1
            continue
        if not s:
            j += 1
            continue
        if _AG_RES_ORD.match(s):
            break
        if s.upper().startswith("MOTION:"):
            break
        if STOP_TITLES.match(s) or s.startswith("The recording"):
            break
        u = s.upper()
        if u in ("SCHEDULED ITEMS", "CONSENT AGENDA"):
            j += 1
            break
        if re.match(r"^\d+\.?\s*RESOLUTIONS$", s, re.IGNORECASE) or re.match(
            r"^\d+\.?\s*ORDINANCES$", s, re.IGNORECASE
        ):
            j += 1
            break
        parts.append(s)
        j += 1
    return " ".join(t for t in parts if t), j


def _plausible_vote_line(s: str) -> bool:
    vm = _VOTE.match(s)
    if not vm:
        return False
    if re.search(
        r"seconded the motion$", s, re.IGNORECASE
    ) or re.search(
        r" moved to ", s, re.IGNORECASE
    ) or s.lower().startswith("councilmember "):
        return False
    n_words = len(s.split())
    return 2 <= n_words <= 5


def _parse_motions(
    source_name: str,
    line_tuples: list[tuple[int, str]],
    meeting_date: str,
) -> list[MotionBlock]:
    out: list[MotionBlock] = []
    last_business: BusinessItem | None = None
    n = len(line_tuples)
    i = 0
    while i < n:
        page, line = line_tuples[i]

        if _clears_business_item(line):
            last_business = None
            i += 1
            continue

        m_ag = _AG_RES_ORD.match(line)
        if m_ag and not _is_footer_line(line):
            kind = m_ag.group(2)
            item_type = "Resolution" if kind.lower() == "resolution" else "Ordinance"
            title, j = _finish_res_ord_title(i + 1, line_tuples, m_ag)
            if not title:
                title = m_ag.group(3).strip() or f"{m_ag.group(1).strip()} {item_type}"
            last_business = BusinessItem(
                ref=m_ag.group(1), item_type=item_type, title=title, page=page
            )
            i = j
            continue

        mm = _MOTION.match(line)
        if mm and not _is_footer_line(line):
            motion_bits: list[str] = [mm.group(1).strip()] if mm.group(1) else []
            page_motion = page
            i += 1
            while i < n:
                _, s = line_tuples[i]
                if _is_footer_line(s) or not s:
                    i += 1
                    continue
                if _plausible_vote_line(s):
                    break
                if s.upper().startswith("MOTION:") or _AG_RES_ORD.match(s):
                    break
                if re.match(
                    r"^The (?:motion|Work Session|meeting) ", s, re.IGNORECASE
                ) or s.startswith("The Work Session") or s.startswith("The meeting"):
                    break
                if STOP_TITLES.match(s):
                    break
                motion_bits.append(s)
                i += 1

            votes: list[tuple[str, str]] = []
            while i < n:
                p_v, s = line_tuples[i]
                if _is_footer_line(s) or not s:
                    i += 1
                    continue
                if not _plausible_vote_line(s):
                    break
                v = _VOTE.match(s)
                if v:
                    votes.append((v.group(1).strip(), v.group(2).rstrip(". ")))
                page_motion = p_v
                i += 1

            outcome = ""
            if i < n:
                _, s = line_tuples[i]
                om = _OUTCOME.match(s)
                if om:
                    out_t = om.group(1).strip()
                    if out_t and not re.search(
                        r"[\.\!]$", out_t
                    ):
                        out_t += "."
                    outcome = out_t
                    i += 1

            ref, btype, ititle = "", "Other / consent / misc.", ""
            if last_business is not None:
                ref = last_business.ref
                btype = last_business.item_type
                ititle = last_business.title

            motion_text = re.sub(
                r"\s+", " ", " ".join(x for x in motion_bits if x)
            ).strip()
            if not motion_text and mm:
                motion_text = mm.group(0)

            if not outcome:
                outcome = _outcome_from_motion_text(motion_text)

            if votes or motion_text or outcome:
                block = MotionBlock(
                    source_file=source_name,
                    meeting_date=meeting_date,
                    page=page_motion,
                    motion_text=motion_text,
                    outcome=outcome or ("" if not votes else "(outcome not found)"),
                    item_ref=ref,
                    business_type=btype,
                    item_title=ititle,
                    member_votes=votes,
                )
                _refine_motion_anchors(block, motion_text)
                if not _should_exclude_adjourn_or_closed_session(block):
                    out.append(block)
                # An agenda-heading anchor is consumed by the first motion it
                # produces. Otherwise a single 13.A. heading would attach its
                # title to every later motion until the next heading appears.
                last_business = None
            continue

        i += 1
    return out


def _should_exclude_adjourn_or_closed_session(block: MotionBlock) -> bool:
    if block.business_type in ("Adjournment", "Closed session"):
        return True
    t = re.sub(r"\s+", " ", (block.motion_text or "").lower())
    if not t:
        return False
    if re.search(
        r"adjourn to (?:a )?closed|moved to adjourn to a closed|"
        r"into (?:a )?closed (?:executive )?session|"
        r"go(?:ing)?\s+to (?:a )?closed|executive session"
        r"|section\s+52-4-205",
        t,
    ):
        return True
    if re.search(
        r"moved to adjourn\b|adjourn the meeting|"
        r"adjourn(?:ed)? at \d{1,2}:\d{2}",
        t,
    ) and "closed session" not in t:
        return True
    return False


_TITLE_FROM_MOTION = re.compile(
    r"\b((?:An?\s+)?(?:Resolution|Ordinance)\s+of"
    r"(?:\s+the\s+City\s+of)?\s+Eagle\s+Mountain"
    r"[\s\S]+?)"
    r"(?=\.\s+(?:Councilmember|Council\s*Member|Mayor)\s+[A-Z]"
    r"|\.\s*$|\Z)",
    re.IGNORECASE,
)
_ORD_NUM = re.compile(
    r"\b((?:Ordinance|Resolution)(?:\s+No\.?)?\s+[Oo]?\d[\d\-]*)",
    re.IGNORECASE,
)

# Match the verb phrase inside "Councilmember X moved [by Y] to <action>."
_VERB_SUMMARY = re.compile(
    r"(?:moved\s+(?:by\s+\S+\s+)?to\s+)([\s\S]+?)"
    r"(?=\.\s+(?:Councilmember|Council\s*Member|Mayor)\s+[A-Z]"
    r"|\.\s*$|\Z)",
    re.IGNORECASE,
)


def _title_from_motion(motion_text: str) -> tuple[str, str]:
    """Pull a resolution/ordinance preamble out of the motion text itself.
    Returns (title, business_type) or ("", "")."""
    if not motion_text:
        return "", ""

    m = _TITLE_FROM_MOTION.search(motion_text)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(".")
        if title:
            title = title[0].upper() + title[1:]
        head = title.lower().split(None, 3)
        btype = "Ordinance" if "ordinance" in head[:3] else "Resolution"
        return title, btype

    # Last resort: a bare "Ordinance 2026-13" / "Resolution No. 2025-09".
    m2 = _ORD_NUM.search(motion_text)
    if m2:
        ref = re.sub(r"\s+", " ", m2.group(1)).strip()
        btype = "Ordinance" if "ordinance" in ref.lower() else "Resolution"
        return ref, btype

    return "", ""


def _summary_from_motion(motion_text: str, max_len: int = 96) -> str:
    """For procedural motions (appointments, schedule items, legislative priority
    additions, etc.) we have no resolution preamble — capture the action verb
    phrase to use as a humane title."""
    if not motion_text:
        return ""
    m = _VERB_SUMMARY.search(motion_text)
    if not m:
        return ""
    s = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(".,")
    if not s:
        return ""
    s = s[0].upper() + s[1:]
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s


def _refine_motion_anchors(block: MotionBlock, motion_text: str) -> None:
    t = (motion_text or "").lower()
    if "consent agenda" in t and "moved" in t:
        block.item_ref = "Consent"
        block.item_title = "(see motion text for included items)"
        block.business_type = "Consent batch"
        return
    if re.search(
        r"adjourn to a closed session|moved to adjourn to a closed", t
    ):
        block.item_ref = ""
        block.item_title = "Move to closed session / executive session"
        block.business_type = "Closed session"
        return
    if re.search(
        r"moved to adjourn the meeting|adjourn the meeting at|adjourn at \d", t
    ) and "closed session" not in t:
        block.item_ref = ""
        block.item_title = "Adjournment"
        block.business_type = "Adjournment"
        return

    # Fallback cascade for motions that didn't pick up a title from an agenda
    # heading. First try the resolution/ordinance preamble inside the motion
    # text; then fall back to summarising the action verb phrase.
    if not block.item_title:
        title, btype = _title_from_motion(motion_text or "")
        if title:
            block.item_title = title
            if btype and block.business_type in ("Other / consent / misc.", ""):
                block.business_type = btype
        else:
            summary = _summary_from_motion(motion_text or "")
            if summary:
                block.item_title = summary


def _line_hits(filename: str, data: bytes, mdate: str) -> tuple[list[dict], list[dict]]:
    ords: list[dict] = []
    ress: list[dict] = []

    def _scan(pages: list[tuple[int, str]]) -> None:
        for pnum, text in pages:
            for line in text.splitlines():
                s = line.strip()
                if not s:
                    continue
                if _ORD.search(s):
                    ords.append(
                        {
                            "source_file": filename,
                            "meeting_date": mdate,
                            "page": pnum,
                            "line_text": s,
                        }
                    )
                if _RES.search(s):
                    ress.append(
                        {
                            "source_file": filename,
                            "meeting_date": mdate,
                            "page": pnum,
                            "line_text": s,
                        }
                    )

    name = filename.lower()
    if name.endswith(".txt"):
        _scan(list(enumerate(_txt_pages(data), start=1)))
    else:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [(pnum, page.extract_text() or "") for pnum, page in enumerate(pdf.pages, start=1)]
        _scan(pages)
    return ords, ress


def parse_document(filename: str, data: bytes) -> dict:
    """Parse a single .pdf or .txt and return a JSON-serializable dict."""
    if isinstance(data, memoryview):
        data = bytes(data)
    elif not isinstance(data, (bytes, bytearray)):
        data = bytes(data)
    if isinstance(data, bytearray):
        data = bytes(data)

    mdate = meeting_date_from_text(_full_text_for_date(filename, data))
    ords, ress = _line_hits(filename, data, mdate)
    motion_blocks = _parse_motions(filename, _all_lines_for(filename, data), mdate)

    motions = [b.to_summary_row() for b in motion_blocks]
    votes_by_member: list[dict] = []
    for b in motion_blocks:
        votes_by_member.extend(b.to_member_rows())

    return {
        "filename": filename,
        "meeting_date": mdate,
        "motions": motions,
        "votes_by_member": votes_by_member,
        "ordinances": ords,
        "resolutions": ress,
    }


_XLSX_COLS_ORDS = ["source_file", "meeting_date", "page", "line_text"]
_XLSX_COLS_RESS = _XLSX_COLS_ORDS


def export_xlsx(results: list[dict]) -> bytes:
    """Build a 4-sheet .xlsx matching the legacy CLI output and return its bytes."""
    all_ords: list[dict] = []
    all_ress: list[dict] = []
    all_motions: list[dict] = []
    all_member_rows: list[dict] = []
    for r in results:
        all_motions.extend(r.get("motions", []))
        all_member_rows.extend(r.get("votes_by_member", []))
        all_ords.extend(r.get("ordinances", []))
        all_ress.extend(r.get("resolutions", []))

    df_s = pd.DataFrame(all_motions)
    df_m = pd.DataFrame(all_member_rows)
    df_o = pd.DataFrame(all_ords, columns=_XLSX_COLS_ORDS)
    df_r = pd.DataFrame(all_ress, columns=_XLSX_COLS_RESS)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df_s.to_excel(writer, sheet_name="motions", index=False)
        df_m.to_excel(writer, sheet_name="votes_by_member", index=False)
        df_o.to_excel(writer, sheet_name="ordinances", index=False)
        df_r.to_excel(writer, sheet_name="resolutions", index=False)
    return buf.getvalue()
