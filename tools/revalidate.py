#!/usr/bin/env python3
"""Re-validate a run's *.fuzz programs against a specific compiler binary.

When the System-Under-Test compiler changes (e.g. GCC 11.5 -> GCC 14), a run
validated on the old compiler is no longer comparable. This recompiles each
*.fuzz with the given compiler using the SAME command the fuzzer uses
(`<compiler> -std=c++23 -x c++ <file> -o <tmp>`) and reports a fresh tally in
the same metric format as compare_runs.py. The run directory is NOT modified.

Usage:
    python tools/revalidate.py RUN_DIR COMPILER [-j N]
    python tools/revalidate.py outputs/huf_saem_1phase_10k_run2 gcc14-g++ -j 6
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compare_runs import _BUG_RE, _FAIL_PATTERNS  # reuse categorization

TIMEOUT_S = 20


def compile_one(args):
    compiler, path = args
    out = tempfile.NamedTemporaryFile(suffix=".out", delete=False).name
    try:
        proc = subprocess.run(
            [compiler, "-std=c++23", "-x", "c++", path, "-o", out],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
        rc, stderr = proc.returncode, proc.stderr
    except subprocess.TimeoutExpired:
        rc, stderr = 124, "timeout"
    except FileNotFoundError:
        print(f"ERROR: compiler not found: {compiler}", file=sys.stderr)
        raise SystemExit(2)
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
    return rc, stderr


def categorize(stderr: str) -> str:
    first = stderr.splitlines()[0] if stderr.strip() else ""
    for name, pat in _FAIL_PATTERNS:
        if pat.search(first) or pat.search(stderr[:500]):
            return name
    return "other"


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("compiler")
    ap.add_argument("-j", "--jobs", type=int, default=4)
    ns = ap.parse_args(argv[1:])

    fuzz = sorted(
        f.path for f in os.scandir(ns.run_dir) if f.name.endswith(".fuzz")
    )
    if not fuzz:
        print(f"no .fuzz files in {ns.run_dir}", file=sys.stderr)
        return 1

    safe = failed = bug = 0
    cats: Counter = Counter()
    work = [(ns.compiler, p) for p in fuzz]
    done = 0
    with ThreadPoolExecutor(max_workers=ns.jobs) as ex:
        for rc, stderr in ex.map(compile_one, work):
            done += 1
            if rc == 0:
                safe += 1
            else:
                failed += 1
                cats[categorize(stderr)] += 1
                if _BUG_RE.search(stderr):
                    bug += 1
            if done % 1000 == 0:
                print(f"  ... {done}/{len(fuzz)}", file=sys.stderr)

    total = safe + failed
    label = os.path.basename(ns.run_dir.rstrip("/"))
    w = 26
    print(f"\nRE-VALIDATION of {label} on {ns.compiler} (-std=c++23)")
    print("-" * (w + 18))
    print(f"{'programs':<{w}}{total:>18}")
    print(f"{'compiled safe':<{w}}{safe:>18}")
    print(f"{'compile-success rate':<{w}}{safe/total*100:>17.1f}%")
    print(f"{'failed':<{w}}{failed:>18}")
    print(f"{'COMPILER-BUG signals':<{w}}{bug:>18}")
    if cats:
        print("\nFAILURE CATEGORY")
        for c, n in cats.most_common():
            print(f"{c:<{w}}{n:>18}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
