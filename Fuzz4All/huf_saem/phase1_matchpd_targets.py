"""Phase 1 (redesign), target kind 1: match.pd simplification rules.

GCC's algebraic simplifier is written in match.pd, a Lisp-like rule language
that genmatch compiles into gimple-match-*.cc / generic-match-*.cc. Each
transform in the generated code carries a logging call

    if (UNLIKELY (debug_dump)) gimple_dump_logs ("match.pd", ID, __FILE__, __LINE__, true);

where ID indexes a per-flavour table `dbg_line_numbers[]` (one table in a
gimple-match file, a separate one in a generic-match file) holding the
match.pd line of that transform's result expression. Coverage of the logging
line therefore says "this transform was reached".

This module:
  1. parses match.pd into (simplify ...) / (match ...) rules with line spans,
     their enclosing (for ...) iterator headers, #if GIMPLE/GENERIC context
     and the developer comment in front of them -- the whole rule, never cut;
  2. maps every logging site back to its rule;
  3. given lcov .info files, marks which rules were applied;
  4. flags rules that C++ on x86-64 cannot trigger;
  5. writes the never-applied, plausibly reachable rules as targets.
"""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

_LOG_SITE = re.compile(r'_dump_logs \("match\.pd", (\d+), __FILE__, __LINE__, (true|false)\)')
_TABLE = re.compile(r"static int dbg_line_numbers\[(\d+)\] = \{([^}]*)\}")


# --------------------------------------------------------------------------- #
# match.pd parsing
# --------------------------------------------------------------------------- #


@dataclass
class Form:
    head: str
    start: int            # 1-based line of '('
    end: int = 0          # 1-based line of matching ')'
    parent: Optional["Form"] = field(default=None, repr=False)
    section: str = "both"  # GIMPLE / GENERIC / both (from #if)


def parse_forms(text: str) -> List[Form]:
    """Every parenthesised form with its head word and line span.

    Skips /* */ comments, string and char literals (C code in { } blocks can
    contain parentheses in them), and preprocessor lines, while tracking
    `#if GIMPLE` / `#if GENERIC` sections.
    """
    forms: List[Form] = []
    stack: List[Form] = []
    section_stack: List[str] = []
    i, n, line = 0, len(text), 1
    at_line_start = True
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            at_line_start = True
            i += 1
            continue
        if at_line_start and c == "#":
            j = text.find("\n", i)
            j = n if j == -1 else j
            directive = text[i:j].split()
            # A directive continues over backslash-newlines (multi-line
            # #define in C blocks); its parentheses are not rule structure.
            while j < n and text[j - 1] == "\\":
                line += 1
                k = text.find("\n", j + 1)
                j = n if k == -1 else k
            if directive and directive[0] in ("#if", "#ifdef", "#ifndef"):
                arg = directive[1] if len(directive) > 1 else ""
                section_stack.append("GIMPLE" if arg == "GIMPLE" else "GENERIC" if arg == "GENERIC" else "other")
            elif directive and directive[0] == "#else" and section_stack:
                flip = {"GIMPLE": "GENERIC", "GENERIC": "GIMPLE"}
                section_stack[-1] = flip.get(section_stack[-1], section_stack[-1])
            elif directive and directive[0] == "#endif" and section_stack:
                section_stack.pop()
            i = j
            continue
        if not c.isspace():
            at_line_start = False
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            line += text.count("\n", i, j)
            i = j
            continue
        if c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            line += text.count("\n", i, j)
            i = j + 1
            continue
        if c == "(":
            m = re.match(r"\(\s*([^\s()]+)", text[i:i + 200])
            sec = next((s for s in reversed(section_stack) if s in ("GIMPLE", "GENERIC")), "both")
            f = Form(head=m.group(1) if m else "", start=line,
                     parent=stack[-1] if stack else None, section=sec)
            stack.append(f)
            forms.append(f)
        elif c == ")" and stack:
            stack.pop().end = line
        i += 1
    return forms


@dataclass
class Rule:
    kind: str                 # "simplify" | "match"
    start: int
    end: int
    section: str
    text: str                 # the complete rule, never truncated
    for_headers: List[str]    # enclosing (for ...) iterator definitions
    comment: str              # developer comment right before the rule / its top form
    operators: List[str]


def _preceding_comment(lines: List[str], start: int) -> str:
    """The /* ... */ block ending just above line `start` (1-based), if any."""
    j = start - 2
    while j >= 0 and not lines[j].strip():
        j -= 1
    if j < 0 or not lines[j].rstrip().endswith("*/"):
        return ""
    k = j
    while k >= 0 and "/*" not in lines[k]:
        k -= 1
    return "\n".join(lines[max(k, 0):j + 1]).strip()


def extract_rules(text: str) -> List[Rule]:
    lines = text.splitlines()
    rules: List[Rule] = []
    for f in parse_forms(text):
        if f.head not in ("simplify", "match"):
            continue
        # A (match ...) nested in a simplify is part of it, not its own rule.
        anc, nested = f.parent, False
        fors: List[str] = []
        top = f
        while anc is not None:
            if anc.head in ("simplify", "match"):
                nested = True
            if anc.head == "for":
                fors.insert(0, lines[anc.start - 1].strip())
            top = anc
            anc = anc.parent
        if nested:
            continue
        body = "\n".join(lines[f.start - 1:f.end])
        comment = _preceding_comment(lines, f.start) or _preceding_comment(lines, top.start)
        ops = sorted(set(re.findall(r"\(\s*([a-z_][a-z0-9_]*)(?=[\s:@?)])", body)) -
                     {"simplify", "match", "if", "with", "for", "convert", "convert1", "convert2",
                      "view_convert", "switch"})
        rules.append(Rule(f.head, f.start, f.end, f.section, body, fors, comment, ops))
    return rules


def rule_index(rules: Sequence[Rule]) -> Dict[int, Rule]:
    """match.pd line -> the rule whose span contains it (innermost wins)."""
    idx: Dict[int, Rule] = {}
    for r in sorted(rules, key=lambda r: r.end - r.start, reverse=True):
        for ln in range(r.start, r.end + 1):
            idx[ln] = r
    return idx


# --------------------------------------------------------------------------- #
# generated code: logging sites
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Site:
    gen_file: str      # basename of the generated .cc
    gen_line: int      # line of the logging call in it
    pd_line: int       # match.pd line it reports
    simplify: bool     # True: a transform; False: a (match) predicate


def load_sites(build_gcc_dir: str) -> List[Site]:
    files = sorted(glob.glob(os.path.join(build_gcc_dir, "gimple-match-*.cc")) +
                   glob.glob(os.path.join(build_gcc_dir, "generic-match-*.cc")))
    tables: Dict[str, List[int]] = {}
    texts: Dict[str, str] = {}
    for f in files:
        t = open(f, errors="replace").read()
        texts[f] = t
        m = _TABLE.search(t)
        if m:
            flavour = "gimple" if os.path.basename(f).startswith("gimple") else "generic"
            tables[flavour] = [int(x) for x in m.group(2).replace("\n", " ").split(",") if x.strip()]
    if set(tables) != {"gimple", "generic"}:
        raise ValueError(f"expected one dbg_line_numbers table per flavour in {build_gcc_dir}, got {sorted(tables)}")
    sites: List[Site] = []
    for f, t in texts.items():
        flavour = "gimple" if os.path.basename(f).startswith("gimple") else "generic"
        table = tables[flavour]
        for lineno, ln in enumerate(t.splitlines(), 1):
            m = _LOG_SITE.search(ln)
            if m:
                sites.append(Site(os.path.basename(f), lineno, table[int(m.group(1))], m.group(2) == "true"))
    return sites


def hit_lines(info_path: str, wanted: Set[str]) -> Set[Tuple[str, int]]:
    """(basename, line) pairs with hits > 0 in an lcov file, for the wanted files."""
    hits: Set[Tuple[str, int]] = set()
    cur = None
    with open(info_path, errors="replace") as f:
        for ln in f:
            if ln.startswith("SF:"):
                b = os.path.basename(ln[3:].strip())
                cur = b if b in wanted else None
            elif cur and ln.startswith("DA:"):
                a, h = ln[3:].split(",")[:2]
                if int(h) > 0:
                    hits.add((cur, int(a)))
    return hits


def applied_rules(sites: Sequence[Site], idx: Dict[int, Rule], info_paths: Iterable[str]) -> Set[int]:
    """Start lines of rules with at least one reached transform site."""
    wanted = {s.gen_file for s in sites}
    hit: Set[Tuple[str, int]] = set()
    for p in info_paths:
        hit |= hit_lines(p, wanted)
    out: Set[int] = set()
    for s in sites:
        r = idx.get(s.pd_line)
        if r is not None and (s.gen_file, s.gen_line) in hit:
            out.add(r.start)
    return out


# --------------------------------------------------------------------------- #
# reachability from C++ on x86-64
# --------------------------------------------------------------------------- #

# Strong evidence a rule cannot fire for C++ code on x86-64: tree codes or
# types only other front ends or other targets produce.
_UNREACHABLE = [
    (r"\b(floor|ceil|round)_(div|mod)\b", "FLOOR/CEIL/ROUND div/mod: Ada/Fortran only"),
    (r"\bIFN_\w*LEN\w*|\bcond_len_\w+|vcond_mask_len", "length-predicated vector IFNs: RISC-V/s390 only"),
    (r"BITINT_TYPE|bitint", "_BitInt: C23, not C++"),
    (r"FIXED_POINT|fixed_convert|\bsat_", "fixed-point / saturating types: not C++"),
    (r"DECIMAL_FLOAT|dfp", "decimal floating point"),
]
# Softer: reachable only with vector code or target features beyond the
# default -march=x86-64 (worth keeping, but worth knowing).
_SOFT = [
    (r"\bvec_(cond|perm|duplicate|series)\b|VECTOR_TYPE_P|VECTOR_INTEGER_TYPE_P", "needs vector types"),
    (r"direct_internal_fn_supported_p|optab_handler|target_supports|expand_vec_",
     "target-dependent (may need -m flags)"),
    (r"COMPLEX_TYPE|\bcomplex\b|realpart|imagpart", "needs complex numbers"),
]


def reachability(rule: Rule) -> Tuple[bool, List[str]]:
    text = rule.text + "\n" + "\n".join(rule.for_headers)
    hard = [why for pat, why in _UNREACHABLE if re.search(pat, text, re.I)]
    # A (for ...) over many operators is not unreachable just because one
    # iteration (e.g. floor_div) is; only hard-exclude when no ordinary
    # integer/float operator remains in the rule.
    if hard and rule.for_headers and re.search(r"\b(trunc_div|trunc_mod|plus|minus|mult|rdiv)\b", text):
        hard = []
    soft = [why for pat, why in _SOFT if re.search(pat, text)]
    return (not hard), hard + soft


# --------------------------------------------------------------------------- #
# targets
# --------------------------------------------------------------------------- #


def build_targets(match_pd: str, build_gcc_dir: str, info_paths: Sequence[str],
                  include_match_predicates: bool = False) -> Tuple[List[dict], dict]:
    text = open(match_pd, errors="replace").read()
    rules = extract_rules(text)
    idx = rule_index(rules)
    sites = load_sites(build_gcc_dir)
    traceable = {idx[s.pd_line].start for s in sites if s.pd_line in idx and
                 (s.simplify or include_match_predicates)}
    applied = applied_rules([s for s in sites if s.simplify or include_match_predicates], idx, info_paths)
    by_start = {r.start: r for r in rules}

    targets = []
    for start in sorted(traceable - applied):
        r = by_start[start]
        ok, reasons = reachability(r)
        targets.append({
            "id": f"matchpd:{r.start}",
            "kind": "matchpd",
            "match_pd_lines": [r.start, r.end],
            "rule_kind": r.kind,
            "section": r.section,
            "reachable": ok,
            "notes": reasons,
            "operators": r.operators,
            "for_headers": r.for_headers,
            "comment": r.comment,
            "rule": r.text,
            "related_test_dirs": ["opt", "tree-ssa", "torture"],
        })
    stats = {
        "rules_parsed": len(rules),
        "rules_traceable": len(traceable),
        "rules_applied": len(applied & traceable),
        "never_applied": len(traceable - applied),
        "never_applied_reachable": sum(t["reachable"] for t in targets),
        "sites": len(sites),
    }
    return targets, stats


def main(argv: Optional[Sequence[str]] = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Build match.pd rule targets for Phase 1.")
    ap.add_argument("--match-pd", default="/home/ec2-user/gcc-cov-src/gcc/match.pd")
    ap.add_argument("--build-dir", default="/home/ec2-user/gcc-cov-build/gcc",
                    help="instrumented GCC build dir holding gimple-/generic-match-*.cc")
    ap.add_argument("--info", nargs="+", required=True,
                    help="lcov .info of the corpus (or corpora) whose coverage defines 'already applied'")
    ap.add_argument("--out", required=True, help="targets .jsonl")
    ap.add_argument("--compare", nargs="*", default=[],
                    help="label=info.lcov pairs: report rules applied by each corpus separately")
    a = ap.parse_args(argv)

    targets, stats = build_targets(a.match_pd, a.build_dir, a.info)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        for t in targets:
            f.write(json.dumps(t) + "\n")
    print(json.dumps(stats, indent=1))
    if a.compare:
        text = open(a.match_pd, errors="replace").read()
        rules = extract_rules(text)
        idx = rule_index(rules)
        sites = [s for s in load_sites(a.build_dir) if s.simplify]
        trace = {idx[s.pd_line].start for s in sites if s.pd_line in idx}
        for pair in a.compare:
            label, path = pair.split("=", 1)
            n = len(applied_rules(sites, idx, [path]) & trace)
            print(f"  {label:16s} transform rules applied: {n:4d} / {len(trace)} ({100 * n / len(trace):.1f}%)")


if __name__ == "__main__":
    main()
