"""Static analysis of compiler source code using tree-sitter."""

from __future__ import annotations

import os
from typing import Dict, List


_OPTIMIZER_ENTRY_PATTERNS = frozenset(
    ["run", "visit", "transform", "optimize", "rewrite", "lower", "emit", "codegen"]
)

_EXTENSIONS: dict = {
    "cpp": (".cpp", ".cxx", ".cc", ".hpp", ".h"),
    "c":   (".c", ".h"),
    "python": (".py",),
}


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
        try:
            from tree_sitter_languages import get_language, get_parser
            lang = get_language(self.language)
            self._parser = get_parser(self.language)
            self._ts_language = lang
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

    def _extract_nodes(self, filepath: str) -> List[Dict]:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()

        if self._parser is not None:
            return self._tree_sitter_extract(filepath, source)
        return self._regex_extract(filepath, source)

    def _tree_sitter_extract(self, filepath: str, source: str) -> List[Dict]:
        import re
        records = []
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
                            "snippet": snippet[:2000],  # cap at 2 KB
                        }
                    )
            for child in node.children:
                walk(child, depth + 1)

        walk(root)
        return records

    def _regex_extract(self, filepath: str, source: str) -> List[Dict]:
        import re

        records = []
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
                        "snippet": snippet,
                    }
                )
        return records

    def top_k_snippets(self, k: int = 10) -> List[Dict]:
        if not self._records:
            self.scan_files()
        return sorted(self._records, key=lambda r: r["complexity"], reverse=True)[:k]


def _count_nesting_depth(snippet: str, language: str) -> int:
    """Estimate maximum conditional nesting depth via brace/keyword counting."""
    import re
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
    for child in node.children:
        if child.type in ("function_declarator", "identifier"):
            # Recurse one level for declarator
            for grandchild in child.children:
                if grandchild.type == "identifier":
                    return source_bytes[grandchild.start_byte: grandchild.end_byte].decode(
                        "utf-8", errors="replace"
                    )
            if child.type == "identifier":
                return source_bytes[child.start_byte: child.end_byte].decode(
                    "utf-8", errors="replace"
                )
    return ""
