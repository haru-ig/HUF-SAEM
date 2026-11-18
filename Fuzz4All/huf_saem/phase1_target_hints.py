"""Phase 1 (redesign), step 2: turn targets into validated C++ hints.

For every reachable match.pd target, GPT-4o is asked (once, cached) for
  - whether C++ source can trigger the rule at -O2,
  - a one-sentence hint a mutation prompt can carry, and
  - a small C++ example that should trigger it.

The example is then VALIDATED by compiling it with the instrumented GCC at
-O2 -fdump-tree-all-folding: GCC prints "Applying pattern match.pd:LINE, ..."
for every rule it applies, so a hint is "validated" when a LINE inside the
target rule's span shows up. One retry feeds the outcome (no trigger, or the
compile errors) back to GPT-4o.

Limitation: folding done by the C++ front end before the GIMPLE passes is not
logged, so validation can miss real triggers (never invent them).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Sequence, Tuple

GXX = "/home/ec2-user/gcc-cov/bin/g++"
# Coverage output of the instrumented compiler goes to a throwaway directory,
# so validation never touches the build's own .gcda counters (which coverage
# replays rely on). Strip /home/ec2-user/gcc-cov-build so gcc/ and gcc/cp/
# files keep distinct names.
GCOV_STRIP = "3"

_APPLY = re.compile(r"Applying pattern match\.pd:(\d+),")
_JSON = re.compile(r"\{.*\}", re.S)

SYSTEM = (
    "You are an expert on GCC internals, its match.pd simplification rules, and on "
    "writing C++ that makes GCC's optimizer see specific GIMPLE shapes."
)

PROMPT = """\
Below is a rule from GCC's match.pd (GCC 14). It fires when GCC's GIMPLE or GENERIC \
folder sees the matched expression shape and the (if ...) conditions hold.

{comment}
{for_headers}
{rule}

Task: decide whether ordinary C++23 source compiled with g++ -O2 on x86-64 (no special \
-m flags) can make this rule fire, and if so show how.

Answer with ONLY a JSON object:
{{
  "triggerable": true or false,
  "why": "<one sentence: what source construct produces this shape, or why it cannot>",
  "hint": "<one imperative sentence telling a programmer what to put in a C++ program so \
the rule fires, mentioning the needed types, e.g. 'For a signed int x, compute (x + (x >> 31)) ^ (x >> 31).'>",
  "example": "<a small self-contained C++ translation unit (functions only, no main needed) \
whose code triggers the rule at -O2; use function parameters for values so they are not \
constant-folded away>"
}}"""

RETRY = """\
Your example did not work: {problem}
Remember: values must come from function parameters (not constants) so the expression \
survives until the GIMPLE optimizers, and the types must satisfy the rule's (if ...) \
conditions. Reply with the corrected JSON object only."""


def rule_fires(source: str, span: Tuple[int, int], timeout: int = 60) -> Tuple[bool, str, List[int]]:
    """Compile `source` at -O2 with folding dumps; did any line in `span` fire?

    Returns (fired, error_text_or_empty, applied_match_pd_lines).
    """
    d = tempfile.mkdtemp(prefix="rulecheck_")
    try:
        with open(os.path.join(d, "t.cc"), "w") as f:
            f.write(source)
        env = dict(os.environ, GCOV_PREFIX=os.path.join(d, "gcda"), GCOV_PREFIX_STRIP=GCOV_STRIP)
        try:
            p = subprocess.run([GXX, "-std=c++23", "-O2", "-w", "-c", "t.cc", "-o", "t.o",
                                "-fdump-tree-all-folding"],
                               cwd=d, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "compile timed out", []
        if p.returncode != 0:
            err = "\n".join(l for l in p.stderr.splitlines() if "error" in l)[:800]
            return False, err or p.stderr[:800], []
        applied = set()
        for name in os.listdir(d):
            if name.startswith("t.cc."):
                with open(os.path.join(d, name), errors="replace") as f:
                    applied.update(int(m.group(1)) for m in _APPLY.finditer(f.read()))
        lo, hi = span
        return any(lo <= a <= hi for a in applied), "", sorted(applied)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _parse_json(text: str) -> Optional[dict]:
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


class HintGenerator:
    def __init__(self, model: str = "gpt-4o", max_tokens: int = 1200, client=None) -> None:
        self.model = model
        self.max_tokens = max_tokens
        if client is None:
            import openai
            client = openai.OpenAI(timeout=120, max_retries=3)
        self.client = client

    def _ask(self, messages: List[dict]) -> str:
        r = self.client.chat.completions.create(model=self.model, messages=messages,
                                                max_tokens=self.max_tokens, temperature=0.2)
        return r.choices[0].message.content or ""

    def hint_for(self, target: dict, retries: int = 1) -> dict:
        span = tuple(target["match_pd_lines"])
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": PROMPT.format(
                comment=target.get("comment", ""), for_headers="\n".join(target.get("for_headers", [])),
                rule=target["rule"])},
        ]
        out = {"id": target["id"], "attempts": 0, "validated": False}
        for attempt in range(retries + 1):
            out["attempts"] = attempt + 1
            try:
                text = self._ask(messages)
            except Exception as e:  # network / quota: record and stop
                out["error"] = repr(e)[:300]
                return out
            ans = _parse_json(text)
            if ans is None:
                problem = "the reply was not a JSON object."
                messages += [{"role": "assistant", "content": text},
                             {"role": "user", "content": RETRY.format(problem=problem)}]
                continue
            out.update(triggerable=bool(ans.get("triggerable")), why=ans.get("why", ""),
                       hint=ans.get("hint", ""), example=ans.get("example", ""))
            if not out["triggerable"] or not out["example"]:
                return out
            fired, err, applied = rule_fires(out["example"], span)
            out.update(validated=fired, compile_error=err[:400], applied_lines=applied[:50])
            if fired:
                return out
            problem = (f"it does not compile:\n{err}" if err else
                       "it compiled, but GCC did not apply this rule (it applied match.pd lines "
                       f"{applied[:20]} instead).")
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": RETRY.format(problem=problem)}]
        return out


def generate_hints(targets: Sequence[dict], out_path: str, generator: HintGenerator,
                   jobs: int = 4, retries: int = 1, limit: int = 0) -> Dict[str, int]:
    """Hints for the reachable targets, appended to out_path (cached by id)."""
    done = set()
    if os.path.exists(out_path):
        with open(out_path) as f:
            done = {json.loads(l)["id"] for l in f if l.strip()}
    todo = [t for t in targets if t.get("reachable", True) and t["id"] not in done]
    if limit:
        todo = todo[:limit]
    lock = threading.Lock()
    counts = {"done": 0, "validated": 0, "triggerable": 0, "errors": 0}

    def work(t):
        h = generator.hint_for(t, retries=retries)
        with lock:
            with open(out_path, "a") as f:
                f.write(json.dumps(h) + "\n")
            counts["done"] += 1
            counts["validated"] += int(h.get("validated", False))
            counts["triggerable"] += int(h.get("triggerable", False))
            counts["errors"] += int("error" in h)
            if counts["done"] % 25 == 0:
                print(f"  {counts['done']}/{len(todo)}  validated {counts['validated']}", flush=True)

    with ThreadPoolExecutor(jobs) as ex:
        list(ex.map(work, todo))
    return counts


def main(argv: Optional[Sequence[str]] = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="GPT-4o hints for Phase 1 targets, validated with GCC.")
    ap.add_argument("--targets", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="gpt-4o")
    ap.add_argument("-j", "--jobs", type=int, default=4)
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    with open(a.targets) as f:
        targets = [json.loads(l) for l in f if l.strip()]
    counts = generate_hints(targets, a.out, HintGenerator(model=a.model), a.jobs, a.retries, a.limit)
    print(json.dumps(counts))


if __name__ == "__main__":
    main()
