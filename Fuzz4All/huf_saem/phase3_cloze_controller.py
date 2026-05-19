"""Monitors rolling success rate and drives cloze-fill generation."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Optional

from Fuzz4All.util.api_request import create_config, request_engine
from Fuzz4All.util.util import simple_parse

if TYPE_CHECKING:
    from Fuzz4All.huf_saem.phase3_cloze_masker import ClozeMasker
    from Fuzz4All.huf_saem.phase3_seed_db import SeedDatabase
    from Fuzz4All.target.target import FResult


class ClozeController:
    def __init__(
        self,
        threshold: float,
        window_size: int,
        seed_db: "SeedDatabase",
        masker: "ClozeMasker",
        model: str = "gpt-4o",
    ) -> None:
        self.threshold = threshold
        self.seed_db = seed_db
        self.masker = masker
        self.model = model
        self._results: deque = deque(maxlen=window_size)
        self.cloze_active: bool = False

    def record_result(self, f_result: "FResult") -> None:
        from Fuzz4All.target.target import FResult
        self._results.append(f_result == FResult.SAFE)
        if len(self._results) == self._results.maxlen:
            rate = self.success_rate()
            self.cloze_active = rate < self.threshold
            if self.cloze_active:
                print(
                    f"[Phase3] cloze mode active (success rate {rate:.1%} < {self.threshold:.1%})"
                )

    def success_rate(self) -> float:
        if not self._results:
            return 1.0
        return sum(self._results) / len(self._results)

    def generate_cloze_input(self, target_language: str) -> Optional[str]:
        if not self.cloze_active:
            return None
        seed = self.seed_db.retrieve_random()
        if not seed:
            return None

        masked_code, _ = self.masker.mask(seed)
        fill_prompt = self.masker.build_fill_prompt(masked_code, target_language)

        messages = [
            {
                "role": "system",
                "content": (
                    f"You are a {target_language} expert. Fill in masked tokens to produce "
                    "valid, compilable code."
                ),
            },
            {"role": "user", "content": fill_prompt},
        ]
        config = create_config(
            prev={},
            messages=messages,
            max_tokens=1024,
            temperature=0.3,
            model=self.model,
        )
        try:
            resp = request_engine(config)
            text = resp.choices[0].message.content or ""
        except Exception:
            return None

        code = simple_parse(text)
        return code if code else text.strip() or None
