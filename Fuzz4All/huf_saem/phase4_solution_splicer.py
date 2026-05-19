"""Merges a constraint-solver snippet into a complete compilable test file."""

from __future__ import annotations

import re


# Header comment added to solver-generated files so they can be identified later.
_SOLVER_HEADER = "/* [HUF-SAEM Phase4: constraint-solver generated] */\n"


class SolutionSplicer:
    def __init__(self, language: str) -> None:
        self.language = language

    def splice(self, base_code: str, solution_snippet: str) -> str:
        snippet = solution_snippet.strip()
        if not snippet:
            return base_code

        if self.language in ("cpp", "c"):
            return self._splice_c_family(base_code, snippet)
        elif self.language == "java":
            return self._splice_java(base_code, snippet)
        elif self.language == "go":
            return self._splice_go(base_code, snippet)
        else:
            # SMT2, Qiskit: append
            return _SOLVER_HEADER + base_code + "\n" + snippet

    def _splice_c_family(self, base_code: str, snippet: str) -> str:
        # If snippet looks like a complete program (has main), use it directly.
        if re.search(r"\bmain\s*\(", snippet):
            return _SOLVER_HEADER + snippet
        # Otherwise wrap snippet in a valid program shell
        return _SOLVER_HEADER + f"int main(void) {{\n{snippet}\nreturn 0;\n}}\n"

    def _splice_java(self, base_code: str, snippet: str) -> str:
        if "class " in snippet and "main" in snippet:
            return _SOLVER_HEADER + snippet
        class_name = "SolverGenerated"
        return (
            _SOLVER_HEADER
            + f"public class {class_name} {{\n"
            + f"  public static void main(String[] args) {{\n"
            + f"    {snippet}\n"
            + "  }\n}\n"
        )

    def _splice_go(self, base_code: str, snippet: str) -> str:
        if snippet.startswith("package "):
            return _SOLVER_HEADER + snippet
        return (
            _SOLVER_HEADER
            + "package main\n\nfunc main() {\n"
            + snippet
            + "\n}\n"
        )
