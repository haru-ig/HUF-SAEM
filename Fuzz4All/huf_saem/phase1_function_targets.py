"""Phase 1 (redesign), target kinds 2 and 3: uncovered regions in GCC's
optimiser passes and in its C++ front end, found with tree-sitter.

Replaces the original Phase 1 selection (deepest nesting anywhere in the
repository, snippet[:800]) with coverage-driven targets:

  * a PARTIAL target is an `if` / `else` / `case` body that a corpus never
    executed although the function containing it ran -- the slice handed to
    the LLM is the chain of enclosing conditions from the function start down
    to the region, plus the region itself;
  * an ENTRY target is a function the corpus never entered -- the slice is its
    leading comment, its signature, its body (whole if it is short) and the
    conditions under which covered code in the same file calls it.

Function boundaries come from the lcov data (exact); tree-sitter parses one
function at a time, so the parse errors GCC's statement macros cause
(FOR_EACH_BB_FN (...) { ... }) stay local.

Validation is per-program coverage: compile with the instrumented GCC with
GCOV_PREFIX pointing at a scratch directory, copy the target file's .gcno
next to its fresh .gcda and read the line counts with gcov.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple

SRC = "/home/ec2-user/gcc-cov-src/gcc/"
BUILD = "/home/ec2-user/gcc-cov-build/gcc/"
GXX = "/home/ec2-user/gcc-cov/bin/g++"
GCOV = "/usr/bin/gcov"   # the build stamps GCC-11-format .gcda files

# --------------------------------------------------------------------------- #
# Which files / functions are candidate territory
# --------------------------------------------------------------------------- #

CXX_FE = re.compile(r"^(cp/|c-family/)")
OPTIMISER = re.compile(
    r"^(tree-ssa|tree-vect|tree-loop|tree-if-conv|tree-scalar-evolution|tree-data-ref|"
    r"tree-predcom|tree-chrec|tree-affine|tree-vrp|tree-sra|tree-switch|tree-call-cdce|"
    r"tree-object-size|tree-tailcall|tree-inline|tree-complex|tree-vector-generic|"
    r"tree-cfgcleanup|gimple-ssa|gimple-fold|gimple-range|gimple-loop|gimple-match-head|"
    r"gimple-harden|gimple-isel|gimple-lower-bitint|ipa|cgraph|value-range|range-op|vr-values|"
    r"fold-const|cfgloop|loop-|combine|cse|gcse|fwprop|ifcvt|dse|dce|ree|"
    r"simplify-rtx|expr\.cc|expmed|optabs|internal-fn|builtins|calls\.cc|cfgexpand|"
    r"lower-subreg|late-combine|ext-dce|crc|bb-reorder|shrink-wrap|regcprop|postreload|"
    r"compare-elim|tree-ssa-math-opts|tree-ssa-phiopt|tree-ssa-reassoc)"
)
SKIP_FILE = re.compile(r"(^|/)(reload1?\.cc|.*-match-\d+\.cc|insn-.*|omp-.*|tree-profile.*|"
                       r"asan|ubsan|tsan|hwasan|sanopt|graphite.*|lto-.*|dwarf.*|var-tracking.*)")
# Functions that never run in a release compiler at default options, or that
# only report problems (our programs must be valid C++).
SKIP_FN = re.compile(r"(^|[^a-z])(verify|debug|dump|print|pp_|error|warn|inform|pedwarn|diag|"
                     r"omp|oacc|openacc|transactional|_tm_|strub|sanitiz|profile|lto|"
                     r"simduid|simd_clone|simt|scan_store|ipa_pta|clobbers|odr|"
                     r"read_section|stream|read_summary|write_summary|cond_exec)", re.I)


def short_name(name: str) -> str:
    """Qualified function name without its parameter list."""
    n = name.replace("(anonymous namespace)::", "")
    return n.split("(", 1)[0]


def area_kind(rel: str) -> Optional[str]:
    if SKIP_FILE.search(rel):
        return None
    if CXX_FE.match(rel):
        return "cxx_frontend"
    if OPTIMISER.match(rel):
        return "optimiser"
    return None


TEST_DIRS = [
    (r"^tree-vect|^tree-loop|^tree-if-conv", ["vect", "tree-ssa", "opt"]),
    (r"^ipa|^cgraph", ["ipa", "opt", "torture"]),
    (r"^cp/constexpr", ["cpp1y", "cpp1z", "cpp2a", "cpp0x"]),
    (r"^cp/coroutines", ["coroutines"]),
    (r"^cp/(pt|class|decl|semantics)", ["template", "cpp0x", "cpp2a", "cpp1z"]),
    (r"^cp/lambda", ["cpp0x", "cpp1y", "cpp2a"]),
    (r"^cp/|^c-family/", ["cpp0x", "cpp1y", "cpp1z", "cpp2a", "template", "other"]),
    (r".", ["opt", "tree-ssa", "torture"]),
]


def related_dirs(rel: str) -> List[str]:
    return next(d for pat, d in TEST_DIRS if re.search(pat, rel))


# --------------------------------------------------------------------------- #
# lcov
# --------------------------------------------------------------------------- #


def parse_lcov(path: str) -> Dict[str, dict]:
    """{rel: {"lines": {ln: hits}, "fns": [(start, end, name, hits)]}} for GCC sources."""
    out: Dict[str, dict] = {}
    cur = fnspan = fnhits = None
    with open(path, errors="replace") as f:
        for line in f:
            if line.startswith("SF:"):
                p = line[3:].strip()
                rel = p[len(SRC):] if p.startswith(SRC) else (p[len(BUILD):] if p.startswith(BUILD) else None)
                cur = {"lines": {}, "fns": []} if rel else None
                if rel:
                    out[rel] = cur
                fnspan, fnhits = {}, {}
            elif cur is None:
                continue
            elif line.startswith("FN:"):
                a = line[3:].strip().split(",")
                if len(a) == 3:
                    fnspan[a[2]] = (int(a[0]), int(a[1]))
            elif line.startswith("FNDA:"):
                h, n = line[5:].strip().split(",", 1)
                fnhits[n] = int(h)
            elif line.startswith("DA:"):
                ln, h = line[3:].split(",")[:2]
                cur["lines"][int(ln)] = int(h)
            elif line.startswith("end_of_record"):
                cur["fns"] = [(s, e, n, fnhits.get(n, 0)) for n, (s, e) in fnspan.items()]
    return out


def demangle(names: Sequence[str]) -> Dict[str, str]:
    if not names:
        return {}
    r = subprocess.run(["c++filt"], input="\n".join(names), capture_output=True, text=True)
    return dict(zip(names, r.stdout.splitlines()))


# --------------------------------------------------------------------------- #
# tree-sitter slicing
# --------------------------------------------------------------------------- #

_PARSER = None


def _parser():
    global _PARSER
    if _PARSER is None:
        import tree_sitter_cpp
        from tree_sitter import Language, Parser
        _PARSER = Parser(Language(tree_sitter_cpp.language()))
    return _PARSER


CONTROL = {"if_statement", "switch_statement", "case_statement", "for_statement", "while_statement",
           "do_statement", "for_range_loop", "else_clause"}


def _header(node, src: bytes) -> str:
    """One-line description of a control node: its condition, not its body."""
    if node.type == "case_statement":
        text = src[node.start_byte:node.end_byte].decode(errors="replace")
        return text.split(":", 1)[0].strip() + ":"
    if node.type == "else_clause":
        return "else"
    cond = node.child_by_field_name("condition") or node.child_by_field_name("initializer")
    kw = node.type.split("_")[0]
    if cond is not None:
        c = re.sub(r"\s+", " ", src[cond.start_byte:cond.end_byte].decode(errors="replace"))
        return f"{kw} {c}"
    first = src[node.start_byte:node.end_byte].decode(errors="replace").split("{", 1)[0]
    return re.sub(r"\s+", " ", first).strip()


def _regions(fn_src: bytes):
    """Yield (region_node, chain_of_ancestor_control_nodes) for every branch body."""
    root = _parser().parse(fn_src).root_node
    stack = [(root, [])]
    while stack:
        node, chain = stack.pop()
        for ch in node.children:
            nchain = chain + [node] if node.type in CONTROL else chain
            if node.type == "if_statement" and ch == node.child_by_field_name("consequence"):
                yield ch, nchain
            elif node.type == "else_clause" and ch.type in ("compound_statement", "expression_statement",
                                                              "return_statement"):
                yield ch, nchain
            elif node.type == "case_statement" and ch.is_named and ch.type not in ("number_literal",
                                                                                     "identifier",
                                                                                     "qualified_identifier"):
                yield ch, nchain
            stack.append((ch, nchain))


def _leading_comment(lines: List[str], start: int) -> str:
    j = start - 2
    while j >= 0 and not lines[j].strip():
        j -= 1
    # GCC style puts the return type on its own line above the name: skip it
    # (and any blank lines above it) to reach the comment.
    if j >= 0 and not lines[j].rstrip().endswith("*/") and not lines[j].rstrip().endswith(";") \
            and not lines[j].rstrip().endswith("}"):
        j -= 1
        while j >= 0 and not lines[j].strip():
            j -= 1
    if j < 0 or not lines[j].rstrip().endswith("*/"):
        return ""
    k = j
    while k >= 0 and "/*" not in lines[k]:
        k -= 1
    return "\n".join(lines[max(k, 0):j + 1]).strip()


# --------------------------------------------------------------------------- #
# target extraction
# --------------------------------------------------------------------------- #

MAX_REGION_TEXT = 60      # lines of the uncovered region shown (the chain is always complete)
MAX_ENTRY_BODY = 120      # entry targets: whole body if at most this many lines


def extract_targets(cov: Dict[str, dict], min_uncovered: int = 3) -> List[dict]:
    targets: List[dict] = []
    names = [n for d in cov.values() for _, _, n, _ in d["fns"]]
    dm = demangle(names)
    for rel, d in cov.items():
        kind = area_kind(rel)
        if kind is None or not os.path.exists(SRC + rel):
            continue
        lines = open(SRC + rel, errors="replace").read().splitlines()
        da = d["lines"]
        for start, end, mangled, hits in d["fns"]:
            name = dm.get(mangled, mangled)
            short = short_name(name)
            if SKIP_FN.search(short) or end <= start:
                continue
            body_da = {ln: h for ln, h in da.items() if start <= ln <= end}
            if not body_da:
                continue
            base = {"kind": kind, "file": rel, "function": name, "function_lines": [start, end],
                    "entry_line": min(body_da),     # first executable line: "was it entered?"
                    "related_test_dirs": related_dirs(rel), "reachable": True,
                    "comment": _leading_comment(lines, start)}
            if hits == 0:
                unc = sum(1 for h in body_da.values() if h == 0)
                if unc < min_uncovered:
                    continue
                body = "\n".join(lines[start - 1:end]) if end - start <= MAX_ENTRY_BODY else \
                    "\n".join(lines[start - 1:start + 15]) + "\n  ... (body continues)"
                targets.append(dict(base, id=f"fn:{rel}:{start}", target_type="entry",
                                    check_lines=sorted(body_da)[:1], uncovered_lines=unc,
                                    slice=body))
                continue
            # partial: uncovered branch bodies inside a function that ran
            fn_src = "\n".join(lines[start - 1:end]).encode()
            for region, chain in _regions(fn_src):
                r0, r1 = start + region.start_point[0], start + region.end_point[0]
                rda = {ln: h for ln, h in body_da.items() if r0 <= ln <= r1}
                unc = [ln for ln, h in rda.items() if h == 0]
                if len(unc) < min_uncovered or any(h > 0 for h in rda.values()):
                    continue   # want regions that never ran at all
                # the guard must have run, otherwise this is not the frontier
                guards = [start + n.start_point[0] for n in chain]
                if chain and da.get(guards[-1], 0) == 0:
                    continue
                cond = "\n".join(f"  line {start + n.start_point[0]}: {_header(n, fn_src)}" for n in chain)
                region_text = lines[r0 - 1:r1]
                if len(region_text) > MAX_REGION_TEXT:
                    region_text = region_text[:MAX_REGION_TEXT] + ["  ... (region continues)"]
                targets.append(dict(base, id=f"fn:{rel}:{r0}", target_type="partial",
                                    check_lines=sorted(unc)[:3], uncovered_lines=len(unc),
                                    guard_chain=cond, slice="\n".join(region_text)))
    return targets


def select(targets: List[dict], per_kind: int, per_file: int = 6, per_function: int = 1) -> List[dict]:
    """Largest gaps first, half entry / half partial per kind, spread over
    files and functions (entry targets would otherwise crowd out partial
    ones simply by being larger)."""
    out = []
    for kind in ("optimiser", "cxx_frontend"):
        fcount: Dict[str, int] = {}
        gcount: Dict[str, int] = {}
        for typ, quota in (("partial", per_kind - per_kind // 2), ("entry", per_kind // 2)):
            taken = 0
            for t in sorted((t for t in targets if t["kind"] == kind and t["target_type"] == typ),
                            key=lambda t: -t["uncovered_lines"]):
                if fcount.get(t["file"], 0) >= per_file or gcount.get(t["function"], 0) >= per_function:
                    continue
                fcount[t["file"]] = fcount.get(t["file"], 0) + 1
                gcount[t["function"]] = gcount.get(t["function"], 0) + 1
                out.append(t)
                taken += 1
                if taken >= quota:
                    break
    return out


# --------------------------------------------------------------------------- #
# validation: did a program execute the target lines?
# --------------------------------------------------------------------------- #

_GCOV_LINE = re.compile(r"^\s*([0-9]+|#####|=====|-)\*?:\s*([0-9]+):")


def line_counts(source: str, rel: str, lines: Sequence[int], timeout: int = 60) -> Tuple[Optional[Dict[int, int]], str]:
    """Compile `source` at -O2 with the instrumented GCC; execution count of
    each requested line of gcc/<rel> (0 = not executed). None + error text if
    the program does not compile."""
    d = tempfile.mkdtemp(prefix="fncheck_")
    try:
        with open(os.path.join(d, "t.cc"), "w") as f:
            f.write(source)
        env = dict(os.environ, GCOV_PREFIX=os.path.join(d, "g"), GCOV_PREFIX_STRIP="3")
        try:
            p = subprocess.run([GXX, "-std=c++23", "-O2", "-w", "-c", "t.cc", "-o", "t.o"],
                               cwd=d, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, "compile timed out"
        if p.returncode != 0:
            return None, "\n".join(l for l in p.stderr.splitlines() if "error" in l)[:600] or p.stderr[:600]
        want = set(lines)
        counts = {ln: 0 for ln in want}
        stem = os.path.splitext(rel)[0]
        gcda = os.path.join(d, "g", "gcc", stem + ".gcda")
        if not os.path.exists(gcda):
            return counts, ""
        objdir = os.path.dirname(gcda)
        shutil.copy(os.path.join(BUILD, stem + ".gcno"), objdir)
        r = subprocess.run([GCOV, "-t", "-o", objdir, os.path.basename(gcda)], cwd=objdir,
                           capture_output=True, text=True, timeout=120)
        for ln in r.stdout.splitlines():
            m = _GCOV_LINE.match(ln)
            if m and int(m.group(2)) in want and m.group(1).isdigit():
                counts[int(m.group(2))] = int(m.group(1))
        return counts, ""
    finally:
        shutil.rmtree(d, ignore_errors=True)


def guard_lines(target: dict) -> List[int]:
    return [int(m) for m in re.findall(r"line (\d+):", target.get("guard_chain", ""))]


def check_target(source: str, target: dict) -> Tuple[bool, str, str]:
    """(hit, compile_error, progress) -- progress says how far GCC got, for
    feeding back to the LLM when the target was missed."""
    start = target["function_lines"][0]
    fn_lines = [target["entry_line"]] if "entry_line" in target else list(range(start, start + 8))
    guards = guard_lines(target)
    counts, err = line_counts(source, target["file"], list(target["check_lines"]) + fn_lines + guards)
    if counts is None:
        return False, err, ""
    if any(counts.get(ln, 0) > 0 for ln in target["check_lines"]):
        return True, "", ""
    entered = max((counts.get(ln, 0) for ln in fn_lines), default=0)
    if not entered:
        return False, "", "GCC never entered this function at all."
    reached = [(ln, counts.get(ln, 0)) for ln in guards]
    text = "; ".join(f"line {ln} ran {c} times" for ln, c in reached)
    deepest = next((ln for ln, c in reversed(reached) if c > 0), None)
    return False, "", (f"GCC entered the function, but the target region never ran. Path conditions: {text}."
                       + (f" The deepest condition reached is at line {deepest}; it never let execution "
                          f"into the region." if deepest else ""))


def lines_executed(source: str, rel: str, check_lines: Sequence[int], timeout: int = 60) -> Tuple[bool, str]:
    counts, err = line_counts(source, rel, check_lines, timeout)
    if counts is None:
        return False, err
    return any(c > 0 for c in counts.values()), ""


def main(argv: Optional[Sequence[str]] = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="tree-sitter coverage-gap targets (optimiser + C++ front end)")
    ap.add_argument("--info", required=True, help="lcov .info of the corpus whose gaps define targets")
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-kind", type=int, default=150)
    ap.add_argument("--per-file", type=int, default=6)
    a = ap.parse_args(argv)
    cov = parse_lcov(a.info)
    allt = extract_targets(cov)
    sel = select(allt, a.per_kind, a.per_file)
    with open(a.out, "w") as f:
        for t in sel:
            f.write(json.dumps(t) + "\n")
    from collections import Counter
    print(json.dumps({
        "candidates": len(allt),
        "candidates_by": {f"{k}/{t}": n for (k, t), n in Counter((x["kind"], x["target_type"]) for x in allt).items()},
        "selected": len(sel),
        "selected_by": {f"{k}/{t}": n for (k, t), n in Counter((x["kind"], x["target_type"]) for x in sel).items()},
        "files": len({x["file"] for x in sel}),
    }, indent=1))


if __name__ == "__main__":
    main()
