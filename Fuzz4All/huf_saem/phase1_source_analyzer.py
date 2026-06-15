"""Static analysis of compiler source code using tree-sitter."""

from __future__ import annotations

import os
import re
from typing import Dict, List


_OPTIMIZER_ENTRY_PATTERNS = frozenset(
    ["run", "visit", "transform", "optimize", "rewrite", "lower", "emit", "codegen"]
)

_EXTENSIONS: dict = {
    "cpp": (".cpp", ".cxx", ".cc", ".hpp", ".h"),
    "c":   (".c", ".h"),
    "python": (".py",),
}

# Per-language tree-sitter grammar packages (these ship Python 3.13 wheels,
# unlike the tree-sitter-languages meta-package).
_LANGUAGE_MODULES: dict = {
    "cpp": "tree_sitter_cpp",
    "c": "tree_sitter_c",
    "python": "tree_sitter_python",
}

_DECLARATOR_WRAPPERS = ("pointer_declarator", "reference_declarator", "array_declarator")

# Human-readable descriptions for the top-level subdirectories of LLVM's
# llvm/lib/Transforms (and similar layouts), used to give the distillation
# step context about *what kind* of optimization a snippet comes from
# without leaking compiler-internal class/API names.
_PASS_CATEGORY_NAMES: dict = {
    "InstCombine": "instruction combining / peephole simplification",
    "AggressiveInstCombine": "aggressive instruction combining",
    "Scalar": "scalar loop and value optimizations",
    "Vectorize": "loop and SLP vectorization",
    "IPO": "interprocedural optimization (inlining, attribute inference)",
    "Utils": "IR transformation utilities",
    "Coroutines": "coroutine lowering",
    "ObjCARC": "Objective-C ARC optimization",
    "CFGuard": "control-flow integrity instrumentation",
    "Instrumentation": "sanitizer/coverage instrumentation",
    "HipStdPar": "HIP standard-parallelism lowering",
}

def _extract_relevant_lines(snippet: str, max_chars: int = 800) -> str:
    """Trim a function snippet to a short, contiguous excerpt starting at its
    signature.

    A contiguous excerpt keeps multi-line statements (asserts, conditions)
    intact. Cherry-picking individual "precondition-looking" lines instead
    produces a collage of unrelated, truncated fragments that the
    distillation step cannot translate.
    """
    return snippet[:max_chars]


class SourceAnalyzer:
    def __init__(
        self,
        source_dir: str,
        language: str = "cpp",
        complexity_threshold: int = 4,
    ) -> None:
        self.source_dir = source_dir
        self.language = language
        self.complexity_threshold = complexity_threshold
        self._parser = None
        self._records: List[Dict] = []
        self._load_parser()

    def _load_parser(self) -> None:
        module_name = _LANGUAGE_MODULES.get(self.language)
        if module_name is None:
            self._parser = None
            self._ts_language = None
            return
        try:
            import importlib
            from tree_sitter import Language, Parser
            grammar = importlib.import_module(module_name)
            self._ts_language = Language(grammar.language())
            self._parser = Parser(self._ts_language)
        except ImportError:
            # Fall back to regex-based analysis
            self._parser = None
            self._ts_language = None

    def scan_files(self) -> List[Dict]:
        self._records = []
        extensions = _EXTENSIONS.get(self.language, (".cpp",))
        for root, _, files in os.walk(self.source_dir):
            for fname in files:
                if any(fname.endswith(ext) for ext in extensions):
                    filepath = os.path.join(root, fname)
                    try:
                        self._records.extend(self._extract_nodes(filepath))
                    except Exception:
                        pass
        return self._records

    def _pass_category(self, filepath: str) -> str:
        """Human-readable description of the optimizer area a file belongs to."""
        rel = os.path.relpath(filepath, self.source_dir)
        top = rel.split(os.sep)[0]
        return _PASS_CATEGORY_NAMES.get(top, top)

    def _extract_nodes(self, filepath: str) -> List[Dict]:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()

        if self._parser is not None:
            return self._tree_sitter_extract(filepath, source)
        return self._regex_extract(filepath, source)

    def _tree_sitter_extract(self, filepath: str, source: str) -> List[Dict]:
        records = []
        pass_category = self._pass_category(filepath)
        source_bytes = source.encode("utf-8", errors="replace")
        tree = self._parser.parse(source_bytes)
        root = tree.root_node

        func_types = {
            "cpp": ["function_definition"],
            "c":   ["function_definition"],
            "python": ["function_definition"],
        }
        target_types = func_types.get(self.language, ["function_definition"])

        def walk(node, depth=0):
            if node.type in target_types:
                snippet = source_bytes[node.start_byte : node.end_byte].decode(
                    "utf-8", errors="replace"
                )
                complexity = _count_nesting_depth(snippet, self.language)
                func_name = _extract_function_name(node, source_bytes)
                if complexity >= self.complexity_threshold or (
                    func_name and any(p in func_name.lower() for p in _OPTIMIZER_ENTRY_PATTERNS)
                ):
                    records.append(
                        {
                            "file": filepath,
                            "function_name": func_name or "<unknown>",
                            "complexity": complexity,
                            "pass_category": pass_category,
                            "snippet": _extract_relevant_lines(snippet),
                        }
                    )
            for child in node.children:
                walk(child, depth + 1)

        walk(root)
        return records

    def _regex_extract(self, filepath: str, source: str) -> List[Dict]:
        records = []
        pass_category = self._pass_category(filepath)
        # Heuristic: look for function-like blocks with high nesting
        func_re = re.compile(
            r"(?:^|\n)(?:[\w:<>*&\s]+)\s+(\w+)\s*\([^)]*\)\s*\{", re.MULTILINE
        )
        for m in func_re.finditer(source):
            func_name = m.group(1)
            start = m.start()
            snippet = source[start : start + 1500]
            complexity = _count_nesting_depth(snippet, self.language)
            if complexity >= self.complexity_threshold or any(
                p in func_name.lower() for p in _OPTIMIZER_ENTRY_PATTERNS
            ):
                records.append(
                    {
                        "file": filepath,
                        "function_name": func_name,
                        "complexity": complexity,
                        "pass_category": pass_category,
                        "snippet": _extract_relevant_lines(snippet),
                    }
                )
        return records

    def top_k_snippets(self, k: int = 10) -> List[Dict]:
        if not self._records:
            self.scan_files()
        ranked = sorted(self._records, key=lambda r: r["complexity"], reverse=True)

        # Diversify: take the highest-complexity snippet from each distinct
        # pass_category first, then fill remaining slots in complexity order.
        seen_categories: set = set()
        diverse: List[Dict] = []
        rest: List[Dict] = []
        for record in ranked:
            if record["pass_category"] not in seen_categories:
                seen_categories.add(record["pass_category"])
                diverse.append(record)
            else:
                rest.append(record)

        return (diverse + rest)[:k]


def _count_nesting_depth(snippet: str, language: str) -> int:
    """Estimate maximum conditional nesting depth via brace/keyword counting."""
    if language in ("python", "qiskit"):
        keywords = re.findall(r"\b(?:if|for|while|with|try)\b", snippet)
        # rough indent-based depth
        lines = snippet.splitlines()
        max_depth = 0
        for line in lines:
            depth = (len(line) - len(line.lstrip())) // 4
            max_depth = max(max_depth, depth)
        return max_depth
    else:
        keywords = re.findall(r"\b(?:if|else|for|while|switch|try|catch)\b", snippet)
        return len(keywords)


def _extract_function_name(node, source_bytes: bytes) -> str:
    """Extract function name from a tree-sitter function_definition node."""
    declarator = None
    for child in node.children:
        if child.type == "identifier":
            return _decode_node(source_bytes, child)
        if child.type == "function_declarator" or child.type in _DECLARATOR_WRAPPERS:
            declarator = child
            break

    # Unwrap pointer/reference declarators, e.g. `Value *Foo::bar(...)`
    while declarator is not None and declarator.type in _DECLARATOR_WRAPPERS:
        declarator = next(
            (c for c in declarator.children
             if c.type == "function_declarator" or c.type in _DECLARATOR_WRAPPERS),
            None,
        )

    if declarator is None or declarator.type != "function_declarator" or not declarator.children:
        return ""

    name_node = declarator.children[0]
    if name_node.type == "qualified_identifier":
        # `Class::method` -> the rightmost segment is the (unqualified) method name
        parts = [c for c in name_node.children if c.type in ("identifier", "field_identifier")]
        return _decode_node(source_bytes, parts[-1]) if parts else ""
    if name_node.type in ("identifier", "field_identifier"):
        return _decode_node(source_bytes, name_node)
    return ""


def _decode_node(source_bytes: bytes, node) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
