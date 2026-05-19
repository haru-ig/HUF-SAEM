"""GPT-4o agent that distills compiler source snippets into constraint specifications."""

from __future__ import annotations

from typing import Dict, List

from Fuzz4All.util.api_request import create_config, request_engine

_SYSTEM = (
    "You are a compiler internals expert. Your task is to identify the invariants, "
    "preconditions, and edge cases that test inputs must satisfy to exercise specific "
    "compiler code paths."
)

_DISTILL_TEMPLATE = """\
Given this {language} compiler source function:

```
{snippet}
```

Identify the key invariants, preconditions, and boundary conditions that a test input
must exhibit in order to reach and exercise this code. Be concise — 2-4 bullet points max.
Start each point with '- '.
"""


class DistillationAgent:
    def __init__(self, model: str = "gpt-4o", max_tokens: int = 512) -> None:
        self.model = model
        self.max_tokens = max_tokens

    def distill_snippet(self, snippet: Dict, language: str = "cpp") -> str:
        messages = [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": _DISTILL_TEMPLATE.format(
                    language=language,
                    snippet=snippet["snippet"][:1500],
                ),
            },
        ]
        config = create_config(
            prev={},
            messages=messages,
            max_tokens=self.max_tokens,
            temperature=0.2,
            model=self.model,
        )
        try:
            resp = request_engine(config)
            return resp.choices[0].message.content or ""
        except Exception:
            return ""

    def batch_distill(self, snippets: List[Dict], language: str = "cpp") -> List[str]:
        constraints: List[str] = []
        seen: set = set()
        for snippet in snippets:
            text = self.distill_snippet(snippet, language)
            if text and text not in seen:
                seen.add(text)
                constraints.append(text)
        return constraints

    def build_constraint_spec(self, constraints: List[str], language: str) -> str:
        if not constraints:
            return ""
        lines = "\n".join(f"  {c.strip()}" for c in constraints)
        return (
            f"Source-derived constraints for {language} test generation:\n"
            f"The generated code should satisfy ALL of the following:\n"
            f"{lines}"
        )
