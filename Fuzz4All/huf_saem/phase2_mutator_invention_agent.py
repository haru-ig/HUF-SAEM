"""GPT-4o agent that identifies syntactic fault patterns from bug reports."""

from __future__ import annotations

from typing import Dict, List

from Fuzz4All.util.api_request import create_config, request_engine

_BATCH_SIZE = 5

_SYSTEM = (
    "You are a compiler bug analysis expert. Your job is to identify "
    "syntactic and semantic code patterns that historically trigger compiler bugs."
)


class MutatorInventionAgent:
    def __init__(self, model: str = "gpt-4o", max_tokens: int = 1024) -> None:
        self.model = model
        self.max_tokens = max_tokens

    def identify_patterns(self, bug_reports: List[Dict], language: str) -> List[str]:
        patterns: List[str] = []
        for i in range(0, len(bug_reports), _BATCH_SIZE):
            batch = bug_reports[i : i + _BATCH_SIZE]
            batch_patterns = self._process_batch(batch, language)
            patterns.extend(batch_patterns)
        return list(dict.fromkeys(patterns))  # preserve order, deduplicate

    def _process_batch(self, batch: List[Dict], language: str) -> List[str]:
        reports_text = "\n\n".join(
            f"Bug #{j + 1}:\nTitle: {r['title']}\nBody: {r['body'][:400]}"
            for j, r in enumerate(batch)
        )
        messages = [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": (
                    f"The following bug reports were found in a {language} compiler.\n\n"
                    f"{reports_text}\n\n"
                    "List the concrete syntactic constructs (specific keywords, construct "
                    "combinations, type patterns, expression forms) that most likely triggered "
                    "each bug. Format: one pattern per line, starting with '- '. Be concise."
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
            text = resp.choices[0].message.content or ""
        except Exception:
            return []
        return _parse_bullet_list(text)


def _parse_bullet_list(text: str) -> List[str]:
    patterns = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            patterns.append(line[2:].strip())
        elif line.startswith("* "):
            patterns.append(line[2:].strip())
    return [p for p in patterns if p]
