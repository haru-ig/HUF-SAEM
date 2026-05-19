"""Injects source-aware constraint specifications into an autoprompt."""

from __future__ import annotations

_COMMENT_WRAPPERS: dict = {
    "cpp":    ("/* ", " */"),
    "c":      ("/* ", " */"),
    "java":   ("/* ", " */"),
    "go":     ("/* ", " */"),
    "smt2":   ("; ", ""),
    "qiskit": ("# ", ""),
    "python": ("# ", ""),
}


class PromptInjector:
    def inject(self, base_prompt: str, constraint_spec: str, language: str) -> str:
        open_tok, close_tok = _COMMENT_WRAPPERS.get(language, ("/* ", " */"))
        if close_tok:
            wrapped = f"{open_tok}{constraint_spec}{close_tok}"
        else:
            # Line-comment style: prefix each line
            lines = constraint_spec.splitlines()
            wrapped = "\n".join(f"{open_tok}{line}" for line in lines)
        return wrapped + "\n" + base_prompt
