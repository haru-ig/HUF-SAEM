"""GPT-4o agent that generates targeted test inputs to unlock blocked branches."""

from __future__ import annotations

import os
from typing import Optional

from Fuzz4All.util.api_request import create_config, request_engine
from Fuzz4All.util.util import simple_parse

_SYSTEM = (
    "You are an expert test generator and symbolic reasoner. "
    "Your goal is to produce code that exercises specific compiler code paths."
)

_SOLVE_TEMPLATE = """\
The following {language} program branch is never reached during fuzzing:

Branch location: {branch_id}

Surrounding source context:
```
{context}
```

Generate a {language} program that, when compiled, will exercise this specific branch.
First briefly explain the data-flow constraints that must be satisfied (1-2 sentences),
then emit ONLY the complete program inside a ```{language} ... ``` code block.
"""


class ConstraintSolvingAgent:
    def __init__(self, model: str = "gpt-4o", max_tokens: int = 2048) -> None:
        self.model = model
        self.max_tokens = max_tokens

    def extract_blocked_context(
        self, source_file: str, branch_id: str, radius: int = 10
    ) -> str:
        """Return ±radius lines around the branch line from the .fuzz source."""
        parts = branch_id.split(":")
        try:
            branch_line = int(parts[1]) if len(parts) >= 2 else 1
        except ValueError:
            branch_line = 1

        if not os.path.exists(source_file):
            return ""
        with open(source_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        start = max(0, branch_line - radius - 1)
        end = min(len(lines), branch_line + radius)
        return "".join(lines[start:end])

    def solve(
        self,
        source_file: str,
        branch_id: str,
        blocked_context: str,
        language: str,
    ) -> Optional[str]:
        messages = [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": _SOLVE_TEMPLATE.format(
                    language=language,
                    branch_id=branch_id,
                    context=blocked_context[:1500],
                ),
            },
        ]
        config = create_config(
            prev={},
            messages=messages,
            max_tokens=self.max_tokens,
            temperature=0.4,
            model=self.model,
        )
        try:
            resp = request_engine(config)
            text = resp.choices[0].message.content or ""
        except Exception:
            return None

        code = simple_parse(text)
        return code if code else None
