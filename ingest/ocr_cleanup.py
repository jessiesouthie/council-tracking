"""
Repair the OCR text layer of scanned minutes (2019–2024, mostly).

The clerk's scanned PDFs carry a machine-OCR text layer that misreads a
handful of glyphs consistently: i → í/Í/ì, a → ø, y → g, "is" → ß, b/ig → þ,
0 → O and 1 → | inside code sections. pdfplumber faithfully extracts that
layer, so "Councilmember Wood moved to approve an ordinance of Eagle Mountain
City" arrives as "Councílmember Wood moved to approve an ordínance of Eagle
MountaÍn Citg".

Repair is dictionary-guided: for each word that isn't already a known word,
generate the readings each suspect glyph could stand for and keep the one most
common in the clean (un-garbled) motions, falling back to /usr/share/dict.
Clean text — anything without a tell-tale glyph — passes through untouched, so
newer born-digital minutes are never rewritten.

  clean_text(s) -> str
"""

from __future__ import annotations

import glob
import itertools
import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Glyphs that never appear in real minutes text but are routine OCR misreads.
# Each maps to the readings it has been seen standing for, likeliest first.
GLYPHS: dict[str, tuple[str, ...]] = {
    "í": ("i",), "ì": ("i",), "î": ("i", "1"), "ï": ("i", "f"), "Ì": ("i",),
    "Í": ("i", "t", "I"), "Ï": ("i", "T"), "¡": ("i", "I"), "¿": ("u", "a", "i"),
    "ø": ("a", "o", "u"), "Ø": ("A",), "å": ("a",), "â": ("a",), "á": ("a",),
    "ä": ("a",), "ã": ("a",),
    "ß": ("is", "ss", "B"), "þ": ("b", "ig", "p", "ty", "gh"), "Þ": ("P", "E"),
    "ü": ("u", "ti", "i"), "Ü": ("A", "U"), "ú": ("u", "W", "C"), "û": ("i", "u"),
    "ù": ("d", "u", "m"), "ñ": ("n", "r", "m"),
    "ê": ("e", "G"), "è": ("e",), "Ë": ("E",), "Ê": ("f", "e"), "€": ("e",),
    "Ð": ("D",), "Ç": ("C",), "|": ("l", "I", "1"),
}
TELL = re.compile("[" + re.escape("".join(GLYPHS)) + "]")
# Letters, the suspect glyphs, and apostrophes inside a word. Digits excluded so
# code sections are handled separately.
WORD = re.compile(r"[A-Za-zÀ-ÿ|¡¿€]+")
# "|7.7O.O3O", "î635.ßO(a)" — section numbers with O for 0 and |/î for 1.
SECTION = re.compile(r"(?<![\w.])[|îIl]?[\dO|î]+(?:\.[\dOß|î]+)+(?![\w])")

MAX_CANDIDATES = 4096

# Plain-ASCII letters this scan confuses ("Coancilmember", "Cily", "lhe",
# "unsnimous"). Tried one at a time, only on words that aren't known at all.
CONFUSABLE: dict[str, str] = {
    "a": "ousne", "o": "aue", "u": "aon", "s": "a", "n": "aur", "l": "ti",
    "t": "lf", "e": "co", "c": "e", "i": "l", "r": "n", "f": "t", "h": "b",
}


def _token_counts(text: str) -> Counter:
    return Counter(w.lower() for w in re.findall(r"[A-Za-z]+", text))


VOCAB_FILE = ROOT / "data" / "meta" / "ocr_vocab.json"


def _dict_words() -> frozenset:
    try:
        return frozenset(w.strip().lower() for w in open("/usr/share/dict/words"))
    except OSError:
        return frozenset()


def _site_texts():
    for path in sorted(glob.glob(str(ROOT / "docs" / "data*.json"))):
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for m in data.get("motions") or []:
            for f in ("motion", "item_title", "outcome", "headline", "summary"):
                if m.get(f):
                    yield m[f]
        for mem in data.get("members") or []:
            yield from [mem.get("name") or ""] * 50


def build_vocab() -> Counter:
    """Word counts from every clean (glyph-free) motion on the site and member
    names, plus the dictionary words that some garbled word could be read as.
    Frozen to VOCAB_FILE — the weekly CI ingest has no /usr/share/dict, and a
    repair that drifted week to week would change the motion's summary-cache key."""
    corpus: Counter = Counter()
    garbled: set[str] = set()
    for t in _site_texts():
        if TELL.search(t):
            garbled.update(WORD.findall(t))
        else:
            corpus.update(_token_counts(t))
    words = _dict_words()
    for w in garbled:
        for cand in _candidates(w, swap_g=True, swap_q=True):
            c = cand.lower()
            if c in words and c not in corpus:
                corpus[c] = 0.5
    return corpus


@lru_cache(maxsize=1)
def _vocab() -> Counter:
    if VOCAB_FILE.exists():
        return Counter(json.loads(VOCAB_FILE.read_text())["counts"])
    return build_vocab()


def _score(word: str) -> float:
    return _vocab().get(word.lower(), 0.0)


def _match_case(src: str, out: str) -> str:
    letters = [c for c in src if c.isalpha()]
    if len(letters) > 1 and all(c.isupper() for c in letters):
        return out.upper()
    if src[:1].isupper() or src[:1] in "ØÜÐÇËÞ":
        return out[:1].upper() + out[1:]
    return out


def _candidates(word: str, swap_g: bool, swap_q: bool):
    opts: list[tuple[str, ...]] = []
    for c in word:
        if c in GLYPHS:
            opts.append(GLYPHS[c])
        elif swap_g and c in "gG":
            # y and q both lose their tail to g in this scan ("Citg", "reguire").
            # q only for words that are otherwise unknown, or "guest" turns "quest".
            opts.append(((c, "y", "q") if swap_q else (c, "y")) if c == "g" else (c, "Y"))
        else:
            opts.append((c,))
    for combo in itertools.islice(itertools.product(*opts), MAX_CANDIDATES):
        yield "".join(combo)


# Real words that a g/y swap would otherwise turn into a commoner one
# ("years of age" is not "years of aye").
KEEP = {"age", "ages"}


def _fix_word(word: str) -> str:
    if word.lower() in KEEP:
        return word
    has_glyph = bool(TELL.search(word))
    base = _score(word)
    if not has_glyph and base >= 3:
        # A common word; only "y read as g" can still be hiding here
        # ("mag" for may, "bg" for by), and only when the y form dominates.
        if "g" not in word.lower():
            return word
    best, best_score = word, (base if not has_glyph else -1.0)
    for cand in _candidates(word, swap_g=True, swap_q=has_glyph or base == 0):
        s = _score(cand)
        # Swapping a real word for another needs a decisive margin.
        if not has_glyph and s <= max(5 * base, 2):
            continue
        if s > best_score:
            best, best_score = cand, s
    if best_score <= 0 and has_glyph:
        # Unknown word (a surname, a typo in the original): take each glyph's
        # likeliest reading and leave g alone.
        best = "".join(GLYPHS[c][0] if c in GLYPHS else c for c in word)
    if _score(best) == 0 and len(best) >= 3 and not best.isupper():
        # One misread plain letter left ("Coancilmember", "Cily"). The fix has
        # to be a common word, so a rare surname isn't "corrected" into one.
        low = best.lower()
        swaps = {
            low[:i] + r + low[i + 1:]
            for i, c in enumerate(low) for r in CONFUSABLE.get(c, "")
        }
        top = max(swaps, key=_score, default="")
        if top and _score(top) >= 3:
            best = _match_case(best, top)
    if _score(best) < 3:
        # Letter pairs the OCR fused or split: "rn" for m ("Burnharn"), a
        # leading "ll" for U ("lltah"), "cl" for d ("ancl"). The misreads recur
        # often enough to be in the corpus themselves, so demand a clear margin.
        for a, b in (("rn", "m"), ("ll", "U"), ("cl", "d")):
            if a == "ll":
                alt = b + best[2:] if best.startswith("ll") else best
            else:
                alt = best.replace(a, b)
            if alt != best and _score(alt) > max(5 * _score(best), 2):
                best = alt
                break
    return _match_case(word, best) if best != word else word


def _fix_section(m: re.Match) -> str:
    s = m.group(0)
    if not re.search(r"\d", s):
        return s
    out = s.translate(str.maketrans({"O": "0", "|": "1", "î": "1", "ß": "3"}))
    if out[:1] in "Il" and len(out) > 1 and out[1].isdigit():
        out = "1" + out[1:]
    return out


# The roll-call grid the clerk prints under each motion ("Those Voting Yes Those
# Voting No … ☒ Donna Burnham ☐ Donna Burnham …"). OCR turns its checkboxes into
# "E tr tr ü", and the votes are already parsed out of it.
GRID = re.compile(r"\s*Those Voting (?:Yes|Aye)\b.*?(?=[Tt]he motion (?:passed|failed|carried|was)\b|$)", re.S)
# "The motion passed with a unanimous vote." Anything after it is the next
# agenda item that the parser's split ran into.
OUTCOME = re.compile(r"\b[Tt]he motion (?:passed|failed|carried|was)\b[^.]*\.")


def trim_motion(text: str, drop_grid: bool) -> str:
    """Cut a motion's text back to the motion: drop the roll-call grid (when its
    votes were parsed, or OCR left it unreadable) and whatever ran on past the
    outcome sentence."""
    if drop_grid:
        text = GRID.sub(" ", text, count=1)
    m = OUTCOME.search(text)
    if m:
        text = text[: m.end()]
    return re.sub(r"\s{2,}", " ", text).strip()


def is_garbled(text: str) -> bool:
    return bool(text and TELL.search(text))


def clean_text(text: str) -> str:
    if not is_garbled(text):
        return text
    text = SECTION.sub(_fix_section, text)
    return WORD.sub(lambda m: _fix_word(m.group(0)), text)


if __name__ == "__main__":
    # python -m ingest.ocr_cleanup   — refreeze the vocabulary (rarely needed;
    # changes repairs, and so summary-cache keys, for some scanned motions).
    counts = build_vocab()
    VOCAB_FILE.write_text(json.dumps({
        "comment": "Word counts used by ingest.ocr_cleanup to repair scanned "
                   "minutes. Regenerate with `python -m ingest.ocr_cleanup`.",
        "counts": dict(sorted(counts.items())),
    }, ensure_ascii=False, indent=0))
    print(f"wrote {VOCAB_FILE.relative_to(ROOT)} ({len(counts)} words)")
