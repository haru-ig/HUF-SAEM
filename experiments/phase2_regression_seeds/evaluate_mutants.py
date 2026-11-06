#!/usr/bin/env python3
"""Phase-2 pilot, step 3: judge every mutant with the tier-1 oracle.

For each mutants/NNNNN.fuzz:
  - DifferentialOracle verdict (gcc14-g++ -O0/-O1/-O2/-O3, UB gate on) --
    the same oracle as experiments/tier1_differential, so rates compare
    directly with the Phase-1 corpus (0 bugs in 10000).
  - similarity to its seed (difflib ratio on whitespace-normalised text), to
    spot verbatim regurgitation of the memorised test.
  - KNOWN-bug match: an ICE whose signature equals one of the step-1 known-bug
    signatures is labelled known, not new.

Writes eval.jsonl (one row per mutant, resumable) and EVAL_SUMMARY.txt.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from Fuzz4All.huf_saem.oracle import DifferentialOracle  # noqa: E402

PHASE1_TIER1 = {  # experiments/tier1_differential/SUMMARY.txt, phase1_on arm
    "n": 10000, "safe": 9094, "invalid": 750, "nondeterministic": 81,
    "wrong_code": 41, "ub": 25, "nonterminating": 9, "real_bugs": 0,
}


def norm(s: str) -> str:
    s = re.sub(r"//[^\n]*|/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"\s+", " ", s).strip()


def load_known_sigs():
    sigs = set()
    for l in open(os.path.join(HERE, "manifest.jsonl")):
        r = json.loads(l)
        for v in list((r.get("compile") or {}).values()) + [r.get("compile_dg_flags") or {}]:
            if v.get("status") == "crash" and v.get("sig"):
                sigs.add(v["sig"])
    return sigs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mutants", default=os.path.join(HERE, "mutants"))
    ap.add_argument("-j", "--jobs", type=int, default=6)
    args = ap.parse_args()

    log = {}
    for l in open(args.mutants + ".jsonl"):
        r = json.loads(l)
        log[r["idx"]] = r
    out_path = os.path.join(HERE, "eval.jsonl")
    done = set()
    if os.path.exists(out_path):
        done = {json.loads(l)["idx"] for l in open(out_path)}

    known_sigs = load_known_sigs()
    oracle = DifferentialOracle(
        compilers=["gcc14-g++"], std="c++23", jobs=2,
        bug_dir=os.path.join(HERE, "mutant_findings"),
    )
    seed_cache = {}

    def seed_text(sid):
        if sid not in seed_cache:
            seed_cache[sid] = norm(open(os.path.join(HERE, "seeds", f"{sid}.cpp")).read().split("\n", 1)[1])
        return seed_cache[sid]

    files = sorted(f for f in os.listdir(args.mutants) if f.endswith(".fuzz"))
    todo = [f for f in files if int(f[:5]) not in done]
    print(f"{len(files)} mutants, {len(todo)} to evaluate", flush=True)
    lock = threading.Lock()
    count = [0]

    def work(fname):
        idx = int(fname[:5])
        meta = log.get(idx, {})
        path = os.path.join(args.mutants, fname)
        code = open(path, errors="replace").read()
        v = oracle.check(path)
        row = {"idx": idx, "seed": meta.get("seed"), "seed_src": meta.get("seed_src"),
               "op": meta.get("op"), "partner": meta.get("partner"),
               "verdict": v.kind, "summary": v.summary[:300], "signature": v.signature,
               "saved_to": v.saved_to, "bytes": len(code)}
        if meta.get("seed"):
            row["sim_seed"] = round(difflib.SequenceMatcher(None, seed_text(meta["seed"]), norm(code),
                                                            autojunk=False).ratio(), 3)
        row["known"] = bool(v.kind == "ice" and any(s and s in (v.signature + v.summary + v.detail)
                                                    for s in known_sigs))
        with lock:
            with open(out_path, "a") as f:
                f.write(json.dumps(row) + "\n")
            count[0] += 1
            if count[0] % 100 == 0:
                print(f"  {count[0]}/{len(todo)}", flush=True)
            if v.is_bug:
                print(f"  *** {fname} [{v.kind}] {v.summary[:150]}  seed={meta.get('seed_src')}", flush=True)

    with ThreadPoolExecutor(args.jobs) as ex:
        list(ex.map(work, todo))
    summarize(out_path)


def summarize(out_path):
    rows = [json.loads(l) for l in open(out_path)]
    n = len(rows)
    tally = Counter(r["verdict"] for r in rows)
    by_op = defaultdict(Counter)
    for r in rows:
        by_op[r["op"]][r["verdict"]] += 1
    bugs = [r for r in rows if r["verdict"] in
            ("ice", "compiler_signal", "compile_hang", "wrong_code", "hang_divergence", "reject_divergence")]
    sims = sorted(r["sim_seed"] for r in rows if "sim_seed" in r)
    pct = lambda a, b: f"{100.0 * a / b:.1f}%" if b else "n/a"
    L = ["#" * 64, "# PHASE-2 PILOT -- oracle verdicts on regression-seeded mutants", "#" * 64,
         f"mutants evaluated: {n}", "", "verdicts                 mutants            phase-1 corpus (tier-1)"]
    for k in sorted(set(tally) | {k for k in PHASE1_TIER1 if k not in ("n", "real_bugs")},
                    key=lambda k: -tally.get(k, 0)):
        L.append(f"  {k:20s} {tally.get(k, 0):6d} ({pct(tally.get(k, 0), n):>6})   "
                 f"{PHASE1_TIER1.get(k, 0):6d} ({pct(PHASE1_TIER1.get(k, 0), PHASE1_TIER1['n']):>6})")
    L += ["", "per operator (n / still-valid / bug candidates):"]
    for op, c in sorted(by_op.items()):
        tot = sum(c.values())
        valid = tot - c.get("invalid", 0)
        cand = sum(v for k, v in c.items() if k in ("ice", "compiler_signal", "compile_hang", "wrong_code",
                                                        "hang_divergence", "reject_divergence"))
        L.append(f"  {str(op):10s} {tot:5d}   valid {pct(valid, tot):>6}   candidates {cand}")
    L += ["", f"bug candidates: {len(bugs)}  (known-signature ICEs: {sum(r['known'] for r in bugs)})",
          f"  distinct seeds behind them: {len({r['seed'] for r in bugs})}",
          f"  distinct signatures       : {len({r['signature'] or r['summary'] for r in bugs})}"]
    for r in sorted(bugs, key=lambda r: r["verdict"])[:40]:
        L.append(f"    {r['idx']:05d} [{r['verdict']}{' KNOWN' if r['known'] else ''}] op={r['op']} "
                 f"seed={r['seed_src']}  {r['summary'][:90]}")
    if sims:
        L += ["", "similarity to seed (difflib, normalised): "
              f"median {sims[len(sims)//2]}  p90 {sims[int(len(sims)*.9)]}  "
              f">=0.9: {sum(s >= 0.9 for s in sims)} ({pct(sum(s >= 0.9 for s in sims), len(sims))})"]
    L += ["", "NOTE: wrong_code candidates are unconfirmed until triaged "
          "(tools/triage_findings.py); the phase-1 corpus had 41 candidates, 0 real bugs."]
    text = "\n".join(L) + "\n"
    open(os.path.join(HERE, "EVAL_SUMMARY.txt"), "w").write(text)
    print(text)


if __name__ == "__main__":
    main()
