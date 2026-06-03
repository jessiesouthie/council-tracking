"""
One-shot sanity check: confirm docs/parser.py produces the same rows as the
legacy CLI for every file in data/pdfs/. Used during the PWA migration.

  python legacy/_parity_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "docs"))

import parser as new_parser  # noqa: E402

DATA_DIR = ROOT / "data" / "pdfs"
BASELINE = ROOT / "output" / "baseline.xlsx"

assert BASELINE.exists(), f"Run legacy CLI first to produce {BASELINE}"

sheets = {
    name: pd.read_excel(BASELINE, sheet_name=name).fillna("")
    for name in ("motions", "votes_by_member", "ordinances", "resolutions")
}

new_motions: list[dict] = []
new_members: list[dict] = []
new_ords: list[dict] = []
new_ress: list[dict] = []

files = sorted(
    [p for p in DATA_DIR.rglob("*") if p.suffix.lower() in {".pdf", ".txt"} and p.is_file()],
    key=lambda p: (p.as_posix().lower(), p.name),
)

# Mirror the legacy CLI's content-dedup so totals match.
import hashlib

def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

seen: dict[str, Path] = {}
unique: list[Path] = []
for p in files:
    key = sha(p)
    if key in seen:
        continue
    seen[key] = p
    unique.append(p)

for p in unique:
    data = p.read_bytes()
    r = new_parser.parse_document(p.name, data)
    new_motions.extend(r["motions"])
    new_members.extend(r["votes_by_member"])
    new_ords.extend(r["ordinances"])
    new_ress.extend(r["resolutions"])


def compare(label: str, base_df: pd.DataFrame, new_rows: list[dict]) -> bool:
    base = base_df.fillna("").astype(str).to_dict("records")
    new = pd.DataFrame(new_rows).fillna("").astype(str).to_dict("records")
    if len(base) != len(new):
        print(f"  {label}: COUNT MISMATCH  baseline={len(base)}  new={len(new)}")
        return False
    diffs = 0
    for i, (b, n) in enumerate(zip(base, new)):
        if b != n:
            diffs += 1
            if diffs <= 3:
                print(f"  {label} row {i}: differs")
                for k in sorted(set(b) | set(n)):
                    if b.get(k, "") != n.get(k, ""):
                        print(f"    {k!r}: baseline={b.get(k, '')!r}  new={n.get(k, '')!r}")
    if diffs:
        print(f"  {label}: {diffs} row(s) differ")
        return False
    print(f"  {label}: OK ({len(base)} rows match)")
    return True


print(f"Comparing {len(unique)} file(s) against baseline at {BASELINE.name}")
ok = True
ok &= compare("motions", sheets["motions"], new_motions)
ok &= compare("votes_by_member", sheets["votes_by_member"], new_members)
ok &= compare("ordinances", sheets["ordinances"], new_ords)
ok &= compare("resolutions", sheets["resolutions"], new_ress)

sys.exit(0 if ok else 1)
