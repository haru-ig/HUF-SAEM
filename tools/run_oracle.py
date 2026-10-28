#!/usr/bin/env python3
"""Replay an existing corpus through the Tier-1 bug oracle.

Every run under outputs/ was validated with a compile-only, -O0 oracle, so a
miscompilation or an optimizer ICE in those programs was never looked for.
This re-checks a corpus without re-fuzzing: each *.fuzz is compiled under
several (compiler, -O level) configurations, every binary is run, and the
outputs are compared. The run directory is never modified; artefacts go to
--out.

Usage:
    python tools/run_oracle.py RUN_DIR [RUN_DIR ...] [options]

    python tools/run_oracle.py outputs/huf_saem_1phase_run2 \
        --opt-levels -O0 -O1 -O2 -O3 -j 6

Exit status is 1 when at least one bug candidate was found.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Fuzz4All.huf_saem.oracle import DifferentialOracle  # noqa: E402


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run_dirs", nargs="+", help="directories containing *.fuzz programs")
    p.add_argument("--compilers", nargs="+", default=["g++"], help="compilers to cross-check (default: g++)")
    p.add_argument("--opt-levels", nargs="+", default=["-O0", "-O1", "-O2", "-O3"])
    p.add_argument("--std", default="c++23")
    p.add_argument("--out", default="outputs/oracle_findings", help="where bug artefacts are written")
    p.add_argument("-j", "--jobs", type=int, default=4, help="programs checked in parallel")
    p.add_argument("--limit", type=int, default=0, help="stop after N programs per directory (0 = all)")
    p.add_argument("--run-timeout", type=float, default=5.0)
    p.add_argument("--compile-timeout", type=float, default=30.0)
    p.add_argument("--no-sanitize", action="store_true", help="skip the UB gate (faster, noisier)")
    p.add_argument("--cross-compiler-rejects", action="store_true",
                   help="also report programs one compiler accepts and another rejects")
    p.add_argument("-v", "--verbose", action="store_true", help="print every non-safe verdict")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    oracle = DifferentialOracle(
        compilers=args.compilers,
        opt_levels=args.opt_levels,
        std=args.std,
        compile_timeout=args.compile_timeout,
        run_timeout=args.run_timeout,
        sanitize=not args.no_sanitize,
        cross_compiler_reject_is_bug=args.cross_compiler_rejects,
        bug_dir=args.out,
        jobs=1,  # parallelism is across programs, not across configurations
    )
    if not oracle.available:
        print(f"ERROR: none of {args.compilers} found on PATH", file=sys.stderr)
        return 2
    missing = [c for c in args.compilers if c not in oracle.compilers]
    if missing:
        print(f"warning: skipping unavailable compiler(s): {', '.join(missing)}", file=sys.stderr)

    print(f"configurations: {', '.join(c.name for c in oracle.configs)}")
    print(f"UB gate: {'off' if args.no_sanitize else 'on'}   artefacts: {args.out}\n")

    grand_total = Counter()
    found = []

    for run_dir in args.run_dirs:
        files = sorted(glob.glob(os.path.join(run_dir, "*.fuzz")),
                       key=lambda p: int(os.path.basename(p).split(".")[0])
                       if os.path.basename(p).split(".")[0].isdigit() else 0)
        if args.limit:
            files = files[: args.limit]
        if not files:
            print(f"{run_dir}: no *.fuzz files, skipping")
            continue

        print(f"=== {run_dir}  ({len(files)} programs) ===")
        tally = Counter()
        started = time.time()

        def check(path):
            return path, oracle.check(path)

        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for done, (path, verdict) in enumerate(pool.map(check, files), 1):
                tally[verdict.kind] += 1
                if verdict.is_bug:
                    found.append((path, verdict))
                    print(f"\n  *** {os.path.basename(path)}: {verdict.summary} [{verdict.kind}]")
                    if verdict.saved_to:
                        print(f"      saved to {verdict.saved_to}")
                elif args.verbose and verdict.kind != "safe":
                    print(f"  {os.path.basename(path)}: {verdict.kind} -- {verdict.summary}")
                if done % 100 == 0:
                    rate = done / max(1e-9, time.time() - started)
                    print(f"  {done}/{len(files)}  ({rate:.1f} prog/s)")

        elapsed = time.time() - started
        print(f"\n  {len(files)} programs in {elapsed/60:.1f} min")
        for kind, n in tally.most_common():
            print(f"    {kind:<20} {n:>6}  ({100.0 * n / len(files):.1f}%)")
        print()
        grand_total.update(tally)

    print("=== total ===")
    for kind, n in grand_total.most_common():
        print(f"  {kind:<20} {n:>6}")
    bugs = sum(n for k, n in grand_total.items() if k in
               {"ice", "compiler_signal", "compile_hang", "wrong_code", "hang_divergence", "reject_divergence"})
    print(f"\nbug candidates: {bugs}")
    if found:
        print("\nprograms to look at:")
        for path, verdict in found[:50]:
            print(f"  {path}  [{verdict.kind}] {verdict.summary}")
        if len(found) > 50:
            print(f"  ... and {len(found) - 50} more")
    return 1 if bugs else 0


if __name__ == "__main__":
    raise SystemExit(main())
