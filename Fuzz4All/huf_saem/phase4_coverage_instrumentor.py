"""Wraps compiler invocation to collect branch/line coverage data.

Concrete implementation for C/C++ using gcov.
Stubs provided for Go (go test -coverprofile) and Python/Qiskit (coverage.py).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import Dict, Optional, Set


class CoverageInstrumentor:
    def __init__(
        self,
        language: str,
        compiler: str,
        output_dir: str,
        gcov_binary: str = "gcov",
        timeout: int = 15,
    ) -> None:
        self.language = language
        self.compiler = compiler
        self.output_dir = output_dir
        self.gcov_binary = gcov_binary
        self.timeout = timeout
        self._cov_dir = os.path.join(output_dir, "coverage_data")
        os.makedirs(self._cov_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # C / C++ — gcov-based
    # ------------------------------------------------------------------

    def compile_with_coverage(self, source_file: str) -> Optional[str]:
        exe = source_file.replace(".fuzz", ".cov_exe")
        flags = [
            self.compiler,
            source_file,
            "-o", exe,
            "-fprofile-arcs",
            "-ftest-coverage",
            "--coverage",
        ]
        if self.language == "cpp":
            flags += ["-std=c++17", "-lstdc++"]
        try:
            result = subprocess.run(
                flags,
                capture_output=True,
                timeout=self.timeout,
            )
            return exe if result.returncode == 0 else None
        except Exception:
            return None

    def run_and_collect(self, executable: str, source_file: str) -> Dict:
        try:
            subprocess.run(
                [executable],
                capture_output=True,
                timeout=self.timeout,
            )
        except Exception:
            pass

        gcov_result = subprocess.run(
            [self.gcov_binary, "-b", "-c", source_file],
            capture_output=True,
            timeout=self.timeout,
            cwd=os.path.dirname(source_file) or ".",
        )
        gcov_output = gcov_result.stdout.decode("utf-8", errors="replace")
        return _parse_gcov_output(gcov_output, source_file)

    # ------------------------------------------------------------------
    # Stubs for other languages
    # ------------------------------------------------------------------

    def stub_collect(self, source_file: str) -> Dict:
        # TODO: implement for Go with `go test -coverprofile`
        # TODO: implement for Python/Qiskit with coverage.py
        return {
            "covered_lines": set(),
            "uncovered_lines": set(),
            "covered_branches": set(),
            "uncovered_branches": set(),
            "gcov_output": "",
        }


def _parse_gcov_output(gcov_output: str, source_file: str) -> Dict:
    covered_lines: Set[str] = set()
    uncovered_lines: Set[str] = set()
    covered_branches: Set[str] = set()
    uncovered_branches: Set[str] = set()

    base = os.path.basename(source_file)
    # Parse .gcov file if present
    gcov_file = source_file + ".gcov"
    if not os.path.exists(gcov_file):
        # gcov may write to cwd
        gcov_file = os.path.join(
            os.path.dirname(source_file) or ".",
            base + ".gcov",
        )

    if os.path.exists(gcov_file):
        with open(gcov_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # Format: <count>:<lineno>:<source>
                m = re.match(r"^\s*(\S+)\s*:\s*(\d+)\s*:", line)
                if not m:
                    continue
                count_str, lineno = m.group(1), m.group(2)
                branch_id = f"{base}:{lineno}"
                if count_str == "#####":
                    uncovered_lines.add(branch_id)
                elif count_str.isdigit() and int(count_str) > 0:
                    covered_lines.add(branch_id)
                # Branch lines
                bm = re.match(r"^\s*branch\s+(\d+)\s+(taken|not taken)", line)
                if bm:
                    idx, status = bm.group(1), bm.group(2)
                    bid = f"{base}:{lineno}:b{idx}"
                    if status == "taken":
                        covered_branches.add(bid)
                    else:
                        uncovered_branches.add(bid)

    return {
        "covered_lines": covered_lines,
        "uncovered_lines": uncovered_lines,
        "covered_branches": covered_branches,
        "uncovered_branches": uncovered_branches,
        "gcov_output": gcov_output,
    }
