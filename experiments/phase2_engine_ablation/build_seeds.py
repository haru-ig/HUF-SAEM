#!/usr/bin/env python3
"""Phase-2 mutation-engine ablation: seeds from LLM output, no regression tests.

Question: does Phase 2's mutation engine beat Fuzz4All's built-in mutation
when both start from the SAME LLM-generated programs?

Seeds = programs 0..N-1 of the Phase-1 10k corpus (huf_saem_1phase_10k_run2)
that compile under gcc14-g++ -std=c++23 -c. They are written in the same
manifest/seed format as experiments/phase2_regression_seeds/, so the
unmodified Phase 2 engine (mode: regression_seeds) consumes them.

The comparison replay is then:
  arm A (existing): run2 programs 0..4999            -> coverage_curve_run2.csv
  arm B (this):     run2 programs 0..1999, then the Phase-2 mutants
Both arms share their first 2000 inputs byte-for-byte; any gap after that is
the mutation engine vs Phase-1 generation + Fuzz4All mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))


def compiles(path: str) -> bool:
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run(["gcc14-g++", "-x", "c++", "-std=c++23", "-w", "-c", path,
                            "-o", os.path.join(d, "o.o")], capture_output=True, timeout=60)
        return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=os.path.join(REPO, "outputs/huf_saem_1phase_10k_run2"))
    ap.add_argument("-n", type=int, default=2000, help="use programs 0..n-1")
    ap.add_argument("-j", type=int, default=8)
    args = ap.parse_args()

    os.makedirs(os.path.join(HERE, "seeds"), exist_ok=True)
    paths = [os.path.join(args.corpus, f"{i}.fuzz") for i in range(args.n)]
    with ThreadPoolExecutor(args.j) as ex:
        ok = list(ex.map(compiles, paths))

    rows, seen = [], set()
    for i, (p, good) in enumerate(zip(paths, ok)):
        code = open(p, encoding="utf-8", errors="replace").read()
        sid = hashlib.sha1(code.encode()).hexdigest()[:12]
        src = f"phase1_run2/{i}.fuzz"
        row = {"src": src, "id": sid, "bytes": len(code), "known_bug": [],
               "class": "run" if "main" in code else "compile"}
        if not good:
            row.update(accepted=False, reason="rejected_by_gcc14_c++23")
        elif sid in seen:
            row.update(accepted=False, reason="duplicate")
        else:
            seen.add(sid)
            row.update(accepted=True, reason="ok")
            with open(os.path.join(HERE, "seeds", f"{sid}.cpp"), "w") as f:
                f.write(f"// seed {sid} from {src}\n" + code)
        rows.append(row)
    with open(os.path.join(HERE, "manifest.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    acc = sum(r["accepted"] for r in rows)
    print(f"{len(rows)} programs -> {acc} seeds "
          f"({sum(r['reason'] == 'duplicate' for r in rows)} duplicates, "
          f"{sum(r['reason'].startswith('rejected') for r in rows)} rejected)")


if __name__ == "__main__":
    main()
