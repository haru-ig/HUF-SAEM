#!/usr/bin/env python3
"""Phase-2 pilot, step 2: LLM mutation of regression-test seeds.

Takes the clean, runnable, oracle-safe seeds from step 1 (manifest.jsonl),
puts torture/ seeds first, caps at --max-seeds, and asks the local DeepSeek
model (Ollama) for --per-seed mutants of each, rotating over OPERATORS.

Every mutant is written as mutants/NNNNN.fuzz (so tools/run_oracle.py and the
coverage replay scripts take the directory as-is) and logged to mutants.jsonl
with its seed, operator, partner seed (for 'merge') and generation time.

Resumable: work items are a deterministic list; items whose .fuzz exists are
skipped.

Usage:
  python experiments/phase2_regression_seeds/mutate_seeds.py --max-seeds 300 --per-seed 10
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import ollama

HERE = os.path.dirname(os.path.abspath(__file__))

OPERATORS = {
    "retype": (
        "Change the types and constants involved: use different integer widths, "
        "signedness, floating point, enums, or class/struct wrappers, and different "
        "literal values (including boundary values such as 0, -1, INT_MAX)."
    ),
    "deepen": (
        "Increase structural depth around the core construct: wrap it in extra "
        "templates, nested lambdas, inheritance layers, namespaces or "
        "constexpr/inline functions, so the compiler must see through more abstraction."
    ),
    "loops": (
        "Restructure the control flow: change loop shapes, bounds, strides and nesting, "
        "add early exits, switch statements or extra conditions, so the optimizer's "
        "loop and branch transformations behave differently."
    ),
    "merge": (
        "Combine it with the SECOND program below into one program that exercises the "
        "core constructs of both, with data flowing between them."
    ),
    "compute": (
        "Embed the core construct in a larger computation: call it many times with "
        "varying arguments, feed results into each other, aggregate them (sums, "
        "checksums), and use the results in later conditions."
    ),
}

PROMPT = """\
You are helping test the GCC C++ optimizer. Below is a C++ program taken from \
GCC's regression test suite; it once triggered a compiler bug. Write a NEW, \
different C++ program that keeps the kind of construct that made this program \
interesting for the compiler, but varies it as follows:

{instruction}

Requirements for the new program:
- One complete, self-contained C++23 translation unit with int main(). Standard headers only.
- Valid, well-defined C++: no undefined behaviour (no signed overflow, no out-of-bounds \
access, no uninitialised reads, no data races).
- Deterministic: no input, no randomness, no time, no threads, no printing of addresses.
- It MUST print its computed results with printf or std::cout, so that a wrong \
optimization changes the output. Do not just call abort() on mismatch.
- It must terminate quickly (well under one second).
- Do not copy the original verbatim, and do not write comments explaining the test.

Original program:
```cpp
{seed}
```
{partner}
Reply with only the new program in a single ```cpp code block."""

PARTNER = """
SECOND program:
```cpp
{seed}
```
"""

_CODE = re.compile(r"```(?:c\+\+|cpp|cc|C)?\s*\n(.*?)```", re.S)


def extract_code(text: str) -> str:
    blocks = _CODE.findall(text)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return text.strip() + "\n"


def read_seed(sid: str) -> str:
    src = open(os.path.join(HERE, "seeds", f"{sid}.cpp")).read()
    # drop our own provenance header line
    return src.split("\n", 1)[1] if src.startswith("// seed ") else src


def select_seeds(max_seeds: int, max_bytes: int, rng: random.Random):
    rows = [json.loads(l) for l in open(os.path.join(HERE, "manifest.jsonl"))]
    pool = [r for r in rows if r.get("accepted") and r.get("class") == "run"
            and not r.get("known_bug") and r.get("oracle") == "safe"
            and r["bytes"] <= max_bytes]
    torture = sorted([r for r in pool if r["src"].startswith("torture/")], key=lambda r: r["src"])
    other = sorted([r for r in pool if not r["src"].startswith("torture/")], key=lambda r: r["src"])
    rng.shuffle(other)
    return (torture + other)[:max_seeds]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-seeds", type=int, default=300)
    ap.add_argument("--per-seed", type=int, default=10)
    ap.add_argument("--max-bytes", type=int, default=4096)
    ap.add_argument("--model", default="deepseek-coder-v2")
    ap.add_argument("--temperature", type=float, default=0.8)
    ap.add_argument("--num-predict", type=int, default=2048)
    ap.add_argument("--workers", type=int, default=2, help="concurrent Ollama requests")
    ap.add_argument("--out", default=os.path.join(HERE, "mutants"))
    ap.add_argument("--rng-seed", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0, help="only the first N work items (smoke test)")
    args = ap.parse_args()

    rng = random.Random(args.rng_seed)
    seeds = select_seeds(args.max_seeds, args.max_bytes, rng)
    ops = list(OPERATORS)
    items = []
    for s in seeds:
        for k in range(args.per_seed):
            op = ops[k % len(ops)]
            partner = rng.choice([p for p in seeds if p["id"] != s["id"]])["id"] if op == "merge" else None
            items.append({"idx": len(items), "seed": s["id"], "seed_src": s["src"], "op": op,
                          "partner": partner, "sample": k // len(ops)})
    if args.limit:
        items = items[: args.limit]
    os.makedirs(args.out, exist_ok=True)
    log_path = os.path.join(os.path.dirname(args.out), os.path.basename(args.out) + ".jsonl")
    with open(os.path.join(os.path.dirname(args.out), os.path.basename(args.out) + "_plan.json"), "w") as f:
        json.dump({"args": vars(args), "seeds": [s["id"] for s in seeds], "n_items": len(items)}, f)
    print(f"{len(seeds)} seeds ({sum(s['src'].startswith('torture/') for s in seeds)} torture), "
          f"{len(items)} work items", flush=True)

    todo = [it for it in items if not os.path.exists(os.path.join(args.out, f"{it['idx']:05d}.fuzz"))]
    print(f"{len(todo)} to generate ({len(items) - len(todo)} already done)", flush=True)
    lock = threading.Lock()
    done = [0]
    t0 = time.time()

    def work(it):
        prompt = PROMPT.format(
            instruction=OPERATORS[it["op"]],
            seed=read_seed(it["seed"]).strip(),
            partner=PARTNER.format(seed=read_seed(it["partner"]).strip()) if it["partner"] else "",
        )
        t = time.time()
        err = None
        for attempt in range(3):
            try:
                resp = ollama.chat(
                    model=args.model,
                    messages=[{"role": "user", "content": prompt}],
                    options={"temperature": args.temperature, "num_predict": args.num_predict,
                             "seed": args.rng_seed * 1_000_003 + it["idx"]},
                )
                text = resp["message"]["content"]
                break
            except Exception as e:  # server hiccup: retry
                err, text = repr(e), None
                time.sleep(5 * (attempt + 1))
        rec = dict(it, gen_sec=round(time.time() - t, 2))
        if text is None:
            rec["error"] = err
        else:
            code = extract_code(text)
            rec["bytes"] = len(code)
            path = os.path.join(args.out, f"{it['idx']:05d}.fuzz")
            with open(path + ".tmp", "w") as f:
                f.write(code)
            os.replace(path + ".tmp", path)  # atomic: a resume never sees half a file
        with lock:
            with open(log_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
            done[0] += 1
            if done[0] % 50 == 0 or done[0] == len(todo):
                el = time.time() - t0
                eta = el / done[0] * (len(todo) - done[0])
                print(f"  {done[0]}/{len(todo)}  {el/60:.0f} min elapsed  eta {eta/60:.0f} min", flush=True)

    with ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(work, todo))


if __name__ == "__main__":
    sys.exit(main())
