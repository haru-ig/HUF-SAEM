"""Phase 2 regression_seeds mode: seed loading, scheduling, mutation filters,
feedback, resume, and the fuzz-loop wiring.

No LLM or compiler is needed: generation is a stub, and the end-to-end test
drives Fuzz4All/fuzz.py's real loop with a fake target.
"""

from __future__ import annotations

import json
import os
import random

import pytest

from Fuzz4All.huf_saem.phase2_regression_seeds import (
    OPERATORS,
    RegressionSeedEngine,
    Seed,
    extract_code,
    load_seeds,
    similarity,
)

PARENT = """#include <cstdio>
int f(int x) { return x * 3 + 1; }
int main() { int s = 0; for (int i = 0; i < 10; ++i) s += f(i); printf("%d\\n", s); }
"""

DIFFERENT = """#include <iostream>
#include <array>
template <typename T> struct Box { T v; constexpr T get() const { return v; } };
int main() {
  std::array<Box<long>, 4> a{{{1}, {2}, {3}, {4}}};
  long acc = 0;
  for (auto& b : a) acc = acc * 7 + b.get();
  std::cout << acc << "\\n";
}
"""


def _seed(i: int, src: str = None, code: str = PARENT) -> Seed:
    return Seed(id=f"s{i:03d}", src=src or f"opt/pr{i}.C", code=code, cls="run")


def _fenced(code: str) -> str:
    return f"Here you go:\n```cpp\n{code}```\n"


# -- seeds --------------------------------------------------------------------


def _write_manifest(tmp_path, rows):
    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir()
    with open(tmp_path / "manifest.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
            if r.get("accepted"):
                (seed_dir / f"{r['id']}.cpp").write_text(
                    f"// seed {r['id']} from gcc/testsuite/g++.dg/{r['src']}\n" + PARENT)
    return str(tmp_path / "manifest.jsonl"), str(seed_dir)


def test_load_seeds_filters_and_orders(tmp_path):
    rows = [
        {"src": "opt/pr2.C", "id": "a2", "accepted": True, "class": "run", "bytes": 100, "known_bug": []},
        {"src": "torture/pr9.C", "id": "t9", "accepted": True, "class": "compile", "bytes": 100, "known_bug": []},
        {"src": "torture/pr1.C", "id": "t1", "accepted": True, "class": "run", "bytes": 100, "known_bug": []},
        {"src": "opt/pr3.C", "id": "kb", "accepted": True, "class": "run", "bytes": 100, "known_bug": ["crash@-O2"]},
        {"src": "opt/pr4.C", "id": "big", "accepted": True, "class": "run", "bytes": 99999, "known_bug": []},
        {"src": "opt/pr5.C", "accepted": False, "reason": "expects_error"},
    ]
    manifest, seed_dir = _write_manifest(tmp_path, rows)

    seeds = load_seeds(manifest, seed_dir)
    assert [s.id for s in seeds] == ["t1", "t9", "a2"]  # torture first; known bug + oversized out
    assert not seeds[0].code.startswith("// seed"), "provenance header must be stripped"

    assert [s.id for s in load_seeds(manifest, seed_dir, classes=["run"])] == ["t1", "a2"]
    assert "kb" in {s.id for s in load_seeds(manifest, seed_dir, exclude_known_bugs=False)}


# -- scheduling ---------------------------------------------------------------


def test_seeds_first_then_mutation():
    seeds = [_seed(i) for i in range(7)]
    calls = []

    def gen(prompt, n):
        calls.append(prompt)
        return [_fenced(DIFFERENT)] * n

    eng = RegressionSeedEngine(seeds, gen, batch_size=3, mutate_ratio=1.0)
    b1, b2, b3 = eng.next_batch(), eng.next_batch(), eng.next_batch()
    assert [m["seed_id"] for _, m in b1 + b2 + b3] == [s.id for s in seeds]
    assert all(m["origin"] == "seed" for _, m in b1 + b2 + b3)
    assert not calls, "no LLM call while seeds remain"

    b4 = eng.next_batch()
    assert len(b4) == 3 and all(m["origin"] == "mutant" for _, m in b4)
    assert len(calls) == 1 and PARENT.strip() in calls[0]


def test_mutate_ratio_zero_falls_back_to_generation():
    eng = RegressionSeedEngine([_seed(0)], lambda p, n: [_fenced(DIFFERENT)] * n,
                               batch_size=5, mutate_ratio=0.0)
    assert eng.next_batch()[0][1]["origin"] == "seed"
    assert all(eng.next_batch() is None for _ in range(20))


# -- mutation filters ---------------------------------------------------------


def test_near_copies_and_mainless_output_are_dropped():
    outputs = [_fenced(PARENT), _fenced("int helper() { return 1; }\n"), _fenced(DIFFERENT)]
    eng = RegressionSeedEngine([_seed(0)], lambda p, n: outputs, batch_size=3, mutate_ratio=1.0)
    eng.next_batch()  # the seed
    batch = eng.next_batch()
    assert [c for c, _ in batch] == [extract_code(_fenced(DIFFERENT))]
    assert eng.counts["near_copy"] == 1 and eng.counts["no_main"] == 1


def test_all_rejected_returns_none_not_empty_list():
    eng = RegressionSeedEngine([_seed(0)], lambda p, n: [_fenced(PARENT)] * n,
                               batch_size=2, mutate_ratio=1.0)
    eng.next_batch()
    assert eng.next_batch() is None  # an empty list would make fuzz.py re-initialise the target


def test_llm_failure_falls_back():
    def boom(p, n):
        raise RuntimeError("ollama down")

    eng = RegressionSeedEngine([_seed(0)], boom, batch_size=2, mutate_ratio=1.0)
    eng.next_batch()
    assert eng.next_batch() is None


def test_merge_includes_partner_seed():
    a = _seed(0)
    b = _seed(1, code=DIFFERENT)
    prompts = []

    def gen(p, n):
        prompts.append(p)
        return []

    eng = RegressionSeedEngine([a, b], gen, batch_size=2, mutate_ratio=1.0, operators=["merge"],
                               rng=random.Random(1))
    eng.next_batch()
    eng.next_batch()
    assert "SECOND program" in prompts[0]


def test_unknown_operator_rejected():
    with pytest.raises(ValueError, match="unknown operators"):
        RegressionSeedEngine([_seed(0)], lambda p, n: [], operators=["retype", "bogus"])


def test_similarity_ignores_comments_and_whitespace():
    assert similarity(PARENT, "// hi\n" + PARENT.replace("  ", " ")) == 1.0
    assert similarity(PARENT, DIFFERENT) < 0.5


# -- feedback ------------------------------------------------------------------


def test_valid_mutants_become_parents_and_invalid_lower_op_weight():
    eng = RegressionSeedEngine([_seed(0)], lambda p, n: [], operators=["retype", "loops"])
    meta = {"origin": "mutant", "op": "retype", "parent": "s000", "root_seed": "s000", "partner": None}
    for _ in range(5):
        eng.record(DIFFERENT, dict(meta), valid=False)
    out = eng.record(DIFFERENT, dict(meta, op="loops"), valid=True)

    assert out["mutant_id"] == "m000005"
    assert [p.id for p in eng.mutant_pool] == ["m000005"]
    assert eng.op_stats["retype"].weight < eng.op_stats["loops"].weight
    # seeds pass through untouched
    seed_meta = {"origin": "seed", "seed_id": "s000"}
    assert eng.record(PARENT, seed_meta, valid=True) is seed_meta
    assert len(eng.mutant_pool) == 1


def test_unknown_validity_never_enters_pool():
    eng = RegressionSeedEngine([_seed(0)], lambda p, n: [])
    eng.record(DIFFERENT, {"origin": "mutant", "op": "retype", "root_seed": "s000"}, valid=None)
    assert not eng.mutant_pool and eng.op_stats["retype"].attempts == 0


def test_restore_from_provenance(tmp_path):
    seeds = [_seed(i) for i in range(4)]
    mfile = tmp_path / "9.fuzz"
    mfile.write_text(DIFFERENT)
    prov = [
        {"origin": "seed", "seed_id": "s000"},
        {"origin": "seed", "seed_id": "s001"},
        {"origin": "mutant", "op": "loops", "mutant_id": "m000000", "root_seed": "s000",
         "valid": True, "file": str(mfile)},
        {"origin": "mutant", "op": "loops", "mutant_id": "m000001", "root_seed": "s000",
         "valid": False, "file": "missing"},
    ]
    eng = RegressionSeedEngine(seeds, lambda p, n: [], batch_size=10)
    eng.restore(prov, lambda p: open(p).read() if os.path.exists(p) else None)

    assert eng.cursor == 2
    assert [m["seed_id"] for _, m in eng.next_batch()] == ["s002", "s003"]
    assert [p.code for p in eng.mutant_pool] == [DIFFERENT]
    assert eng.op_stats["loops"].attempts == 2 and eng.op_stats["loops"].valid == 1
    assert eng.record(DIFFERENT, {"origin": "mutant", "op": "loops", "root_seed": "s000"}, True)[
        "mutant_id"] == "m000002"


# -- fuzz-loop wiring -----------------------------------------------------------


class _FakeTarget:
    """Just enough of Target for Fuzz4All/fuzz.py's loop."""

    def __init__(self):
        self.prompt_used = {"begin": "#include <bits/stdc++.h>"}
        self.generated = 0

    def initialize(self):
        pass

    def generate(self, **kw):
        self.generated += 1
        return ["int main() { return 0; }\n"] * 2

    def _backend_generate(self, prompt, n=1, temperature=None):
        return [_fenced(DIFFERENT), _fenced(DIFFERENT.replace("* 7", "* 11"))][:n]

    def validate_individual(self, file_name):
        from Fuzz4All.target.target import FResult
        code = open(file_name).read()
        # pretend: the second mutant variant fails to compile
        return (FResult.FAILURE, "err") if "* 11" in code else (FResult.SAFE, "ok")

    def parse_validation_message(self, *a):
        pass

    def update(self, **kw):
        pass


def test_fuzz_loop_end_to_end(tmp_path):
    from Fuzz4All.fuzz import fuzz
    from Fuzz4All.huf_saem.huf_saem_coordinator import HUFSAEMCoordinator

    rows = [{"src": f"torture/pr{i}.C", "id": f"t{i}", "accepted": True, "class": "run",
             "bytes": 100, "known_bug": []} for i in range(3)]
    manifest, seed_dir = _write_manifest(tmp_path, rows)
    out = tmp_path / "run"
    out.mkdir()
    config = {
        "fuzzing": {"output_folder": str(out), "resume": False},
        "target": {"language": "cpp"},
        "llm": {"batch_size": 2, "temperature": 0.8},
        "huf_saem": {"phase2": {
            "enabled": True, "mode": "regression_seeds", "seed_manifest": manifest,
            "seed_dir": seed_dir, "mutate_ratio": 1.0, "max_similarity": 0.95,
        }},
    }
    target = _FakeTarget()
    coord = HUFSAEMCoordinator(config, target)
    coord.initialize()
    assert coord._p2_mutator_registry is None, "legacy regex mutators must stay off"

    fuzz(target, number_of_iterations=9, total_time=1, output_folder=str(out),
         resume=False, otf=True, coordinator=coord)

    prov = [json.loads(l) for l in open(out / "phase2_provenance.jsonl")]
    origins = [p["origin"] for p in prov]
    assert origins[:3] == ["seed"] * 3 and set(origins[3:]) == {"mutant"}
    assert [p["file"] for p in prov[:3]] == [str(out / f"{i}.fuzz") for i in range(3)]
    # Once a mutant is a parent, the fake LLM echoes it back: a near-copy,
    # rejected, so that iteration falls back to ordinary generation (2 files,
    # no provenance row). Every file is accounted for one way or the other.
    n_files = len([f for f in os.listdir(out) if f.endswith(".fuzz")])
    assert n_files == len(prov) + 2 * target.generated
    assert len({p["file"] for p in prov}) == len(prov)
    # seeds are written verbatim (no provenance header, no regex mutation)
    assert open(out / "0.fuzz").read() == PARENT
    # only compiling mutants become parents
    valid = [p for p in prov if p["origin"] == "mutant" and p["valid"]]
    assert len(coord._p2_engine.mutant_pool) == len(valid) > 0
    assert all(p["result"] in ("SAFE", "FAILURE") for p in prov)


def test_unknown_mode_rejected(tmp_path):
    from Fuzz4All.huf_saem.huf_saem_coordinator import HUFSAEMCoordinator

    config = {"fuzzing": {"output_folder": str(tmp_path)}, "target": {"language": "cpp"},
              "huf_saem": {"phase2": {"enabled": True, "mode": "nope"}}}
    with pytest.raises(ValueError, match="phase2.mode"):
        HUFSAEMCoordinator(config, _FakeTarget()).initialize()


def test_missing_seed_files_is_a_clear_error(tmp_path):
    from Fuzz4All.huf_saem.huf_saem_coordinator import HUFSAEMCoordinator

    config = {"fuzzing": {"output_folder": str(tmp_path)}, "target": {"language": "cpp"},
              "huf_saem": {"phase2": {"enabled": True, "mode": "regression_seeds",
                                      "seed_manifest": "nope.jsonl", "seed_dir": "nope"}}}
    with pytest.raises(ValueError, match="extract_seeds.py"):
        HUFSAEMCoordinator(config, _FakeTarget()).initialize()


def test_every_operator_has_an_instruction():
    assert set(OPERATORS) == {"retype", "deepen", "loops", "merge", "compute"}
