"""Phase 1 `targets` mode feeding Phase 2's engine, and the rule-firing check."""

from __future__ import annotations

import json
import os
import random

import pytest

from Fuzz4All.huf_saem.phase2_regression_seeds import RegressionSeedEngine, Seed, Target

DIFFERENT = """#include <cstdio>
int g(int a, int b) { int r = 0; for (int i = 0; i < b; ++i) r ^= a * i; return r; }
int main() { printf("%d\\n", g(7, 9)); }
"""


def _seeds():
    return [Seed("a", "opt/pr1.C", "int main() { return 0; }\n", "run"),
            Seed("b", "cpp0x/pr2.C", "int main() { return 1; }\n", "run"),
            Seed("c", "tree-ssa/pr3.C", "int main() { return 2; }\n", "run")]


def _target(**kw):
    t = dict(id="matchpd:207", hint="For a signed int x, compute (x + (x >> 31)) ^ (x >> 31).",
             example="int f(int x) { return (x + (x >> 31)) ^ (x >> 31); }",
             span=(207, 212), related_dirs=["opt", "tree-ssa"])
    t.update(kw)
    return Target(**t)


def test_targeted_batch_uses_hint_example_and_related_seed():
    prompts = []

    def gen(p, n):
        prompts.append(p)
        return [f"```cpp\n{DIFFERENT}```"] * n

    eng = RegressionSeedEngine(_seeds(), gen, batch_size=2, mutate_ratio=1.0,
                               targets=[_target()], p_targeted=1.0, rng=random.Random(4))
    for _ in range(2):
        eng.next_batch()                  # the three seeds
    parents = set()
    for _ in range(12):
        batch = eng.next_batch() or []
        for _, m in batch:
            assert m["op"] == "target" and m["target"] == "matchpd:207"
            parents.add(m["parent"])
    assert parents <= {"a", "c"}, "parents come from the target's related test dirs"
    assert "(x + (x >> 31)) ^ (x >> 31)" in prompts[0] and "int f(int x)" in prompts[0]
    assert eng.counts["targeted_batches"] == 12


def test_targets_absent_means_no_targeting_and_no_rng_draw():
    a = RegressionSeedEngine(_seeds(), lambda p, n: [], rng=random.Random(1))
    b = RegressionSeedEngine(_seeds(), lambda p, n: [], rng=random.Random(1), p_targeted=1.0)
    assert b.p_targeted == 0.0
    assert a.rng.random() == b.rng.random()


def test_feedback_makes_hit_targets_rarer():
    t1, t2 = _target(id="t1"), _target(id="t2")
    eng = RegressionSeedEngine(_seeds(), lambda p, n: [], targets=[t1, t2], p_targeted=1.0)
    eng.target_feedback("t1", True)
    eng.target_feedback("t2", False)
    eng.target_feedback("unknown", True)      # ignored
    assert t1.weight < t2.weight
    assert eng.counts["target_hits"] == 1 and eng.counts["target_checks"] == 2
    assert eng.stats()["targets_hit"] == 1


# -- coordinator -----------------------------------------------------------------


class _FakeTarget:
    prompt_used = {"begin": ""}

    def _backend_generate(self, prompt, n=1, temperature=None, seed=None):
        return []


def _write(tmp_path):
    seeds_dir = tmp_path / "seeds"
    seeds_dir.mkdir()
    (seeds_dir / "s1.cpp").write_text("// seed s1 from x\nint main() { return 0; }\n")
    (tmp_path / "manifest.jsonl").write_text(json.dumps(
        {"src": "opt/pr1.C", "id": "s1", "accepted": True, "class": "run", "bytes": 20, "known_bug": []}) + "\n")
    targets = [{"id": "matchpd:207", "match_pd_lines": [207, 212], "related_test_dirs": ["opt"], "reachable": True},
               {"id": "matchpd:300", "match_pd_lines": [300, 305], "related_test_dirs": [], "reachable": True}]
    (tmp_path / "targets.jsonl").write_text("".join(json.dumps(t) + "\n" for t in targets))
    hints = [{"id": "matchpd:207", "triggerable": True, "validated": True, "hint": "h1", "example": "int f();"},
             {"id": "matchpd:300", "triggerable": True, "validated": False, "hint": "h2", "example": "int g();"}]
    (tmp_path / "hints.jsonl").write_text("".join(json.dumps(h) + "\n" for h in hints))


def _config(tmp_path, **p1):
    return {
        "fuzzing": {"output_folder": str(tmp_path / "run")},
        "target": {"language": "cpp"},
        "llm": {"batch_size": 2},
        "huf_saem": {
            "phase1": dict(enabled=True, mode="targets", targets_file=str(tmp_path / "targets.jsonl"),
                           hints_file=str(tmp_path / "hints.jsonl"), p_targeted=0.7, **p1),
            "phase2": {"enabled": True, "mode": "regression_seeds",
                       "seed_manifest": str(tmp_path / "manifest.jsonl"), "seed_dir": str(tmp_path / "seeds")},
        },
    }


def test_coordinator_loads_validated_hints_into_engine(tmp_path):
    from Fuzz4All.huf_saem.huf_saem_coordinator import HUFSAEMCoordinator
    _write(tmp_path)
    c = HUFSAEMCoordinator(_config(tmp_path), _FakeTarget())
    c.initialize()
    assert list(c._p2_engine.targets) == ["matchpd:207"]      # unvalidated hint skipped
    assert c._p2_engine.p_targeted == 0.7

    c2 = HUFSAEMCoordinator(_config(tmp_path, use_hints="triggerable"), _FakeTarget())
    c2.initialize()
    assert sorted(c2._p2_engine.targets) == ["matchpd:207", "matchpd:300"]


def test_coordinator_checks_hits_and_logs_them(tmp_path, monkeypatch):
    from Fuzz4All.huf_saem import phase1_target_hints
    from Fuzz4All.huf_saem.huf_saem_coordinator import HUFSAEMCoordinator
    from Fuzz4All.target.target import FResult
    _write(tmp_path)
    calls = []
    monkeypatch.setattr(phase1_target_hints, "rule_fires",
                        lambda code, span: (calls.append(span) or (True, "", [208])))
    c = HUFSAEMCoordinator(_config(tmp_path), _FakeTarget())
    c.initialize()
    os.makedirs(tmp_path / "run")
    meta = {"origin": "mutant", "op": "target", "target": "matchpd:207", "parent": "s1", "root_seed": "s1"}
    c.record_phase2(str(tmp_path / "run" / "0.fuzz"), DIFFERENT, meta, FResult.SAFE)
    row = json.loads((tmp_path / "run" / "phase2_provenance.jsonl").read_text())
    assert row["target_hit"] is True and calls == [(207, 212)]
    assert c._p2_engine.targets["matchpd:207"].hits == 1


def test_bad_phase1_mode_and_missing_files(tmp_path):
    from Fuzz4All.huf_saem.huf_saem_coordinator import HUFSAEMCoordinator
    _write(tmp_path)
    cfg = _config(tmp_path)
    cfg["huf_saem"]["phase1"]["mode"] = "nope"
    with pytest.raises(ValueError, match="phase1.mode"):
        HUFSAEMCoordinator(cfg, _FakeTarget()).initialize()
    cfg = _config(tmp_path)
    cfg["huf_saem"]["phase1"]["hints_file"] = "missing.jsonl"
    with pytest.raises(ValueError, match="hints_file"):
        HUFSAEMCoordinator(cfg, _FakeTarget()).initialize()


# -- the GCC-backed rule check ------------------------------------------------------


@pytest.mark.skipif(not os.path.exists("/home/ec2-user/gcc-cov/bin/g++"), reason="instrumented GCC not built")
def test_rule_fires_against_real_gcc():
    from Fuzz4All.huf_saem.phase1_target_hints import rule_fires
    # match.pd 207-212 (GCC 14.3): (X + (X >> (prec-1))) ^ (X >> (prec-1)) -> abs (X)
    fired, err, applied = rule_fires("int f(int x){return (x+(x>>31))^(x>>31);}", (207, 212))
    assert fired and not err and 212 in applied
    assert rule_fires("int f(int x){return x+1;}", (207, 212))[0] is False
    fired, err, _ = rule_fires("int f(int x){return x+;}", (207, 212))
    assert not fired and "error" in err
