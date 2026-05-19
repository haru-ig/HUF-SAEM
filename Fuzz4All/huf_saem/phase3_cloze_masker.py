"""Masks critical tokens in a valid seed program to produce a cloze-fill prompt."""

from __future__ import annotations

import random
import re
from typing import List, Tuple

MASK_TOKEN = "___MASK___"

# Language-specific maskable token patterns
_OPERATOR_PATTERN = re.compile(
    r"(?<![=!<>])(?:==|!=|<=|>=|&&|\|\||<<|>>|\+\+|--|[+\-*/%&|^~]|<(?!<)|>(?!>))(?![=])"
)
_TYPE_KEYWORDS = {
    "cpp": r"\b(int|float|double|long|short|unsigned|signed|char|bool|void|auto|size_t)\b",
    "c":   r"\b(int|float|double|long|short|unsigned|signed|char|void|size_t)\b",
    "java": r"\b(int|float|double|long|short|byte|char|boolean|void|String|Object)\b",
    "go":  r"\b(int|int8|int16|int32|int64|uint|uint8|uint16|uint32|uint64|float32|float64|bool|string|byte|rune)\b",
    "smt2": r"\b(Int|Real|Bool|BitVec|Array)\b",
    "qiskit": r"\b(int|float|complex|bool|str|list|dict|tuple)\b",
}
_NUMERIC_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\b")
_SMT_SORT_PATTERN = re.compile(r"\b(?:Int|Real|Bool|BitVec|Array)\b")


class ClozeMasker:
    def __init__(self, language: str, mask_ratio: float = 0.15) -> None:
        self.language = language
        self.mask_ratio = mask_ratio
        self._type_re = re.compile(
            _TYPE_KEYWORDS.get(language, _TYPE_KEYWORDS["c"])
        )

    def _find_candidates(self, code: str) -> List[Tuple[int, int, str]]:
        """Return list of (start, end, token) for maskable positions."""
        candidates: List[Tuple[int, int, str]] = []
        if self.language in ("smt2",):
            for m in _NUMERIC_PATTERN.finditer(code):
                candidates.append((m.start(), m.end(), m.group()))
            for m in _SMT_SORT_PATTERN.finditer(code):
                candidates.append((m.start(), m.end(), m.group()))
        elif self.language in ("qiskit",):
            # Use ast for Python/Qiskit token extraction where possible
            candidates.extend(self._ast_candidates(code))
        else:
            for m in _OPERATOR_PATTERN.finditer(code):
                candidates.append((m.start(), m.end(), m.group()))
            for m in self._type_re.finditer(code):
                candidates.append((m.start(), m.end(), m.group()))
        # Deduplicate by start position
        seen: set = set()
        unique = []
        for c in sorted(candidates, key=lambda x: x[0]):
            if c[0] not in seen:
                seen.add(c[0])
                unique.append(c)
        return unique

    def _ast_candidates(self, code: str) -> List[Tuple[int, int, str]]:
        import ast as _ast

        candidates = []
        try:
            tree = _ast.parse(code)
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Constant) and isinstance(node.value, (int, float)):
                    # Approximate token span via line/col — not exact but good enough
                    candidates.append((-1, -1, str(node.value)))
        except Exception:
            pass
        return candidates

    def mask(self, code: str) -> Tuple[str, List[str]]:
        candidates = self._find_candidates(code)
        if not candidates:
            return code, []

        k = max(1, int(len(candidates) * self.mask_ratio))
        chosen = random.sample(candidates, min(k, len(candidates)))
        chosen_valid = [(s, e, t) for s, e, t in chosen if s >= 0 and e >= 0]
        chosen_valid.sort(key=lambda x: x[0], reverse=True)  # process right-to-left

        original_tokens = []
        result = code
        for start, end, token in chosen_valid:
            result = result[:start] + MASK_TOKEN + result[end:]
            original_tokens.append(token)

        return result, list(reversed(original_tokens))

    def build_fill_prompt(self, masked_code: str, language: str) -> str:
        return (
            f"The following {language} program has some tokens replaced with "
            f'"{MASK_TOKEN}". Fill in each blank with a syntactically and semantically '
            "valid replacement that preserves program structure. "
            "Return ONLY the completed program with no explanation.\n\n"
            f"```{language}\n{masked_code}\n```"
        )
