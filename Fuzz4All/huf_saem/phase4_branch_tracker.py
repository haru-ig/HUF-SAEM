"""Accumulates coverage data across iterations to identify blocked branches."""

from __future__ import annotations

from typing import Dict, List, Optional, Set


class BranchTracker:
    def __init__(self, block_threshold: int = 10) -> None:
        self.block_threshold = block_threshold
        self._miss_counts: Dict[str, int] = {}
        self._branch_source: Dict[str, str] = {}  # branch_id → source_file
        self._all_known: Set[str] = set()

    def record_coverage(self, coverage_data: Dict, source_file: str) -> None:
        covered = coverage_data.get("covered_branches", set())
        uncovered = coverage_data.get("uncovered_branches", set())

        for bid in covered:
            self._all_known.add(bid)
            self._miss_counts[bid] = 0
            self._branch_source[bid] = source_file

        for bid in uncovered:
            self._all_known.add(bid)
            self._miss_counts[bid] = self._miss_counts.get(bid, 0) + 1
            self._branch_source[bid] = source_file

    def blocked_branches(self) -> List[str]:
        return [
            bid
            for bid, count in self._miss_counts.items()
            if count >= self.block_threshold
        ]

    def source_for_branch(self, branch_id: str) -> Optional[str]:
        return self._branch_source.get(branch_id)

    def clear_branch(self, branch_id: str) -> None:
        self._miss_counts.pop(branch_id, None)
