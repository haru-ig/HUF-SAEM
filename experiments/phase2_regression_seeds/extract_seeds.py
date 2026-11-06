#!/usr/bin/env python3
"""Phase-2 pilot, step 1: extract usable seeds from GCC's g++ regression tests.

Candidates are g++.dg/**/pr*.C (one file per historical bug report) plus every
g++.dg/torture/*.C (tests GCC itself runs at all -O levels).

Pipeline, per candidate:
  1. static filter   -- drop tests that are invalid code by design (dg-error,
                        dg-ice), multi-file (dg-additional-sources, local
                        #include "..."), or live in a directory that needs a
                        special runtime/flag (modules, sanitizers, openmp, lto...).
  2. strip           -- remove DejaGnu directives so the LLM doesn't copy them.
  3. compile screen  -- gcc14-g++ -std=c++23 at -O0/-O2/-O3 (compile only).
                        Must be accepted at -O0. A crash/ICE at any level marks
                        the seed as a KNOWN bug (fixed upstream after 14.2, but
                        still present in our gcc14) -- kept, but flagged so the
                        pilot never counts it, or its mutants' identical crash,
                        as a new find.
  4. run screen      -- if it has main(): link at -O0, run (5 s); runnable iff
                        exit 0. Runnable seeds also go through the tier-1
                        DifferentialOracle; a non-safe verdict is recorded too.
  5. dg-options      -- if the test carried its own flags (often the exact
                        pass that crashed, e.g. -O2 -fno-tree-vrp), one more
                        compile with them, also part of the known-bug screen.

Outputs (under --out):
  seeds/<id>.cpp      stripped, accepted seeds
  manifest.jsonl      one row per candidate (accepted or not, with reason)
  SUMMARY.txt         counts

Usage:
  python experiments/phase2_regression_seeds/extract_seeds.py -j 8
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from Fuzz4All.huf_saem.oracle import (  # noqa: E402
    DEFAULT_STDIN,
    DifferentialOracle,
    classify_compiler_output,
    ice_signature,
)

GXX_DG = "/home/ec2-user/gcc/gcc/testsuite/g++.dg"
COMPILER = "gcc14-g++"
STD = "c++23"

# Directories whose tests need a runtime, a flag family, or a harness we don't use.
EXCLUDED_DIRS = {
    "modules", "asan", "ubsan", "tsan", "hwasan", "gomp", "goacc", "tm", "lto",
    "plugin", "pch", "guality", "simulate-thread", "analyzer", "gcov",
    "tree-prof", "compat", "cpp", "contracts", "reflect", "special", "dfp",
    "charset", "pubtypes",
}

# Directives that make a test useless as a valid-program seed.
STATIC_REJECTS = (
    ("expects_error", re.compile(r"dg-error|dg-excess-errors")),
    ("expects_ice", re.compile(r"dg-ice")),
    ("multi_file", re.compile(r"dg-additional-sources|dg-additional-files|dg-module")),
    ("local_include", re.compile(r'^\s*#\s*include\s+"', re.M)),
    ("not_compilable_kind", re.compile(r"dg-do\s+(preprocess|assemble)")),
    ("needs_lto", re.compile(r"dg-lto")),
)

# DejaGnu directives, both comment styles. Removing a whole // comment is safe;
# for /* */ we only remove comments that consist of a directive.
_DG_LINE = re.compile(r"//\s*\{\s*dg-[^\n]*")
_DG_BLOCK = re.compile(r"/\*\s*\{\s*dg-.*?\*/", re.S)

_DG_OPTIONS = re.compile(r"dg-(?:additional-)?options\s+\"([^\"]*)\"|dg-(?:additional-)?options\s+\{([^}]*)\}")
_DG_DO = re.compile(r"dg-do\s+(\w+)")
_HAS_MAIN = re.compile(r"\bint\s+main\s*\(|\bmain\s*\(\s*\)\s*\{|\bauto\s+main\s*\(")

# dg-options flags we cannot or should not replay.
_DROP_FLAG = re.compile(
    r"^(-std=|-fdump|-save-temps|-Werror|-pedantic|-W|-fdiagnostics|-fmodules|"
    r"-fopenmp|-fopenacc|-flto|-fprofile|-fsanitize|-fplugin|-fself-test|-fcompare-debug|"
    r"-g|-fno-show-column|-fmessage-length|-v$|--param=ggc|-fmem-report|-ftime-report)"
)


def sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def run(cmd, timeout, stdin=None, cwd=None):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, input=stdin, cwd=cwd)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace"), False
    except subprocess.TimeoutExpired:
        return None, "", True


def list_candidates():
    out = set()
    for root, _dirs, files in os.walk(GXX_DG):
        rel = os.path.relpath(root, GXX_DG)
        for f in files:
            if not f.endswith(".C"):
                continue
            if f.startswith("pr") or rel.split(os.sep)[0] == "torture":
                out.add(os.path.join(rel, f) if rel != "." else f)
    return sorted(out)


def strip_dg(src: str) -> str:
    src = _DG_BLOCK.sub("", src)
    src = _DG_LINE.sub("", src)
    # collapse runs of blank lines left behind
    src = re.sub(r"\n[ \t]*(\n[ \t]*){2,}", "\n\n", src)
    return src.strip() + "\n"


def dg_flags(src: str):
    flags = []
    for m in _DG_OPTIONS.finditer(src):
        text = m.group(1) if m.group(1) is not None else m.group(2)
        try:
            toks = shlex.split(text)
        except ValueError:
            toks = text.split()
        flags += [t for t in toks if not _DROP_FLAG.match(t)]
    return flags


def compile_one(path, opt, workdir, extra=()):
    cmd = [COMPILER, "-x", "c++", f"-std={STD}", opt, "-w", *extra, "-c", path,
           "-o", os.path.join(workdir, "o.o")]
    rc, out, to = run(cmd, timeout=60)
    if to:
        return {"status": "hang"}
    crash = classify_compiler_output(rc, out)
    if crash:
        return {"status": "crash", "kind": crash, "sig": ice_signature(out)}
    if rc != 0:
        first = next((l for l in out.splitlines() if "error" in l), out[:200])
        return {"status": "reject", "err": first[:300]}
    return {"status": "ok"}


def process(rel, out_dir, oracle):
    row = {"src": rel}
    top = rel.split(os.sep)[0]
    if top in EXCLUDED_DIRS:
        row["accepted"], row["reason"] = False, f"excluded_dir:{top}"
        return row
    try:
        raw = open(os.path.join(GXX_DG, rel), encoding="utf-8", errors="replace").read()
    except OSError as e:
        row["accepted"], row["reason"] = False, f"read_error:{e}"
        return row
    for reason, pat in STATIC_REJECTS:
        if pat.search(raw):
            row["accepted"], row["reason"] = False, reason
            return row

    m = _DG_DO.search(raw)
    row["dg_do"] = m.group(1) if m else "default"
    row["dg_flags"] = dg_flags(raw)
    src = strip_dg(raw)
    row["id"] = sha(src)
    row["bytes"] = len(src)
    row["lines"] = src.count("\n")
    row["has_main"] = bool(_HAS_MAIN.search(src))

    work = tempfile.mkdtemp(prefix="p2seed_")
    try:
        path = os.path.join(work, "s.cpp")
        with open(path, "w") as f:
            f.write(src)

        levels = {}
        for opt in ("-O0", "-O2", "-O3"):
            levels[opt] = compile_one(path, opt, work)
            if opt == "-O0" and levels[opt]["status"] == "reject":
                break
        row["compile"] = levels
        if levels["-O0"]["status"] == "reject":
            row["accepted"], row["reason"] = False, "rejected_by_gcc14_c++23"
            return row

        known = []
        for opt, r in levels.items():
            if r["status"] in ("crash", "hang"):
                known.append(f"{r['status']}@{opt}")
        if row["dg_flags"]:
            r = compile_one(path, "-O0", work, extra=row["dg_flags"])
            row["compile_dg_flags"] = r
            if r["status"] in ("crash", "hang"):
                known.append(f"{r['status']}@dg_flags")

        row["runnable"] = False
        if row["has_main"] and levels["-O0"]["status"] == "ok":
            exe = os.path.join(work, "a.out")
            rc, out, to = run([COMPILER, "-x", "c++", f"-std={STD}", "-O0", "-w", "-fPIE", "-pie",
                               path, "-o", exe], timeout=60)
            if rc == 0 and not to:
                rc, out, to = run([exe], timeout=5, stdin=DEFAULT_STDIN, cwd=work)
                row["run"] = "timeout" if to else f"rc={rc}"
                row["runnable"] = (rc == 0 and not to)
            else:
                row["run"] = "link_fail"
        if row["runnable"]:
            v = oracle.check(path)
            row["oracle"] = v.kind
            if v.is_bug:
                known.append(f"oracle:{v.kind}")

        row["known_bug"] = known
        row["class"] = "run" if row["runnable"] else "compile"
        row["accepted"], row["reason"] = True, "ok"
        with open(os.path.join(out_dir, "seeds", f"{row['id']}.cpp"), "w") as f:
            f.write(f"// seed {row['id']} from gcc/testsuite/g++.dg/{rel}\n" + src)
        return row
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(REPO, "experiments/phase2_regression_seeds"))
    ap.add_argument("-j", "--jobs", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    os.makedirs(os.path.join(args.out, "seeds"), exist_ok=True)
    oracle = DifferentialOracle(
        compilers=[COMPILER], std=STD, jobs=1,
        bug_dir=os.path.join(args.out, "seed_oracle_findings"),
    )
    cands = list_candidates()
    if args.limit:
        cands = cands[: args.limit]
    print(f"{len(cands)} candidates", flush=True)

    rows, seen = [], {}
    with ThreadPoolExecutor(args.jobs) as ex:
        futs = {ex.submit(process, c, args.out, oracle): c for c in cands}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                row = fut.result()
            except Exception as e:  # never lose the whole run to one file
                row = {"src": futs[fut], "accepted": False, "reason": f"exception:{e!r}"}
            if row.get("accepted"):
                if row["id"] in seen:
                    row["accepted"], row["reason"] = False, f"duplicate_of:{seen[row['id']]}"
                else:
                    seen[row["id"]] = row["src"]
            rows.append(row)
            if i % 200 == 0:
                print(f"  {i}/{len(cands)}  accepted={len(seen)}", flush=True)

    rows.sort(key=lambda r: r["src"])
    with open(os.path.join(args.out, "manifest.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    write_summary(rows, args.out)


def write_summary(rows, out):
    acc = [r for r in rows if r.get("accepted")]
    reasons = Counter(r["reason"].split(":")[0] if not r.get("accepted") else "accepted" for r in rows)
    excl = Counter(r["reason"] for r in rows if r["reason"].startswith("excluded_dir"))
    known = [r for r in acc if r.get("known_bug")]
    clean = [r for r in acc if not r.get("known_bug")]
    lines = [
        "#" * 60,
        "# PHASE-2 PILOT STEP 1 -- regression-test seed extraction",
        f"# compiler {COMPILER}  -std={STD}  source {GXX_DG}",
        "#" * 60,
        f"candidates                : {len(rows)}",
        "",
        "outcome:",
        *[f"  {k:28s} {v}" for k, v in reasons.most_common()],
        "",
        "excluded directories:",
        *[f"  {k:28s} {v}" for k, v in excl.most_common()],
        "",
        f"ACCEPTED seeds            : {len(acc)}",
        f"  runnable (exit 0 @ -O0) : {sum(r['class'] == 'run' for r in acc)}",
        f"     of which torture/    : {sum(r['class'] == 'run' and r['src'].startswith('torture') for r in acc)}",
        f"  compile-only            : {sum(r['class'] == 'compile' for r in acc)}",
        f"  KNOWN BUG in gcc14      : {len(known)}   (crash/ICE/hang/oracle-divergence on the seed itself)",
        f"  clean seeds             : {len(clean)}",
        "",
        "known-bug triggers:",
        *[f"  {k:28s} {v}" for k, v in Counter(t for r in known for t in r['known_bug']).most_common()],
        "",
        "runnable-seed oracle verdicts:",
        *[f"  {k:28s} {v}" for k, v in Counter(r.get('oracle') for r in acc if r['class'] == 'run').most_common()],
        "",
        "seed size (bytes, accepted): "
        + (lambda s: f"median {s[len(s)//2]}  p90 {s[int(len(s)*.9)]}  max {s[-1]}" if s else "n/a")(
            sorted(r["bytes"] for r in acc)),
        f"  <= 2 KB: {sum(r['bytes'] <= 2048 for r in acc)}   <= 4 KB: {sum(r['bytes'] <= 4096 for r in acc)}",
    ]
    text = "\n".join(lines) + "\n"
    with open(os.path.join(out, "SUMMARY.txt"), "w") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()
