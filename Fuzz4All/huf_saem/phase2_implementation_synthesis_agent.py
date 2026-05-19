"""GPT-4o agent that converts fault patterns into executable Python mutator functions."""

from __future__ import annotations

from typing import Callable, Optional

from Fuzz4All.util.api_request import create_config, request_engine
from Fuzz4All.util.util import simple_parse

_SYSTEM = (
    "You are a Python expert writing deterministic AST/text mutation functions "
    "for compiler fuzzing. Write safe, crash-resistant code."
)

_PROMPT_TEMPLATE = """\
Write a Python function with EXACTLY this signature:

    def mutate(code: str) -> str:

The function should take valid {language} source code and apply this transformation:

    {pattern}

Rules:
- Use stdlib only (re, ast, random — no third-party imports).
- The function MUST return a string in all code paths.
- It must not raise exceptions on arbitrary input; use try/except if needed.
- It must be deterministic given the same random seed.
- For C/C++/Go/Java/SMT: use regex or simple string manipulation.
- For Python/Qiskit: you may use the `ast` module.

Emit ONLY the Python function, inside a ```python ... ``` code block. No explanation.
"""


class ImplementationSynthesisAgent:
    def __init__(self, model: str = "gpt-4o", max_tokens: int = 2048) -> None:
        self.model = model
        self.max_tokens = max_tokens

    def synthesize_mutator(self, pattern: str, language: str) -> Optional[str]:
        messages = [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": _PROMPT_TEMPLATE.format(
                    language=language, pattern=pattern
                ),
            },
        ]
        config = create_config(
            prev={},
            messages=messages,
            max_tokens=self.max_tokens,
            temperature=0.1,
            model=self.model,
        )
        try:
            resp = request_engine(config)
            text = resp.choices[0].message.content or ""
        except Exception:
            return None
        code = simple_parse(text)
        if not code:
            # Fallback: take everything between first 'def mutate' and end
            idx = text.find("def mutate")
            if idx != -1:
                code = text[idx:]
        return code if code and "def mutate" in code else None

    def validate_and_exec(self, mutator_source: str) -> Optional[Callable]:
        namespace: dict = {}
        try:
            exec(compile(mutator_source, "<mutator>", "exec"), namespace)  # noqa: S102
        except Exception:
            return None
        fn = namespace.get("mutate")
        if not callable(fn):
            return None
        # Smoke-test: must return a non-empty string on trivial input
        try:
            result = fn("int main() { return 0; }")
            if not isinstance(result, str):
                return None
        except Exception:
            return None
        return fn
