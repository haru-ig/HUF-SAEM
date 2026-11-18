#!/usr/bin/env python3
"""Phase 1 as a SEED GENERATOR: turn validated match.pd hints into seed programs.

Each validated hint carries a small example (bare functions) that GCC has been
confirmed to apply the target rule to. DeepSeek wraps it into a complete
program -- functions kept, a main() that calls them with values only known at
run time, results printed -- and the program is kept only if
  1. it compiles and links (gcc14-g++ -std=c++23),
  2. it runs, exits 0 within 5 s and prints something, and
  3. GCC still applies the target rule in it (folding-dump check).

Output, in the same format as experiments/phase2_regression_seeds/ so Phase 2
loads it unchanged:
  target_seeds/seeds/<id>.cpp, target_seeds/manifest.jsonl
and a combined seed set (regression seeds + target seeds) for the run:
  combined_seeds/seeds/  (symlinks), combined_seeds/manifest.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

REPO = "/home/ec2-user/HUF-SAEM"
sys.path.insert(0, REPO)
from Fuzz4All.huf_saem.phase1_target_hints import rule_fires  # noqa: E402
from Fuzz4All.huf_saem.phase2_regression_seeds import extract_code  # noqa: E402
from Fuzz4All.huf_saem.oracle import DEFAULT_STDIN  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

PROMPT = """\
The C++ code below makes GCC apply a specific optimization ({hint}).

```cpp
{example}
```

Write ONE complete, self-contained C++23 program that:
- keeps the code above unchanged (you may add #include lines),
- adds int main() that calls the function(s) several times with argument values that are \
NOT compile-time constants: derive them at run time, e.g. from a `volatile` variable or a loop \
counter combined with it, and cover a few different values (including edge cases such as 0, \
negative numbers, large values) as long as no undefined behaviour occurs,
- prints every result with printf (and nothing else nondeterministic),
- is deterministic, reads no input, has no undefined behaviour, and finishes in well under a second.

Reply with only the program in a single ```cpp code block."""


def check(code: str, span):
    d = tempfile.mkdtemp(prefix="tseed_")
    try:
        src, exe = os.path.join(d, "s.cpp"), os.path.join(d, "a.out")
        open(src, "w").write(code)
        p = subprocess.run(["gcc14-g++", "-std=c++23", "-O0", "-w", src, "-o", exe],
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            return "compile", p.stderr[:300]
        try:
            r = subprocess.run([exe], input=DEFAULT_STDIN, capture_output=True, timeout=5, cwd=d)
        except subprocess.TimeoutExpired:
            return "timeout", ""
        if r.returncode != 0 or not r.stdout.strip():
            return "run", f"rc={r.returncode} out={len(r.stdout)}B"
        fired, err, _ = rule_fires(code, span)
        return ("ok", "") if fired else ("no_fire", err[:200])
    finally:
        subprocess.run(["rm", "-rf", d])


def main():
    import ollama
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default=os.path.join(HERE, "matchpd_targets.jsonl"))
    ap.add_argument("--hints", default=os.path.join(HERE, "matchpd_hints.jsonl"))
    ap.add_argument("--regression", default=f"{REPO}/experiments/phase2_regression_seeds")
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("-j", type=int, default=2)
    ap.add_argument("--model", default="deepseek-coder-v2")
    a = ap.parse_args()

    targets = {json.loads(l)["id"]: json.loads(l) for l in open(a.targets)}
    hints = [json.loads(l) for l in open(a.hints)]
    hints = [h for h in hints if h.get("validated") and h["id"] in targets]
    out = os.path.join(HERE, "target_seeds")
    os.makedirs(os.path.join(out, "seeds"), exist_ok=True)
    rows, lock = [], threading.Lock()
    tally = {}

    def work(i_h):
        i, h = i_h
        span = tuple(targets[h["id"]]["match_pd_lines"])
        prompt = PROMPT.format(hint=h["hint"].rstrip("."), example=h["example"].strip())
        status, detail, code = "no_attempt", "", ""
        for k in range(a.attempts):
            try:
                resp = ollama.chat(model=a.model, messages=[{"role": "user", "content": prompt}],
                                   options={"temperature": 0.4, "seed": 1000 * i + k, "num_predict": 2048})
                code = extract_code(resp["message"]["content"])
            except Exception as e:
                status, detail = "llm_error", repr(e)[:200]
                continue
            status, detail = check(code, span)
            if status == "ok":
                break
        with lock:
            tally[status] = tally.get(status, 0) + 1
            row = {"src": f"matchpd/{h['id'].split(':')[1]}.C", "target": h["id"], "status": status,
                   "detail": detail, "attempts": k + 1}
            if status == "ok":
                sid = "t" + hashlib.sha1(code.encode()).hexdigest()[:11]
                with open(os.path.join(out, "seeds", f"{sid}.cpp"), "w") as f:
                    f.write(f"// seed {sid} from phase1 target {h['id']}\n" + code)
                row.update(id=sid, accepted=True, reason="ok", **{"class": "run"}, bytes=len(code),
                           known_bug=[])
            else:
                row.update(accepted=False, reason=status)
            rows.append(row)
            if len(rows) % 25 == 0:
                print(f"  {len(rows)}/{len(hints)} {tally}", flush=True)

    with ThreadPoolExecutor(a.j) as ex:
        list(ex.map(work, enumerate(hints)))
    rows.sort(key=lambda r: r["src"])
    with open(os.path.join(out, "manifest.jsonl"), "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    print("target seeds:", tally)

    # combined seed set for the run: regression seeds + target seeds
    comb = os.path.join(HERE, "combined_seeds")
    os.makedirs(os.path.join(comb, "seeds"), exist_ok=True)
    reg_rows = [json.loads(l) for l in open(os.path.join(a.regression, "manifest.jsonl"))]
    n = 0
    for base, rs in ((a.regression, reg_rows), (out, rows)):
        for r in rs:
            if r.get("accepted"):
                link = os.path.join(comb, "seeds", f"{r['id']}.cpp")
                if not os.path.lexists(link):
                    os.symlink(os.path.join(base, "seeds", f"{r['id']}.cpp"), link)
                n += 1
    with open(os.path.join(comb, "manifest.jsonl"), "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in reg_rows + rows)
    print(f"combined seed set: {n} accepted seeds -> {comb}")


if __name__ == "__main__":
    main()
