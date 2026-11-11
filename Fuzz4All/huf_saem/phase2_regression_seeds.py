"""Phase 2 (regression_seeds mode): seed the fuzzer with the compiler's own
regression tests, then LLM-mutate them.

Why this replaced the bug-report regex mutators (see
experiments/phase2_regression_seeds/ for the pilot):

  * The old mutators rarely fired and ~90% of what they produced no longer
    compiled; Phase 1+2 ended slightly *below* Phase 1 alone.
  * 2074 screened GCC g++.dg regression tests, replayed unmutated, reach 34.6%
    GCC line coverage -- every LLM-generation arm plateaued near 29%.
  * 3000 DeepSeek mutants of those seeds add another +1.2 points (to 35.8%)
    that no seed reaches. Seeds are finite, so mutation is what keeps the
    stream going once they are used up.

Schedule:
  1. seeds-first: emit every seed once, in batches, before any generation.
  2. afterwards, each batch is (with probability ``mutate_ratio``) an LLM
     mutation of a parent drawn from the seeds or from earlier mutants that
     compiled; otherwise the caller falls back to ordinary generation.

Seeds come from ``experiments/phase2_regression_seeds/extract_seeds.py``
(manifest.jsonl + seeds/<id>.cpp).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import random
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

OPERATORS: Dict[str, str] = {
    "retype": (
        "Change the types and constants involved: use different integer widths, "
        "signedness, floating point, enums, or class/struct wrappers, and different "
        "literal values (including boundary values such as 0, -1, INT_MAX)."
    ),
    # Reworded after the pilot: the original wording had the lowest validity
    # (66% vs ~79% for the rest), mostly from use-before-declaration and
    # half-finished template machinery.
    "deepen": (
        "Increase structural depth around the core construct: wrap it in one or two "
        "extra layers of templates, lambdas, inheritance or constexpr/inline functions, "
        "so the compiler must see through more abstraction. Keep every template "
        "complete and declare everything before it is used."
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
You are helping test the GCC C++ optimizer. Below is a C++ program derived from \
GCC's regression test suite; programs like it once triggered compiler bugs. Write a \
NEW, substantially different C++ program that keeps the kind of construct that made \
this program interesting for the compiler, but varies it as follows:

{instruction}

Requirements for the new program:
- One complete, self-contained C++23 translation unit with int main(). Standard headers only.
- Valid, well-defined C++: no undefined behaviour (no signed overflow, no out-of-bounds \
access, no uninitialised reads, no use of objects after their lifetime ends, no data races).
- Deterministic: no input, no randomness, no time, no threads, no printing of addresses.
- It MUST print its computed results with printf or std::cout, so that a wrong \
optimization changes the output. Do not just call abort() on mismatch.
- It must terminate quickly (well under one second).
- Change most of the code: do not reuse the original's lines verbatim, and do not \
write comments explaining the test.

Original program:
```cpp
{parent}
```
{partner}
Reply with only the new program in a single ```cpp code block."""

PARTNER = """
SECOND program:
```cpp
{code}
```
"""

# --- prompt version 2 ---------------------------------------------------------
# v1 told the model every parent came from GCC's regression suite, which is
# false for LLM-written seeds and for mutant parents. v2 is provenance-neutral
# and asks for larger changes: in the runs so far 19-75% of samples per
# operator were near-copies of the parent (retype worst), mostly because
# "change the types and constants" invites a handful of textual edits.

PROMPT_V2 = """\
You are helping test the GCC C++ optimizer. Below is a C++ program used as a \
starting point. Write a NEW C++ program that keeps the kind of construct this \
program exercises, but transforms it substantially as follows:

{instruction}

Requirements for the new program:
- One complete, self-contained C++23 translation unit with int main(). Standard headers only.
- Valid, well-defined C++: no undefined behaviour (no signed overflow, no out-of-bounds \
access, no uninitialised reads, no use of objects after their lifetime ends, no data races).
- Deterministic: no input, no randomness, no time, no threads, no printing of addresses.
- It MUST print its computed results with printf or std::cout, so that a wrong \
optimization changes the output. Do not just call abort() on mismatch.
- It must terminate quickly (well under one second).
- It must differ substantially from the original: rename the identifiers, restructure \
the functions and statements, and rewrite most lines. A program that only edits a \
few tokens of the original is not acceptable. No comments explaining the test.

Original program:
```cpp
{parent}
```
{partner}
Reply with only the new program in a single ```cpp code block."""

OPERATORS_V2: Dict[str, str] = dict(
    OPERATORS,
    retype=(
        "Rebuild the program around different data types: change the type of most "
        "variables, parameters and return values (different integer widths, signedness, "
        "floating point, enums, small structs or classes wrapping the values), change every "
        "literal (include boundary values such as 0, -1, INT_MAX), and rewrite the "
        "expressions and helper functions so they work naturally with the new types."
    ),
    loops=(
        "Rebuild the control flow: replace the loop structure with a different one "
        "(for/while/do-while, nested or flattened, different bounds, strides and directions), "
        "add early exits, switch statements and extra conditions, and move work between "
        "loops and helper functions, so the optimizer's loop and branch transformations "
        "behave differently."
    ),
)

RETRY_FEEDBACK = """

IMPORTANT: a previous answer to this request was rejected because it was almost \
identical to the original program. Make much larger changes this time: different \
names, different structure, different helper functions, rewritten expressions."""

_CODE_BLOCK = re.compile(r"```(?:c\+\+|cpp|cc|C)?\s*\n(.*?)```", re.S)
_HAS_MAIN = re.compile(r"\bmain\s*\(")
_PROVENANCE_HEADER = re.compile(r"\A// seed \S+ from [^\n]*\n")


def extract_code(text: str) -> str:
    """Largest fenced code block, or the whole text when there is none."""
    blocks = _CODE_BLOCK.findall(text or "")
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return (text or "").strip() + "\n"


def _normalise(code: str) -> str:
    code = re.sub(r"//[^\n]*|/\*.*?\*/", "", code, flags=re.S)
    return re.sub(r"\s+", " ", code).strip()


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalise(a), _normalise(b), autojunk=False).ratio()


# --------------------------------------------------------------------------- #
# Seeds
# --------------------------------------------------------------------------- #


@dataclass
class Seed:
    id: str
    src: str          # path relative to gcc/testsuite/g++.dg
    code: str
    cls: str          # "run" | "compile"


def load_seeds(
    manifest: str,
    seed_dir: str,
    classes: Sequence[str] = ("run", "compile"),
    exclude_known_bugs: bool = True,
    max_bytes: int = 4096,
    rng: Optional[random.Random] = None,
) -> List[Seed]:
    """Accepted seeds from an extract_seeds.py manifest, torture/ first.

    Known-bug seeds (ones that already crash or miscompile under the screening
    compiler) are excluded by default: they would only re-report bugs fixed
    upstream, and their mutants' identical crashes would look like new finds.
    """
    rng = rng or random.Random(0)
    seeds: List[Seed] = []
    with open(manifest, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if not r.get("accepted"):
                continue
            if r.get("class") not in classes:
                continue
            if exclude_known_bugs and r.get("known_bug"):
                continue
            if r.get("bytes", 0) > max_bytes:
                continue
            path = os.path.join(seed_dir, f"{r['id']}.cpp")
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as sf:
                code = _PROVENANCE_HEADER.sub("", sf.read())
            seeds.append(Seed(r["id"], r["src"], code, r["class"]))
    torture = sorted((s for s in seeds if s.src.startswith("torture/")), key=lambda s: s.src)
    other = sorted((s for s in seeds if not s.src.startswith("torture/")), key=lambda s: s.src)
    rng.shuffle(other)
    return torture + other


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

# One output's provenance. origin: "seed" | "mutant".
Meta = Dict[str, object]
GenerateFn = Callable[[str, int], List[str]]


@dataclass
class _OpStats:
    attempts: int = 0
    valid: int = 0
    near_copies: int = 0

    @property
    def weight(self) -> float:
        # Laplace-smoothed validity rate: every operator keeps a chance, and
        # one that keeps producing non-compiling code is picked less often.
        return (self.valid + 1) / (self.attempts + 2)


@dataclass
class _Parent:
    id: str
    code: str
    root_seed: str


class RegressionSeedEngine:
    """Seeds-first scheduler plus LLM mutation of seeds and valid mutants."""

    def __init__(
        self,
        seeds: List[Seed],
        generate_fn: GenerateFn,
        batch_size: int = 5,
        mutate_ratio: float = 0.5,
        parent_mutant_ratio: float = 0.5,
        max_similarity: float = 0.9,
        operators: Optional[Sequence[str]] = None,
        max_pool: int = 5000,
        rng: Optional[random.Random] = None,
        prompt_version: int = 1,
        retry_near_copies: bool = False,
        dedupe: bool = False,
        on_reject: Optional[Callable[[str, Meta], None]] = None,
    ) -> None:
        if not seeds:
            raise ValueError("phase2 regression_seeds: no seeds loaded (check seed_manifest / seed_dir)")
        unknown = [o for o in (operators or []) if o not in OPERATORS]
        if unknown:
            raise ValueError(f"phase2 regression_seeds: unknown operators {unknown}; known: {list(OPERATORS)}")
        self.seeds = seeds
        self.generate_fn = generate_fn
        self.batch_size = max(1, batch_size)
        self.mutate_ratio = mutate_ratio
        self.parent_mutant_ratio = parent_mutant_ratio
        self.max_similarity = max_similarity
        self.operators = list(operators or OPERATORS)
        self.max_pool = max_pool
        self.rng = rng or random.Random(0)

        self.cursor = 0                      # next seed to emit
        self.mutant_pool: List[_Parent] = []  # mutants that compiled
        self.op_stats: Dict[str, _OpStats] = {o: _OpStats() for o in self.operators}
        if prompt_version not in (1, 2):
            raise ValueError(f"phase2 regression_seeds: prompt_version must be 1 or 2, got {prompt_version}")
        self.prompt_version = prompt_version
        self.retry_near_copies = retry_near_copies
        self.dedupe = dedupe
        self.on_reject = on_reject
        self._seen_hashes: set = set()
        self._calls = 0
        # Does generate_fn take a per-call sampling seed? (tests pass 2-arg stubs)
        try:
            import inspect
            self._gen_takes_seed = "seed" in inspect.signature(generate_fn).parameters
        except (TypeError, ValueError):
            self._gen_takes_seed = False

        self.counts: Dict[str, int] = {"seed": 0, "mutant": 0, "near_copy": 0, "no_main": 0,
                                       "duplicate": 0, "retry_calls": 0}
        self._next_mutant_id = 0

    # -- scheduling ---------------------------------------------------------

    @property
    def seeds_exhausted(self) -> bool:
        return self.cursor >= len(self.seeds)

    def next_batch(self) -> Optional[List[Tuple[str, Meta]]]:
        """Programs for this iteration, or None to let the caller generate.

        An empty list never escapes: a batch whose every mutant was rejected
        returns None so the fuzz loop falls back to ordinary generation instead
        of spinning.
        """
        if not self.seeds_exhausted:
            batch = self.seeds[self.cursor : self.cursor + self.batch_size]
            self.cursor += len(batch)
            self.counts["seed"] += len(batch)
            return [
                (s.code, {"origin": "seed", "seed_id": s.id, "seed_src": s.src, "root_seed": s.id})
                for s in batch
            ]
        if self.rng.random() >= self.mutate_ratio:
            return None
        return self._mutate_batch() or None

    def _pick_op(self) -> str:
        weights = [self.op_stats[o].weight for o in self.operators]
        return self.rng.choices(self.operators, weights=weights, k=1)[0]

    def _pick_parent(self) -> _Parent:
        if self.mutant_pool and self.rng.random() < self.parent_mutant_ratio:
            return self.rng.choice(self.mutant_pool)
        s = self.rng.choice(self.seeds)
        return _Parent(s.id, s.code, s.id)

    def build_prompt(self, op: str, parent: _Parent, partner: Optional[Seed]) -> str:
        template, ops = (PROMPT, OPERATORS) if self.prompt_version == 1 else (PROMPT_V2, OPERATORS_V2)
        return template.format(
            instruction=ops[op],
            parent=parent.code.strip(),
            partner=PARTNER.format(code=partner.code.strip()) if partner else "",
        )

    def _generate(self, prompt: str, n: int) -> List[str]:
        self._calls += 1
        try:
            if self._gen_takes_seed:
                # A distinct sampling seed per call: identical prompts sent with
                # Ollama's default seed came back as exact duplicates (2.5-5.5%
                # of kept mutants).
                return self.generate_fn(prompt, n, seed=self.rng.randrange(2**31)) or []
            return self.generate_fn(prompt, n) or []
        except Exception:
            return []

    def _reject(self, code: str, meta: Meta, reason: str) -> None:
        self.counts[reason] += 1
        if reason == "near_copy" and meta.get("op") in self.op_stats:
            self.op_stats[str(meta["op"])].near_copies += 1
        if self.on_reject is not None:
            try:
                self.on_reject(code, dict(meta, reason=reason))
            except Exception:
                pass

    def _filter(self, texts: List[str], base: Meta, parent: _Parent) -> Tuple[List[Tuple[str, Meta]], int]:
        kept: List[Tuple[str, Meta]] = []
        near = 0
        for text in texts:
            code = extract_code(text)
            if not _HAS_MAIN.search(code):
                self._reject(code, base, "no_main")
                continue
            sim = similarity(parent.code, code)
            meta = dict(base, sim_parent=round(sim, 3))
            if sim >= self.max_similarity:
                # Near-verbatim regurgitation of the parent: costs a compile
                # and (we assume) adds nothing, so it is dropped unwritten.
                near += 1
                self._reject(code, meta, "near_copy")
                continue
            if self.dedupe:
                h = hashlib.sha1(_normalise(code).encode()).hexdigest()
                if h in self._seen_hashes:
                    self._reject(code, meta, "duplicate")
                    continue
                self._seen_hashes.add(h)
            self.counts["mutant"] += 1
            kept.append((code, meta))
        return kept, near

    def _mutate_batch(self) -> List[Tuple[str, Meta]]:
        op = self._pick_op()
        parent = self._pick_parent()
        partner = None
        if op == "merge":
            candidates = [s for s in self.seeds if s.id != parent.root_seed] or self.seeds
            partner = self.rng.choice(candidates)
        prompt = self.build_prompt(op, parent, partner)
        base: Meta = {"origin": "mutant", "op": op, "parent": parent.id, "root_seed": parent.root_seed,
                      "partner": partner.id if partner else None, "retry": False}
        out, near = self._filter(self._generate(prompt, self.batch_size), base, parent)
        # One retry when most of the batch came back as near-copies.
        missing = self.batch_size - len(out)
        if self.retry_near_copies and missing > 0 and near * 2 >= self.batch_size:
            self.counts["retry_calls"] += 1
            more, _ = self._filter(self._generate(prompt + RETRY_FEEDBACK, missing),
                                   dict(base, retry=True), parent)
            out += more
        return out

    # -- feedback -----------------------------------------------------------

    def record(self, code: str, meta: Meta, valid: Optional[bool]) -> Meta:
        """Feed back whether an emitted program compiled; returns the meta,
        completed with a mutant id, for the provenance log."""
        if meta.get("origin") != "mutant":
            return meta
        mid = f"m{self._next_mutant_id:06d}"
        self._next_mutant_id += 1
        meta = dict(meta, mutant_id=mid)
        op = str(meta["op"])
        if valid is not None and op in self.op_stats:
            self.op_stats[op].attempts += 1
            if valid:
                self.op_stats[op].valid += 1
        if valid:
            self._add_to_pool(_Parent(mid, code, str(meta["root_seed"])))
        return meta

    def _add_to_pool(self, p: _Parent) -> None:
        if len(self.mutant_pool) < self.max_pool:
            self.mutant_pool.append(p)
        else:  # reservoir-style replacement keeps the pool bounded but fresh
            self.mutant_pool[self.rng.randrange(self.max_pool)] = p

    def restore(self, provenance: Sequence[Meta], read_file: Callable[[str], Optional[str]]) -> None:
        """Rebuild scheduler state from a provenance log when resuming a run."""
        seen_seeds = {m.get("seed_id") for m in provenance if m.get("origin") == "seed"}
        self.cursor = sum(1 for s in self.seeds if s.id in seen_seeds)
        # Seeds were emitted in order, so the first `cursor` seeds are the
        # ones already used; anything else means the seed set changed.
        self.seeds = [s for s in self.seeds if s.id in seen_seeds] + \
                     [s for s in self.seeds if s.id not in seen_seeds]
        for m in provenance:
            if m.get("origin") != "mutant":
                continue
            self._next_mutant_id += 1
            op = str(m.get("op"))
            if m.get("valid") is not None and op in self.op_stats:
                self.op_stats[op].attempts += 1
                self.op_stats[op].valid += int(bool(m.get("valid")))
            if m.get("valid"):
                code = read_file(str(m.get("file")))
                if code:
                    self._add_to_pool(_Parent(str(m.get("mutant_id")), code, str(m.get("root_seed"))))

    def stats(self) -> Dict[str, object]:
        return {
            **self.counts,
            "seeds_total": len(self.seeds),
            "seeds_emitted": self.cursor,
            "mutant_pool": len(self.mutant_pool),
            "ops": {o: vars(s) | {"weight": round(s.weight, 3)} for o, s in self.op_stats.items()},
        }
