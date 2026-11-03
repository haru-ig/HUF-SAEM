#!/usr/bin/env python3
"""Re-check saved oracle findings against the current oracle.

A finding is only as good as the oracle that produced it, and this one has
been corrected several times mid-campaign -- a repaired sanitizer gate, a
real input stream, PIE binaries. Rather than restart a multi-hour run every
time, let it finish and replay its findings through the oracle as it stands
now. Re-checking is cheap because the candidate set is small; the sweep that
produced it is not.

Reads <findings_dir>/<kind>/<hash>/program.cpp, re-runs each through
DifferentialOracle, and reports which verdicts survive.

Usage:
    python tools/triage_findings.py outputs/oracle_findings/phase1_on
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Fuzz4All.huf_saem.oracle import DifferentialOracle  # noqa: E402


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("findings_dirs", nargs="+", help="oracle --out directories to re-check")
    p.add_argument("--compilers", nargs="+", default=["gcc14-g++"])
    p.add_argument("--opt-levels", nargs="+", default=["-O0", "-O1", "-O2", "-O3"])
    p.add_argument("-j", "--jobs", type=int, default=4)
    p.add_argument("--out", default=None,
                   help="save surviving findings here (default: report only)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    oracle = DifferentialOracle(
        compilers=args.compilers,
        opt_levels=args.opt_levels,
        bug_dir=args.out,
        jobs=1,
    )
    if not oracle.available:
        print(f"ERROR: none of {args.compilers} found on PATH", file=sys.stderr)
        return 2

    print(f"re-checking with: {', '.join(c.name for c in oracle.configs)}")
    print(f"flags: {' '.join(oracle.extra_flags)}\n")

    survivors, total = [], Counter()

    for findings_dir in args.findings_dirs:
        programs = sorted(glob.glob(os.path.join(findings_dir, "*", "*", "program.cpp")))
        if not programs:
            print(f"{findings_dir}: no findings, skipping")
            continue

        print(f"=== {findings_dir}  ({len(programs)} findings) ===")
        tally = Counter()

        def recheck(path):
            # The original verdict is the name of the directory two levels up.
            was = os.path.basename(os.path.dirname(os.path.dirname(path)))
            return path, was, oracle.check(path)

        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for path, was, verdict in pool.map(recheck, programs):
                tally[f"{was} -> {verdict.kind}"] += 1
                if verdict.is_bug:
                    survivors.append((path, was, verdict))

        for transition, n in sorted(tally.items()):
            mark = "  " if " -> safe" in transition or "nondeterministic" in transition else "* "
            print(f"  {mark}{transition:<44} {n:>4}")
        print()
        total.update(tally)

    kept = len(survivors)
    print(f"=== {sum(total.values())} findings re-checked, {kept} still reported ===")
    for path, was, verdict in survivors[:50]:
        print(f"  {path}\n      was {was}, now [{verdict.kind}] {verdict.summary}")
    if kept > 50:
        print(f"  ... and {kept - 50} more")
    return 1 if kept else 0


if __name__ == "__main__":
    raise SystemExit(main())
