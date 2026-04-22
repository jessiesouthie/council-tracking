"""
Extract ordinance/resolution line hits and roll-call vote blocks from city
council meeting PDFs or plain text (.txt); export to Excel.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pdfplumber
import pandas as pd

# Legacy line-level sheets: whole-word matches
_ORD = re.compile(r"\bordinance\b", re.IGNORECASE)
_RES = re.compile(r"\bresolution\b", re.IGNORECASE)

# 13.A. RESOLUTION – title…
_AG_RES_ORD = re.compile(
    r"^(\d+\.\s*[A-Z]\.)\s*(RESOLUTION|ORDINANCE)\s*[\u2013-]\s*(.*)$",
    re.IGNORECASE,
)

_VOTE = re.compile(r"^(.+?)\s+(Yes|No|Abstain|Excused|Nay|Absent)\.?$", re.IGNORECASE)

_MOTION = re.compile(r"^MOTION:\s*(.*)$", re.IGNORECASE)

_OUTCOME = re.compile(r"^The motion\s+(.+?)(?:\.\s*)?$", re.IGNORECASE)

STOP_TITLES = re.compile(
    r"^The recording of the (?:discussion|motion) can be found", re.IGNORECASE
)

# e.g. "Eagle Mountain City Council Minutes – March 25, 2026 Page 1 of 8"
_MINUTES_DASH_DATE = re.compile(
    r"C(?:ity )?ouncil\s+Minutes\s*[\s\u2013\-\u2014–]+\s*"
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
    """
    Return meeting date as YYYY-MM-DD when detectable, else "".
    Prefers the date on the "Council Minutes – …" line (often a page footer);
    else uses the "CITY COUNCIL MEETING MINUTES" header plus a 4-digit year
    from the same document.
    """
    if not text or not str(text).strip():
        return ""
    t = text.replace("\u00ad", " ").replace("\r\n", "\n")

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
                r"\s*[\s\u2013\-\u2014–]+\s*"
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


def _read_txt_as_pages(path: Path) -> list[str]:
    """Read .txt. Form feed (\\f) characters split into virtual pages; otherwise one page."""
    data = path.read_text(encoding="utf-8", errors="replace")
    if "\f" in data:
        return data.split("\f")
    return [data]


def full_text_for_date(path: Path) -> str:
    """All document text, used to infer meeting date (PDF or .txt)."""
    if path.suffix.lower() == ".txt":
        return path.read_text(encoding="utf-8", errors="replace")
    parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t:
                parts.append(t)
    return "\n".join(parts)


def _all_lines(pdf_path: Path) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            t = page.extract_text()
            if not t:
                continue
            for raw in t.splitlines():
                s = raw.strip()
                if s:
                    lines.append((page_index, s))
    return lines


def _all_lines_txt(path: Path) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for page_index, part in enumerate(_read_txt_as_pages(path), start=1):
        for raw in part.splitlines():
            s = raw.strip()
            if s:
                lines.append((page_index, s))
    return lines


def _all_lines_for_path(path: Path) -> list[tuple[int, str]]:
    suf = path.suffix.lower()
    if suf == ".txt":
        return _all_lines_txt(path)
    if suf == ".pdf":
        return _all_lines(path)
    raise SystemExit(f"Unsupported file type (use .pdf or .txt): {path}")


def _finish_res_ord_title(
    i_after_ag: int, lines: list[tuple[int, str]], m: re.Match[str]
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
            continue

        i += 1
    return out


def _should_exclude_adjourn_or_closed_session(block: MotionBlock) -> bool:
    """
    True for end-of-meeting adjourn, move to closed/executive session, etc. —
    routine procedural motions the user does not want in the export.
    """
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
        r"adjourn the meeting|moved to adjourn the meeting|"
        r"adjourn at \d{1,2}:\d{2}|moved to adjourn at",
        t,
    ) and "closed session" not in t:
        return True
    return False


def _refine_motion_anchors(block: MotionBlock, motion_text: str) -> None:
    """Re-label consent batches, closed-session moves, and final adjournment."""
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


def extract_rows(
    doc_path: Path,
) -> tuple[list[dict], list[dict], list[MotionBlock]]:
    mdate = meeting_date_from_text(full_text_for_date(doc_path))
    ords: list[dict] = []
    ress: list[dict] = []
    if doc_path.suffix.lower() == ".txt":
        for pnum, part in enumerate(_read_txt_as_pages(doc_path), start=1):
            for line in part.splitlines():
                s = line.strip()
                if not s:
                    continue
                if _ORD.search(s):
                    ords.append(
                        {
                            "source_file": doc_path.name,
                            "meeting_date": mdate,
                            "page": pnum,
                            "line_text": s,
                        }
                    )
                if _RES.search(s):
                    ress.append(
                        {
                            "source_file": doc_path.name,
                            "meeting_date": mdate,
                            "page": pnum,
                            "line_text": s,
                        }
                    )
    else:
        with pdfplumber.open(doc_path) as pdf:
            for pnum, page in enumerate(pdf.pages, start=1):
                text = page.extract_text()
                if not text:
                    continue
                for line in text.splitlines():
                    s = line.strip()
                    if not s:
                        continue
                    if _ORD.search(s):
                        ords.append(
                            {
                                "source_file": doc_path.name,
                                "meeting_date": mdate,
                                "page": pnum,
                                "line_text": s,
                            }
                        )
                    if _RES.search(s):
                        ress.append(
                            {
                                "source_file": doc_path.name,
                                "meeting_date": mdate,
                                "page": pnum,
                                "line_text": s,
                            }
                        )
    al = _all_lines_for_path(doc_path)
    return ords, ress, _parse_motions(doc_path.name, al, mdate)


OUT_DIR = Path("output")
DEFAULT_XLSX = OUT_DIR / "eagle_mountain_extract.xlsx"


SUPPORTED_EXTS = frozenset({".pdf", ".txt"})


def collect_input_paths(target: Path) -> list[Path]:
    if target.is_file():
        if target.suffix.lower() in SUPPORTED_EXTS:
            return [target.resolve()]
        raise SystemExit(f"Expected a .pdf or .txt file, got: {target}")
    if target.is_dir():
        files = [
            p
            for p in target.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
        ]
        return sorted(files, key=lambda p: (p.as_posix().lower(), p.name))
    raise SystemExit(f"Not a file or directory: {target}")


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def unique_by_content(
    paths: list[Path],
) -> tuple[list[Path], list[tuple[Path, Path]]]:
    """
    Deduplicate by file contents (SHA-256). The first file in the given order
    is kept; later files with the same content are skipped.
    Returns (paths_to_process, list of (skipped_path, kept_path)).
    """
    if not paths:
        return [], []
    if len(paths) == 1:
        return paths, []
    seen: dict[str, Path] = {}
    unique: list[Path] = []
    skipped: list[tuple[Path, Path]] = []
    for p in paths:
        key = _file_sha256(p)
        if key in seen:
            skipped.append((p, seen[key]))
            continue
        seen[key] = p
        unique.append(p)
    return unique, skipped


def main() -> None:
    p = argparse.ArgumentParser(
        description="Extract ordinances, resolutions, and roll-call votes to Excel."
    )
    p.add_argument(
        "path",
        type=Path,
        help="Path to a .pdf / .txt file, or a folder to scan for .pdf and .txt files",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_XLSX,
        help=f"Output .xlsx path (default: {DEFAULT_XLSX})",
    )
    args = p.parse_args()

    target = args.path
    if not target.exists():
        raise SystemExit(f"Path does not exist: {target}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.output.parent and str(args.output.parent) != ".":
        args.output.parent.mkdir(parents=True, exist_ok=True)

    all_ords: list[dict] = []
    all_ress: list[dict] = []
    all_motions: list[MotionBlock] = []

    collected = collect_input_paths(target)
    to_process, skip_pairs = unique_by_content(collected)
    for duplicate, kept in skip_pairs:
        print(
            f"Skipped duplicate (same content as {kept.name!r}): {duplicate!s}"
        )
    for path in to_process:
        o, r, m = extract_rows(path)
        all_ords.extend(o)
        all_ress.extend(r)
        all_motions.extend(m)

    df_o = pd.DataFrame(
        all_ords,
        columns=["source_file", "meeting_date", "page", "line_text"],
    )
    df_r = pd.DataFrame(
        all_ress,
        columns=["source_file", "meeting_date", "page", "line_text"],
    )
    summ = [b.to_summary_row() for b in all_motions]
    long: list[dict] = []
    for b in all_motions:
        long.extend(b.to_member_rows())
    df_s = pd.DataFrame(summ)
    df_m = pd.DataFrame(long)

    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        df_s.to_excel(writer, sheet_name="motions", index=False)
        df_m.to_excel(writer, sheet_name="votes_by_member", index=False)
        df_o.to_excel(writer, sheet_name="ordinances", index=False)
        df_r.to_excel(writer, sheet_name="resolutions", index=False)

    nmv = len(long)
    out_msg = (
        f"Wrote {len(all_ords)} ord. line(s), {len(all_ress)} res. line(s), "
        f"{len(all_motions)} motion(s) ({nmv} member row(s)) from "
        f"{len(to_process)} file(s) -> {args.output}"
    )
    if skip_pairs:
        out_msg += f" (skipped {len(skip_pairs)} duplicate file(s))"
    print(out_msg)


if __name__ == "__main__":
    main()
