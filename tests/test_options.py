"""Per-program compiler options: header, allowlist, classification, and their
plumbing through Phase 2, the seed loader, the corpus table and the oracle."""

from __future__ import annotations

import json
import os
import random
import shutil

import pytest

from Fuzz4All.huf_saem import options as o
from Fuzz4All.huf_saem.phase2_regression_seeds import RegressionSeedEngine, Seed, load_seeds

BODY = "int main() { return 0; }\n"
DIFFERENT = """#include <cstdio>
int g(int a, int b) { int r = 0; for (int i = 0; i < b; ++i) r ^= a * i; return r; }
int main() { printf("%d\\n", g(7, 9)); }
"""


# -- header ------------------------------------------------------------------


def test_header_roundtrip():
    c = o.set_options(BODY, ["-O3", "-mavx2"])
    assert c.startswith("// huf-options: -O3 -mavx2\n")
    assert o.parse_options(c) == ["-O3", "-mavx2"]
    assert o.strip_options(c) == BODY
    assert o.set_options(c, ["-Os"]) == "// huf-options: -Os\n" + BODY      # replaced, not stacked
    assert o.set_options(c, []) == BODY
    assert o.parse_options(BODY) == []
    assert o.parse_options("int x;\n// huf-options: -O3\n") == [], "only the first line counts"


def test_read_options_from_file(tmp_path):
    p = tmp_path / "1.fuzz"
    p.write_text(o.set_options(BODY, ["-O1", "-fno-inline"]))
    assert o.read_options(str(p)) == ["-O1", "-fno-inline"]
    assert o.read_options(str(tmp_path / "missing.fuzz")) == []


# -- allowlist -----------------------------------------------------------------


@pytest.mark.parametrize("opt", [
    "-O", "-O0", "-O3", "-Os", "-Og", "-Ofast", "-ffast-math", "-fno-tree-forwprop", "-fipa-cp-clone",
    "-funroll-loops", "-fchecking", "-fcf-protection", "-fsanitize=undefined", "-fsanitize=address,undefined",
    "-march=x86-64-v3", "-march=skylake-avx512", "-march=core-avx2", "-mtune=i686", "-mavx2", "-mno-avx",
    "-mfma", "-mprefer-vector-width=512", "-mcmodel=medium", "-g", "-g3", "-gdwarf-5", "-DFOO", "-DN=3",
    "-UBAR", "-fopenmp", "-flto",
])
def test_allowed(opt):
    assert o.is_allowed(opt)


@pytest.mark.parametrize("opt", [
    "-fdump-tree-all", "-fplugin=x.so", "-fprofile-use", "-fsyntax-only", "-fpreprocessed", "-fstack-usage",
    "-fsanitize=thread", "-march=armv8-a", "-march=i686", "-mthumb", "-mcpu=cortex-a57", "-m32", "-std=c++17",
    "-Wall", "-Werror", "-o", "-DX='a b'", "-fsave-optimization-record", "-fmodules-ts",
])
def test_rejected(opt):
    assert not o.is_allowed(opt)


def test_sanitize_dedupes_and_reports():
    kept, dropped = o.sanitize(["-O3", "-mavx2", "-O3", "-mthumb", "-Wall"])
    assert kept == ["-O3", "-mavx2"] and dropped == ["-mthumb", "-Wall"]


def test_level_helpers_and_value_changing():
    assert o.level_of(["-O1", "-fno-inline", "-O3"]) == "-O3"
    assert o.level_of(["-fno-inline"]) is None
    assert o.without_level(["-O3", "-mavx2"]) == ["-mavx2"]
    assert o.value_changing(["-O2", "-ffast-math"]) and o.value_changing(["-Ofast"])
    assert not o.value_changing(["-O3", "-funroll-loops", "-mavx2"])


def test_oracle_mode_uses_host_cpu(monkeypatch):
    monkeypatch.setattr(o, "host_cpu_flags", lambda: frozenset({"avx", "avx2", "fma", "sse4_1", "sse4_2", "ssse3"}))
    monkeypatch.setattr(o, "_isa_needed",
                        lambda cpu, comp: frozenset({"avx512f"}) if any("avx512" in c or "v4" in c for c in cpu)
                        else frozenset({"avx2"}))
    assert o.oracle_mode(["-O3"]) == "full"
    assert o.oracle_mode(["-mavx2"]) == "full"
    assert o.oracle_mode(["-march=x86-64-v4"]) == "compile_only"
    assert o.oracle_mode(["-mavx2", "-ffast-math"]) == "crash_only"


# -- Phase 2 --------------------------------------------------------------------


def test_mutants_inherit_parent_options_and_llm_never_sees_them():
    prompts = []
    llm_out = f"```cpp\n// huf-options: -O0 -fdump-tree-all\n{DIFFERENT}```"   # model tries to set its own

    def gen(p, n):
        prompts.append(p)
        return [llm_out] * n

    seed = Seed("s", "opt/pr1.C", o.set_options(BODY, ["-O3", "-mavx2"]), "run")
    eng = RegressionSeedEngine([seed], gen, batch_size=1, mutate_ratio=1.0, rng=random.Random(0))
    assert eng.next_batch()[0][0].startswith("// huf-options: -O3 -mavx2")   # seed emitted with header
    code, meta = eng.next_batch()[0]
    assert o.parse_options(code) == ["-O3", "-mavx2"]
    assert "huf-options" not in prompts[0]
    assert "-fdump-tree-all" not in code


def test_dedupe_distinguishes_options():
    outs = iter([[f"```cpp\n{DIFFERENT}```"], [f"```cpp\n{DIFFERENT}```"]])
    a = Seed("a", "x/1.C", o.set_options(BODY, ["-O1"]), "run")
    b = Seed("b", "x/2.C", o.set_options(BODY, ["-O3"]), "run")
    eng = RegressionSeedEngine([a, b], lambda p, n: next(outs), batch_size=2, mutate_ratio=1.0,
                               dedupe=True, parent_mutant_ratio=0.0, rng=random.Random(2))
    eng.next_batch()
    got = []
    for _ in range(2):
        got += eng.next_batch() or []
    # same program text twice; kept twice only if the parents' options differed
    expect = 2 if len({o.parse_options(c)[0] for c, _ in got}) == 2 else 1
    assert len(got) == expect


def test_load_seeds_attaches_allowlisted_manifest_options(tmp_path):
    (tmp_path / "seeds").mkdir()
    (tmp_path / "seeds" / "s1.cpp").write_text("// seed s1 from x\n" + BODY)
    (tmp_path / "manifest.jsonl").write_text(json.dumps(
        {"src": "opt/pr1.C", "id": "s1", "accepted": True, "class": "run", "bytes": 20, "known_bug": [],
         "options": ["-O3", "-mthumb", "-fno-tree-vrp"]}) + "\n")
    s = load_seeds(str(tmp_path / "manifest.jsonl"), str(tmp_path / "seeds"))
    assert o.parse_options(s[0].code) == ["-O3", "-fno-tree-vrp"]


def test_corpus_table_cli(tmp_path, capsys):
    (tmp_path / "0.fuzz").write_text(o.set_options(BODY, ["-O3", "-fdump-tree-all"]))
    (tmp_path / "1.fuzz").write_text(BODY)
    o.main([str(tmp_path)])
    lines = capsys.readouterr().out.splitlines()
    assert lines == [f"{tmp_path}/0.fuzz\t-O3", f"{tmp_path}/1.fuzz\t"]


# -- oracle against the real compiler ---------------------------------------------


@pytest.mark.skipif(not shutil.which("gcc14-g++"), reason="needs gcc14-g++")
def test_oracle_applies_options(tmp_path):
    from Fuzz4All.huf_saem.oracle import DifferentialOracle
    orc = DifferentialOracle(compilers=["gcc14-g++"], bug_dir=None, sanitize=False)

    def check(name, opts, body):
        p = tmp_path / f"{name}.fuzz"
        p.write_text(o.set_options(body, opts))
        return orc.check(str(p))

    plain = '#include <cstdio>\nint main(){ volatile int n=5; int s=0; for(int i=0;i<n;i++) s+=i; printf("%d\\n",s); }\n'
    assert check("ok", ["-O3", "-mavx2", "-funroll-loops"], plain).kind == "safe"
    assert check("fm", ["-ffast-math"], plain).kind == "unchecked_crash_only"
    if not o.runs_on_host(["-march=x86-64-v4"]):
        assert check("v4", ["-march=x86-64-v4"], plain).kind == "unchecked_compile_only"
    # an option the program needs to compile at all must reach every level
    needs_d = '#include <cstdio>\nint main(){ printf("%d\\n", FOO); }\n'
    assert check("d", ["-DFOO=3"], needs_d).kind == "safe"
    # the known-divergence control still fires with options present
    # (same control as tests/test_oracle_controls.py: 0 at -O0, 1 once optimised)
    cp = '#include <cstdio>\nint main(){ int x = 42; printf("constant_p=%d\\n", __builtin_constant_p(x * 2)); }\n'
    assert check("cp", ["-fno-inline"], cp).kind == "wrong_code"
