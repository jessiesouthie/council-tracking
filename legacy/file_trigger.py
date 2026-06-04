"""
Watch a folder and run extract_meeting.py when new .pdf / .txt files appear
or change. Uses only the Python stdlib (no extra packages; polls the directory).

  python file_trigger.py
  python file_trigger.py --watch data/pdfs -o output/eagle_mountain_extract.xlsx
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_EXTRACT = SCRIPT_DIR / "extract_meeting.py"
DEFAULT_WATCH = PROJECT_ROOT / "data" / "pdfs"
DEFAULT_OUT = PROJECT_ROOT / "output" / "eagle_mountain_extract.xlsx"

SUFFIXES = {".pdf", ".txt"}


def _snapshot(watch: Path, recursive: bool) -> frozenset[tuple[str, int, int]]:
    """
    (resolved path, size, mtime_ns) for every matching file.
    Ignores temp/hidden files (e.g. ._, ~$).
    """
    rows: list[tuple[str, int, int]] = []
    it = watch.rglob("*") if recursive else watch.iterdir()
    for p in it:
        if not p.is_file():
            continue
        if p.name.startswith((".", "~$")):
            continue
        if p.suffix.lower() not in SUFFIXES:
            continue
        st = p.stat()
        rows.append((str(p.resolve()), st.st_size, st.st_mtime_ns))
    return frozenset(rows)


def _run_extract(extract: Path, watch: Path, output: Path) -> int:
    cmd = [
        sys.executable,
        str(extract),
        str(watch),
        "-o",
        str(output),
    ]
    print("Running:", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if r.returncode != 0:
        print(
            f"extract_meeting.py exited with code {r.returncode}",
            file=sys.stderr,
        )
    return r.returncode


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Poll a directory; after .pdf / .txt files change, wait, then run "
            "the council minutes extractor (debounced)."
        )
    )
    p.add_argument(
        "--watch",
        type=Path,
        default=DEFAULT_WATCH,
        help=f"Directory to watch (default: {DEFAULT_WATCH})",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output .xlsx for extract_meeting.py (default: {DEFAULT_OUT})",
    )
    p.add_argument(
        "--extract",
        type=Path,
        default=DEFAULT_EXTRACT,
        help=f"Path to extract_meeting.py (default: {DEFAULT_EXTRACT})",
    )
    p.add_argument(
        "--debounce",
        type=float,
        default=2.0,
        help="Wait this many seconds after the folder stops changing (default: 2)",
    )
    p.add_argument(
        "--poll",
        type=float,
        default=0.5,
        help="Seconds between directory checks (default: 0.5)",
    )
    p.add_argument(
        "--no-recursive",
        action="store_true",
        help="Only look at the top of the watch folder, not subfolders",
    )
    args = p.parse_args()

    w = args.watch.resolve()
    if not w.is_dir():
        print(f"Watch path is not a directory: {w}", file=sys.stderr)
        sys.exit(1)
    if not args.extract.is_file():
        print(f"Extract script not found: {args.extract}", file=sys.stderr)
        sys.exit(1)

    w.mkdir(parents=True, exist_ok=True)
    out = args.output
    out.parent.mkdir(parents=True, exist_ok=True)

    deb = max(0.5, float(args.debounce))
    poll = max(0.1, float(args.poll))
    rec = not args.no_recursive

    # Baseline: no run until the folder’s contents (names/sizes/mtimes) change
    last_built: frozenset[tuple[str, int, int]] = _snapshot(w, rec)
    # Snapshot we are "waiting" to see stable; deadline when we can run
    pending: frozenset[tuple[str, int, int]] | None = None
    deadline: float = 0.0

    how = "including subfolders" if rec else "top-level only"
    print(
        f"Polling {w} ({how}) for .pdf / .txt every {poll}s; "
        f"debounce {deb}s. Baseline {len(last_built)} file(s), then run on changes. "
        f"Ctrl+C to stop. Output -> {out}",
        flush=True,
    )

    try:
        while True:
            time.sleep(poll)
            snap = _snapshot(w, rec)
            if last_built is not None and snap == last_built:
                continue

            if snap != pending:
                pending = snap
                deadline = time.time() + deb
            elif time.time() >= deadline:
                _run_extract(args.extract, w, out)
                last_built = snap
                pending = None
    except KeyboardInterrupt:
        print("Stopped.", flush=True)


if __name__ == "__main__":
    main()
