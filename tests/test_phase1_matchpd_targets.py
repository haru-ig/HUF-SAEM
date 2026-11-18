"""match.pd target builder: parsing, site->rule mapping, coverage, reachability.

Uses a small synthetic match.pd plus fake generated files and lcov data, so no
GCC build is needed.
"""

from __future__ import annotations

import json

from Fuzz4All.huf_saem.phase1_matchpd_targets import (
    build_targets,
    extract_rules,
    parse_forms,
    reachability,
    rule_index,
)

MATCH_PD = """\
/* Header comment. */
(define_operator_list cmp lt le)

/* Fold X ^ X to zero.  */
(simplify
 (bit_xor @0 @0)
 { build_zero_cst (type); })

#if GIMPLE
/* Abs pattern: (X + (X >> p)) ^ (X >> p) -> abs (X).  */
(simplify
 (bit_xor (plus @0 (rshift@2 @0 INTEGER_CST@1)) @2)
 (abs @0))
#endif

(for op (trunc_div exact_div)
 (simplify
  (op (plus @0 INTEGER_CST@1) INTEGER_CST@2)
  (with
   {
     bool odd = true;
#define helper(v) (odd ? (v) \\
                        : (v))
     const char *s = "( not a paren";
   }
   (plus (op @0 @2) { helper (@1); }))))
#undef helper

(simplify
 (floor_div @0 integer_onep)
 @0)

(match (power_of_two_cand @1)
 INTEGER_CST@1)
"""

# line numbers of interest in MATCH_PD (1-based)
XOR_RESULT = 7     # "{ build_zero_cst ..." -> rule at 5
ABS_RESULT = 13    # "(abs @0))"           -> rule at 11
FOR_RESULT = 26    # "(plus (op @0 @2) ..." -> rule at 17
FLOOR_RESULT = 31  # "@0)"                 -> rule at 29


def _gen(tmp_path, flavour, table, sites):
    """A fake generated file: a dbg_line_numbers table + logging calls."""
    body = ["// generated", f"static int dbg_line_numbers[{len(table)}] = {{" +
            ", ".join(map(str, table)) + "};"]
    for idx, simplify in sites:
        body.append(f'  if (UNLIKELY (debug_dump)) {flavour}_dump_logs ("match.pd", {idx}, '
                    f'__FILE__, __LINE__, {"true" if simplify else "false"});')
    (tmp_path / f"{flavour}-match-1.cc").write_text("\n".join(body) + "\n")
    # logging calls start at line 3
    return {i: 3 + k for k, (i, _) in enumerate(sites)}


def test_parse_handles_comments_strings_defines_and_sections():
    forms = parse_forms(MATCH_PD)
    assert all(f.end for f in forms), "every form must close"
    rules = extract_rules(MATCH_PD)
    assert [(r.kind, r.start) for r in rules] == [
        ("simplify", 5), ("simplify", 11), ("simplify", 17), ("simplify", 29), ("match", 33)]
    by = {r.start: r for r in rules}
    assert by[11].section == "GIMPLE" and by[5].section == "both"
    assert by[17].for_headers == ["(for op (trunc_div exact_div)"]
    assert by[17].end == FOR_RESULT
    assert "Fold X ^ X" in by[5].comment and "Abs pattern" in by[11].comment
    assert "bit_xor" in by[5].operators


def test_rule_index_maps_result_lines_to_their_rule():
    idx = rule_index(extract_rules(MATCH_PD))
    assert idx[XOR_RESULT].start == 5
    assert idx[ABS_RESULT].start == 11
    assert idx[FOR_RESULT].start == 17
    assert 3 not in idx


def test_reachability():
    by = {r.start: r for r in extract_rules(MATCH_PD)}
    assert reachability(by[29])[0] is False          # floor_div only
    assert reachability(by[17])[0] is True           # trunc_div iteration is fine
    assert reachability(by[5]) == (True, [])


def test_build_targets_uses_both_tables_and_coverage(tmp_path):
    (tmp_path / "match.pd").write_text(MATCH_PD)
    # gimple table: id0 -> XOR, id1 -> ABS, id2 -> predicate line
    g = _gen(tmp_path, "gimple", [XOR_RESULT, ABS_RESULT, 34], [(0, True), (1, True), (2, False)])
    # generic table uses its OWN ids: id0 -> FOR, id1 -> FLOOR
    (tmp_path / "x").mkdir()
    gen = _gen(tmp_path, "generic", [FOR_RESULT, FLOOR_RESULT], [(0, True), (1, True)])
    # coverage: only the gimple XOR site was reached
    info = tmp_path / "cov.info"
    info.write_text(
        f"SF:/b/gcc/gimple-match-1.cc\nDA:{g[0]},5\nDA:{g[1]},0\nend_of_record\n"
        f"SF:/b/gcc/generic-match-1.cc\nDA:{gen[0]},0\nDA:{gen[1]},0\nend_of_record\n")

    targets, stats = build_targets(str(tmp_path / "match.pd"), str(tmp_path), [str(info)])
    assert stats["rules_traceable"] == 4          # the (match) predicate is not a transform
    assert stats["rules_applied"] == 1
    ids = {t["id"]: t for t in targets}
    assert set(ids) == {"matchpd:11", "matchpd:17", "matchpd:29"}
    assert ids["matchpd:29"]["reachable"] is False
    assert ids["matchpd:17"]["rule"].startswith(" (simplify")
    assert ids["matchpd:11"]["section"] == "GIMPLE"
    json.dumps(targets)  # serialisable
