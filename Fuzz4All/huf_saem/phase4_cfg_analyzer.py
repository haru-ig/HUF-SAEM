"""Estimates CFG centrality of a blocked branch using coverage data heuristics."""

from __future__ import annotations

import os
import re


class CFGAnalyzer:
    def __init__(self, language: str, min_downstream: int = 5) -> None:
        self.language = language
        self.min_downstream = min_downstream

    def estimate_downstream_size(self, source_file: str, branch_id: str) -> int:
        """Count contiguous uncovered lines after the blocked branch line."""
        gcov_file = source_file + ".gcov"
        if not os.path.exists(gcov_file):
            gcov_file = os.path.join(
                os.path.dirname(source_file) or ".",
                os.path.basename(source_file) + ".gcov",
            )
        if not os.path.exists(gcov_file):
            return 0

        # branch_id format: basename:lineno:bN
        parts = branch_id.split(":")
        try:
            branch_line = int(parts[1]) if len(parts) >= 2 else 0
        except ValueError:
            return 0

        downstream = 0
        past_branch = False
        with open(gcov_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"^\s*(\S+)\s*:\s*(\d+)\s*:", line)
                if not m:
                    continue
                count_str = m.group(1)
                lineno = int(m.group(2))
                if lineno == branch_line:
                    past_branch = True
                    continue
                if past_branch and count_str == "#####":
                    downstream += 1
                elif past_branch and count_str != "#####" and downstream > 0:
                    break  # end of contiguous uncovered block
        return downstream

    def is_worth_targeting(self, source_file: str, branch_id: str) -> bool:
        return self.estimate_downstream_size(source_file, branch_id) >= self.min_downstream
