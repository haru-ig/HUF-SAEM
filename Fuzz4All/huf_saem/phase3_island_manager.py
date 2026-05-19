"""Multi-island evolutionary algorithm for generation diversity."""

from __future__ import annotations

import random
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from Fuzz4All.huf_saem.phase3_seed_db import SeedDatabase

_MAX_POPULATION = 50

_BIAS_FRAGMENTS: dict = {
    "memory_allocation": (
        "emphasizing memory allocation patterns such as malloc/free, "
        "new/delete, RAII, unique_ptr, or custom allocators"
    ),
    "concurrency": (
        "emphasizing thread-concurrency features such as mutex, "
        "std::thread, atomic operations, or coroutines"
    ),
    "arithmetic": (
        "emphasizing arithmetic edge cases such as integer overflow, "
        "floating-point precision, bitwise operations, or mixed-type expressions"
    ),
    "template_meta": (
        "emphasizing template metaprogramming, concepts, constexpr, "
        "or type traits"
    ),
    "error_handling": (
        "emphasizing error-handling patterns such as exceptions, "
        "setjmp/longjmp, errno, or RAII guards"
    ),
}


class Island:
    def __init__(
        self,
        island_id: int,
        bias: str,
        seed_db: Optional["SeedDatabase"] = None,
    ) -> None:
        self.island_id = island_id
        self.bias = bias
        self.seed_db = seed_db
        self._population: List[tuple] = []  # list of (fitness, code)

    def add_individual(self, code: str, fitness: float) -> None:
        self._population.append((fitness, code))
        self._population.sort(key=lambda x: x[0], reverse=True)
        if len(self._population) > _MAX_POPULATION:
            self._population = self._population[:_MAX_POPULATION]

    def best(self) -> Optional[str]:
        return self._population[0][1] if self._population else None

    def top_fraction(self, fraction: float) -> List[str]:
        k = max(1, int(len(self._population) * fraction))
        return [code for _, code in self._population[:k]]

    def bias_prompt_fragment(self) -> str:
        desc = _BIAS_FRAGMENTS.get(self.bias, self.bias)
        return f"When generating code, focus on {desc}."

    def __len__(self) -> int:
        return len(self._population)


class IslandManager:
    def __init__(
        self,
        num_islands: int,
        biases: List[str],
        seed_db: Optional["SeedDatabase"] = None,
        migration_interval: int = 50,
        migration_fraction: float = 0.10,
    ) -> None:
        # Pad or truncate biases to match num_islands
        padded_biases = (biases * ((num_islands // len(biases)) + 1))[:num_islands]
        self.islands: List[Island] = [
            Island(i, padded_biases[i], seed_db) for i in range(num_islands)
        ]
        self.migration_interval = migration_interval
        self.migration_fraction = migration_fraction

    def get_active_island(self, iteration: int) -> Island:
        return self.islands[iteration % len(self.islands)]

    def maybe_migrate(self, iteration: int) -> None:
        if iteration % self.migration_interval != 0 or iteration == 0:
            return
        if len(self.islands) < 2:
            return
        for src in self.islands:
            if not src._population:
                continue
            emigrants = src.top_fraction(self.migration_fraction)
            # Pick a different island as destination
            others = [isl for isl in self.islands if isl is not src]
            dest = random.choice(others)
            for code in emigrants:
                dest.add_individual(code, fitness=0.5)

    def record_success_rate(self, window_results: list) -> float:
        from Fuzz4All.target.target import FResult
        if not window_results:
            return 0.0
        safe_count = sum(1 for r in window_results if r == FResult.SAFE)
        return safe_count / len(window_results)
