"""Loads, persists, and applies synthesized mutator functions."""

from __future__ import annotations

import os
import random
from typing import Callable, List

from Fuzz4All.huf_saem.phase2_implementation_synthesis_agent import (
    ImplementationSynthesisAgent,
)
from Fuzz4All.huf_saem.phase2_mutator_invention_agent import MutatorInventionAgent


class MutatorRegistry:
    def __init__(self, mutator_dir: str) -> None:
        self.mutator_dir = mutator_dir
        self._mutators: List[Callable] = []

    def load_or_synthesize(
        self,
        bug_reports: list,
        language: str,
        invention_agent: MutatorInventionAgent,
        synthesis_agent: ImplementationSynthesisAgent,
    ) -> None:
        existing = [
            f for f in os.listdir(self.mutator_dir) if f.endswith(".py")
        ]
        if existing:
            self.load_mutators()
            return

        if not bug_reports:
            return

        patterns = invention_agent.identify_patterns(bug_reports, language)
        for i, pattern in enumerate(patterns):
            source = synthesis_agent.synthesize_mutator(pattern, language)
            if source is None:
                continue
            fn = synthesis_agent.validate_and_exec(source)
            if fn is None:
                continue
            name = f"mutator_{i:03d}"
            self.save_mutator(name, source)
            self._mutators.append(fn)

    def save_mutator(self, name: str, source: str) -> None:
        path = os.path.join(self.mutator_dir, f"{name}.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)

    def load_mutators(self) -> List[Callable]:
        synth = ImplementationSynthesisAgent.__new__(ImplementationSynthesisAgent)
        self._mutators = []
        for fname in sorted(os.listdir(self.mutator_dir)):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(self.mutator_dir, fname)
            with open(path, "r", encoding="utf-8") as f:
                source = f.read()
            fn = synth.validate_and_exec(source)
            if fn is not None:
                self._mutators.append(fn)
        return self._mutators

    def apply_random(self, code: str) -> str:
        if not self._mutators:
            return code
        fn = random.choice(self._mutators)
        try:
            result = fn(code)
            return result if isinstance(result, str) and result else code
        except Exception:
            return code
