#!/usr/bin/env python3
"""Where is the uncovered GCC code, and how much of it could a C++ fuzzer reach?

Reads lcov .info files (GCC-source-only, as produced by collect.sh /
run_gcc_coverage.sh) and reports:
  1. coverage by area of GCC (front end, GIMPLE optimiser, IPA, RTL backend,
     target, flag-gated subsystems, infrastructure), for every corpus given;
  2. for the optimiser areas of the LAST corpus: uncovered lines in functions
     never entered vs. functions entered but only partly covered;
  3. the largest uncovered optimiser functions of each kind.

Usage: analyze_gaps.py label=path.info [label=path.info ...]   (last = focus)
"""

from __future__ import annotations

import collections
import re
import subprocess
import sys

ROOTS = ("/home/ec2-user/gcc-cov-src/gcc/", "/home/ec2-user/gcc-cov-build/gcc/")

# (area, gated-by-flag?, regex on the path relative to gcc/). First match wins.
AREAS = [
    ("C++ front end",               False, r"^cp/"),
    ("C-family front end",          False, r"^c-family/"),
    ("static analyzer",             True,  r"^analyzer/"),
    ("OpenMP/OpenACC",              True,  r"^(omp-|gimple-low|tree-nested)|oacc"),
    ("sanitizers",                  True,  r"^(asan|ubsan|tsan|hwasan|sanopt|sancov|gimple-isel-sanit)"),
    ("profiling/coverage/FDO",      True,  r"^(tree-profile|profile|value-prof|coverage|auto-profile|gcov)"),
    ("LTO / streaming",             True,  r"^(lto|ipa-icf|data-streamer|ipa-param-manipulation-stream)"),
    ("debug info (-g)",             True,  r"^(dwarf2|var-tracking|ctfc|ctfout|btfout|dbgcnt|debug)"),
    ("graphite (-fgraphite)",       True,  r"^graphite"),
    ("match.pd folding (generated)", False, r"^(gimple|generic)-match"),
    ("target backend (x86)",        False, r"^(config/|insn-|common/config)"),
    ("IPA / call graph",            False, r"^(ipa|cgraph|symtab|varpool)"),
    ("vectorizer",                  False, r"^tree-vect|^tree-loop-distribution|^tree-if-conv"),
    ("loop optimisation",           False, r"^(tree-ssa-loop|cfgloop|tree-scalar-evolution|tree-data-ref|"
                                           r"tree-predcom|tree-chrec|tree-affine|gimple-loop|tree-parloops|"
                                           r"tree-ssa-scopedtables|loop-)"),
    ("value ranges",                False, r"^(gimple-range|value-range|range-op|tree-vrp|vr-values|"
                                           r"gimple-ssa-warn-access|tree-ssanames|value-query)"),
    ("GIMPLE optimiser (other)",    False, r"^(tree-ssa|gimple-ssa|tree-sra|tree-switch|tree-call-cdce|"
                                           r"tree-object-size|tree-tailcall|tree-cfg|tree-inline|tree-into-ssa|"
                                           r"tree-outof-ssa|tree-complex|tree-vector-generic|tree-eh|"
                                           r"tree-stdarg|tree-nrv|tree-dfa|tree-phinodes|gimple-fold|gimple-|"
                                           r"fold-const|tree-ssa\.|passes|tree-pass|dominance|domwalk|"
                                           r"tree-if-|tree-cfgcleanup|gimplify|tree-ssa-)"),
    ("RTL backend",                 False, r"^(rtl|emit-rtl|expr|explow|optabs|calls|function|cfgexpand|cfgrtl|"
                                           r"combine|cse|gcse|ira|lra|reload|regcprop|regrename|sched|haifa|df-|"
                                           r"dse|dce|final|varasm|recog|reginfo|postreload|ree|shrink-wrap|"
                                           r"bb-reorder|fwprop|ifcvt|modulo-sched|compare-elim|cfgcleanup|"
                                           r"auto-inc-dec|mode-switching|lower-subreg|stor-layout|alias|"
                                           r"builtins|internal-fn|targhooks|expmed|dojump|stmt|cfg|caller-save|"
                                           r"regstat|resource|reorg|jump|simplify-rtx|late-combine|"
                                           r"pair-fusion|ext-dce|crc|avoid-store|cprop|hw-doloop|rtlanal|"
                                           r"lcm|bitmap|sbitmap|loop-|ddg|sel-sched|insn-)"),
]
OPTIMISER = {"match.pd folding (generated)", "IPA / call graph", "vectorizer",
             "loop optimisation", "value ranges", "GIMPLE optimiser (other)", "RTL backend"}


# Functions inside optimiser files that only run under a non-default option or
# never on x86-64 at all (old reload: x86 uses LRA).
GATED_FN = re.compile(r"omp|oacc|openacc|debug|strub|sanitiz|asan|tsan|ubsan|profile|lto_|"
                      r"transactional|_tm_|instrument", re.I)
GATED_FILE = re.compile(r"^(reload\.cc|reload1\.cc)$")


def area_of(rel: str):
    for name, gated, pat in AREAS:
        if re.search(pat, rel):
            return name, gated
    return "infrastructure / other", False


def parse(path):
    """-> {rel_file: {"lines": {line: hits}, "fns": [(start, end, name, hits)]}}"""
    files = {}
    cur = None
    fnspan, fnhits = {}, {}
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith("SF:"):
                p = line[3:].strip()
                for r in ROOTS:
                    if p.startswith(r):
                        p = p[len(r):]
                cur = {"lines": {}, "fns": []}
                files[p] = cur
                fnspan, fnhits = {}, {}
            elif line.startswith("FN:"):
                parts = line[3:].strip().split(",")
                if len(parts) == 3:
                    fnspan[parts[2]] = (int(parts[0]), int(parts[1]))
                else:
                    fnspan[parts[1]] = (int(parts[0]), int(parts[0]))
            elif line.startswith("FNDA:"):
                h, name = line[5:].strip().split(",", 1)
                fnhits[name] = int(h)
            elif line.startswith("DA:"):
                ln, h = line[3:].split(",")[:2]
                cur["lines"][int(ln)] = int(h)
            elif line.startswith("end_of_record"):
                cur["fns"] = [(s, e, n, fnhits.get(n, 0)) for n, (s, e) in fnspan.items()]
    return files


def demangle(names):
    if not names:
        return {}
    out = subprocess.run(["c++filt"], input="\n".join(names), capture_output=True, text=True).stdout.splitlines()
    return dict(zip(names, out))


def area_table(files):
    t = collections.defaultdict(lambda: [0, 0])
    for rel, d in files.items():
        a, _ = area_of(rel)
        t[a][0] += len(d["lines"])
        t[a][1] += sum(1 for h in d["lines"].values() if h > 0)
    return t


def main(args):
    corpora = [a.split("=", 1) for a in args]
    parsed = [(label, parse(p)) for label, p in corpora]
    tables = [(label, area_table(f)) for label, f in parsed]
    focus_label, focus = parsed[-1]
    gated = {name: g for name, g, _ in AREAS}
    gated["infrastructure / other"] = False

    total = sum(v[0] for v in tables[-1][1].values())
    print(f"GCC lines instrumented in cc1plus+driver: {total}\n")
    hdr = f"{'area':32s} {'lines':>8s} " + " ".join(f"{l[:14]:>15s}" for l, _ in tables) + f" {'uncovered':>10s}  gated"
    print(hdr)
    print("-" * len(hdr))
    rows = sorted(tables[-1][1].items(), key=lambda kv: -(kv[1][0] - kv[1][1]))
    sums = collections.Counter()
    for a, (n, h) in rows:
        cells = " ".join(f"{100 * t.get(a, [0, 0])[1] / max(1, n):14.1f}%" for _, t in tables)
        print(f"{a:32s} {n:8d} {cells} {n - h:10d}  {'yes' if gated.get(a) else ''}")
        key = "flag-gated" if gated.get(a) else ("optimiser" if a in OPTIMISER else "other reachable")
        sums[key + "_n"] += n
        sums[key + "_u"] += n - h
    print()
    unc_total = sum(sums[k] for k in sums if k.endswith("_u"))
    for key in ("optimiser", "other reachable", "flag-gated"):
        print(f"  {key:16s}: {sums[key + '_u']:7d} uncovered lines "
              f"({100 * sums[key + '_u'] / unc_total:.0f}% of all uncovered), "
              f"area coverage {100 * (1 - sums[key + '_u'] / max(1, sums[key + '_n'])):.1f}%")

    # --- optimiser functions in the focus corpus -------------------------------
    never, partial, gated_fn = [], [], 0
    for rel, d in focus.items():
        a, _ = area_of(rel)
        if a not in OPTIMISER:
            continue
        for s, e, name, hits in d["fns"]:
            ls = [h for ln, h in d["lines"].items() if s <= ln <= e]
            unc = sum(1 for h in ls if h == 0)
            if not ls or unc == 0:
                continue
            if GATED_FN.search(name) or GATED_FILE.match(rel):
                gated_fn += unc
                continue
            (never if hits == 0 else partial).append((unc, len(ls), rel, name, a))
    dm = demangle([x[3] for x in never + partial])
    nu, pu = sum(x[0] for x in never), sum(x[0] for x in partial)
    print(f"\n[{focus_label}] uncovered OPTIMISER lines: {nu + pu + gated_fn}")
    print(f"  in option-gated / not-on-x86 functions (OpenMP, -g, reload, ...): {gated_fn:7d}")
    print(f"  in functions never entered       : {nu:7d} ({len(never)} functions)")
    print(f"  in functions entered, partly run : {pu:7d} ({len(partial)} functions)")
    by_area = collections.Counter()
    for unc, n, rel, name, a in never + partial:
        by_area[a] += unc
    print("  remaining gap by optimiser area:", ", ".join(f"{a} {u}" for a, u in by_area.most_common()))
    for title, lst in (("largest NEVER-ENTERED optimiser functions", never),
                       ("largest gaps in PARTLY-COVERED optimiser functions", partial)):
        print(f"\n  {title}:")
        for unc, n, rel, name, a in sorted(lst, reverse=True)[:25]:
            nm = dm.get(name, name)
            nm = nm if len(nm) <= 70 else nm[:67] + "..."
            print(f"    {unc:5d}/{n:<5d} {rel:34s} {nm}")


if __name__ == "__main__":
    main(sys.argv[1:])
