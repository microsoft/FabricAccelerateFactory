from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import DEFAULT_CONFIG
from .converter import convert_file
from .parser import HEADER
from .report import pct, totals, write_reports
from .task_values import helper_references

SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".ipynb_checkpoints"}


def create_run_directory(base: Path, input_root: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d%H%M")
    suffix = 0
    while True:
        collision_suffix = f"_{suffix:02d}" if suffix else ""
        candidate = base.with_name(f"{base.name}_{timestamp}{collision_suffix}")
        if candidate == input_root or input_root in candidate.parents or candidate in input_root.parents:
            raise ValueError(f"output folder overlaps the input folder: {candidate}")
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            if not candidate.exists() and not candidate.is_symlink():
                raise
            suffix += 1


def discover(root: Path, include_plain: bool):
    found, skipped = [], []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and not d.endswith("_fabric"))
        for name in sorted(files):
            p = Path(dirpath) / name
            ext = p.suffix.lower()
            rel = p.relative_to(root).as_posix()
            if ext not in (".py", ".sql"):
                if ext in (".scala", ".r", ".ipynb", ".dbc"):
                    skipped.append((rel, f"{ext} is not supported by this version (supported: Databricks .py / .sql exports)"))
                continue
            try:
                head = p.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff").split("\n", 1)[0].strip()
            except OSError as e:
                skipped.append((rel, f"unreadable: {e}"))
                continue
            if HEADER.match(head) or include_plain:
                found.append(p)
            else:
                skipped.append((rel, "no 'Databricks notebook source' header: not a notebook export (use --include-plain to convert it as a single-cell notebook)"))
    return found, skipped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="adb2fabric", description="Convert Azure Databricks notebook exports (.py/.sql) into Fabric notebooks (.ipynb) with a coverage report.")
    ap.add_argument("input", help="folder containing Databricks notebooks (searched recursively)")
    ap.add_argument("-o", "--output", help="output folder prefix (default: <input>_fabric next to the input folder); each run appends _YYYYMMDDHHMM")
    ap.add_argument("--include-plain", action="store_true", help="also convert .py/.sql files that lack the Databricks header")
    ap.add_argument("--dry-run", action="store_true", help="analyse and report only; do not write notebooks")
    ap.add_argument("--no-open", action="store_true", help="do not open the HTML report")
    ap.add_argument("--fail-below", type=float, default=None, metavar="PCT", help="exit with code 3 when weighted construct coverage is below PCT (for CI; not notebook completion)")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)

    root = Path(a.input).expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR: input folder not found: {root}", file=sys.stderr)
        return 2
    try:
        output_base = Path(a.output).expanduser().resolve() if a.output else root.with_name(root.name + "_fabric")
        if not output_base.name:
            raise ValueError("output folder prefix must have a name")
    except (OSError, RuntimeError, ValueError) as e:
        print(f"ERROR: invalid output folder prefix: {e}", file=sys.stderr)
        return 2
    notebooks, skipped = discover(root, a.include_plain)
    if not notebooks:
        print(f"No Databricks notebooks found under {root}.")
        for f, r in skipped[:10]:
            print(f"  skipped {f}: {r}")
        return 1
    try:
        out_root = create_run_directory(output_base, root)
    except (OSError, ValueError) as e:
        print(f"ERROR: cannot create output folder: {e}", file=sys.stderr)
        return 2
    shared_helpers = helper_references(root)
    results = []
    for p in notebooks:
        try:
            results.append(convert_file(p, root, out_root, write=not a.dry_run, shared_helpers=shared_helpers))
        except Exception as e:  # keep going: one bad file must not stop the batch
            skipped.append((p.relative_to(root).as_posix(), f"conversion error: {type(e).__name__}: {e}"))
    report = write_reports(results, skipped, root, out_root)
    t = totals(results)
    print(f"\nadb2fabric {__version__}: {t['notebooks']} notebook(s)  ->  {out_root}")
    print(f"  output folder    : {out_root}")
    print(f"  rules config    : {DEFAULT_CONFIG}")
    print(f"  fully converted : {t['converted']}")
    print(f"  need review     : {t['review']}")
    print(f"  need manual work: {t['manual']}")
    print(f"  constructs      : {t['constructs']} ({t['auto']} auto, {t['review_items']} review, {t['manual_items']} manual)")
    print(f"  notebook completion: {pct(t['notebook_completion'])} ({t['converted']} of {t['notebooks']} fully converted)")
    print(f"  construct coverage : {pct(t['weighted_coverage'])} weighted, {pct(t['strict_coverage'])} auto-only")
    print(f"  code lines         : {pct(t['line_coverage'])} without manual flags (includes review lines)")
    if skipped:
        print(f"  skipped         : {len(skipped)} (see report)")
    print(f"  report          : {report}")
    if not a.no_open and not os.environ.get("ADB2FABRIC_NO_OPEN"):
        try:
            webbrowser.open(report.as_uri())
        except Exception:
            pass
    if a.fail_below is not None and t["weighted_coverage"] * 100 < a.fail_below:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
