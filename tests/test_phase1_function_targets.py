"""tree-sitter function targets: region slicing, filters, selection, feedback.

Synthetic source + synthetic lcov data; GCC itself is only needed by the last
test (skipped without the instrumented build).
"""

from __future__ import annotations

import os

import pytest

from Fuzz4All.huf_saem import phase1_function_targets as ft

SOURCE = """\
/* Fold the thing.  */

static int
fold_thing (int a, int b)
{
  int r = 0;
  if (a > 0)
    {
      r = a;
      if (b == 42)
        {
          r += 1;
          r *= 2;
          r -= 3;
        }
    }
  else
    {
      r = b;
    }
  return r;
}

/* Never called.  */

static int
unused_helper (int x)
{
  int y = x * 2;
  y += 1;
  y -= 4;
  return y;
}

static void
debug_thing (int x)
{
  x += 1;
  x += 2;
  x += 3;
}
"""
# line numbers in SOURCE
FOLD = (4, 22)
B42_GUARD, B42_BODY = 10, (11, 15)
UNUSED = (27, 34)
DEBUG = (36, 42)


def _cov():
    lines = {}
    for ln in (6, 7, 9, 10, 21):            # fold_thing ran, b == 42 never true
        lines[ln] = 5
    for ln in (12, 13, 14):                 # inside the never-run b == 42 body
        lines[ln] = 0
    lines[19] = 2                           # else branch ran
    for ln in (29, 30, 31, 32):             # unused_helper never entered
        lines[ln] = 0
    for ln in (38, 39, 40, 41):             # debug_thing never entered, but filtered
        lines[ln] = 0
    fns = [(FOLD[0], FOLD[1], "_ZL10fold_thingii", 5), (UNUSED[0], UNUSED[1], "_ZL13unused_helperi", 0),
           (DEBUG[0], DEBUG[1], "_ZL11debug_thingi", 0)]
    return {"tree-ssa-thing.cc": {"lines": lines, "fns": fns}}


@pytest.fixture
def src_tree(tmp_path, monkeypatch):
    (tmp_path / "tree-ssa-thing.cc").write_text(SOURCE)
    monkeypatch.setattr(ft, "SRC", str(tmp_path) + "/")
    return tmp_path


def test_area_kind_and_skips():
    assert ft.area_kind("cp/pt.cc") == "cxx_frontend"
    assert ft.area_kind("tree-vect-loop.cc") == "optimiser"
    assert ft.area_kind("gimple-match-3.cc") is None          # generated match.pd code
    assert ft.area_kind("omp-low.cc") is None
    assert ft.area_kind("diagnostic.cc") is None
    assert ft.short_name("(anonymous namespace)::pass_x::execute(function*)") == "pass_x::execute"


def test_extract_partial_and_entry_targets(src_tree):
    ts = {t["id"]: t for t in ft.extract_targets(_cov())}
    part = ts[f"fn:tree-ssa-thing.cc:{B42_BODY[0]}"]
    assert part["target_type"] == "partial" and part["kind"] == "optimiser"
    assert f"line {B42_GUARD}: if (b == 42)" in part["guard_chain"]
    assert "line 7: if (a > 0)" in part["guard_chain"]
    assert "r += 1;" in part["slice"]
    assert part["check_lines"] == [12, 13, 14] and part["uncovered_lines"] == 3
    assert part["entry_line"] == 6

    entry = ts[f"fn:tree-ssa-thing.cc:{UNUSED[0]}"]
    assert entry["target_type"] == "entry" and "Never called" in entry["comment"]
    assert "unused_helper (int x)" in entry["slice"]

    assert not any("debug_thing" in t["function"] for t in ts.values()), "debug_* is filtered"
    assert not any(t["target_type"] == "partial" and "r = b" in t["slice"] for t in ts.values()), \
        "regions that ran are not targets"


def test_frontier_only_guard_must_have_run(src_tree):
    cov = _cov()
    cov["tree-ssa-thing.cc"]["lines"][B42_GUARD] = 0      # guard itself never ran
    ts = ft.extract_targets(cov)
    assert not any(t["target_type"] == "partial" for t in ts)


def test_select_balances_types_and_spreads():
    mk = lambda i, typ, f, n: {"id": str(i), "kind": "optimiser", "target_type": typ, "file": f,
                               "function": f"fn{i}", "uncovered_lines": n}
    ts = [mk(i, "entry", "a.cc", 100 - i) for i in range(10)] + [mk(20 + i, "partial", "b.cc", 5) for i in range(10)]
    sel = ft.select(ts, per_kind=4, per_file=6)
    assert sorted(t["target_type"] for t in sel) == ["entry", "entry", "partial", "partial"]
    sel = ft.select(ts, per_kind=20, per_file=3)
    assert sum(t["file"] == "a.cc" for t in sel) == 3


def test_check_target_progress_feedback(monkeypatch):
    t = {"file": "x.cc", "function_lines": [100, 200], "entry_line": 102, "check_lines": [150, 151],
         "guard_chain": "  line 120: if (a)\n  line 140: if (b)"}
    monkeypatch.setattr(ft, "line_counts", lambda src, rel, lines: ({102: 3, 120: 3, 140: 2, 150: 0, 151: 0}, ""))
    hit, err, progress = ft.check_target("int f();", t)
    assert not hit and "line 120 ran 3 times" in progress and "deepest condition reached is at line 140" in progress
    monkeypatch.setattr(ft, "line_counts", lambda src, rel, lines: ({ln: 0 for ln in lines}, ""))
    assert ft.check_target("int f();", t)[2] == "GCC never entered this function at all."
    monkeypatch.setattr(ft, "line_counts", lambda src, rel, lines: ({150: 1}, ""))
    assert ft.check_target("int f();", t)[0] is True
    monkeypatch.setattr(ft, "line_counts", lambda src, rel, lines: (None, "error: boom"))
    assert ft.check_target("int f();", t) == (False, "error: boom", "")


@pytest.mark.skipif(not os.path.exists(ft.GXX), reason="instrumented GCC not built")
def test_line_counts_against_real_gcc():
    # tree-vect-loop.cc:10009 = vectorizable_induction (GCC 14.3): reached for a
    # simple induction loop at -O2, not for a trivial function.
    loop = "void g(int*a,int n){for(int i=0;i<n;i++)a[i]=i*3;}"
    counts, err = ft.line_counts(loop, "tree-vect-loop.cc", [10009])
    assert not err and counts[10009] > 0
    counts, _ = ft.line_counts("int f(int a){return a+1;}", "tree-vect-loop.cc", [10009])
    assert counts[10009] == 0
    counts, err = ft.line_counts("int f(int a){return a+;}", "tree-vect-loop.cc", [10009])
    assert counts is None and "error" in err
