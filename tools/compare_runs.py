#!/usr/bin/env python3
"""Compare HUF-SAEM fuzzing runs (baseline / Phase 1 / autoprompt).

Parses each run's *.fuzz outputs and log_validation.txt and reports the
metrics that matter for the prompt-strategy ablation: compile-success rate,
unique-program ratio, failure-category breakdown, throughput, and compiler-bug
indicators (ICE / segfault). Generator is held constant across arms, so these
isolate the effect of the prompting strategy.

Usage:
    python tools/compare_runs.py RUN_DIR [RUN_DIR ...]
    python tools/compare_runs.py outputs/huf_saem_1phase_10k_run2 outputs/huf_saem_autoprompt_10k

Optionally label arms:
    python tools/compare_runs.py phase1=outputs/.../run2 autoprompt=outputs/.../autoprompt_10k

Reads only; never modifies a run directory.
"""

from __future__ import annotations

import glob
import hashlib
import os
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field

# First-line compiler-stderr patterns -> coarse failure category. Order matters:
# more specific patterns first.
_FAIL_PATTERNS = [
    ("linker (undefined ref)", re.compile(r"undefined reference|/usr/bin/ld")),
    ("redefinition", re.compile(r"error: redefinition")),
    ("redeclaration/conflict", re.compile(r"error: (redeclaration|conflicting|no declaration matches)")),
    ("template/specialization", re.compile(r"error: (template|specializ|explicit special|wrong number of template)")),
    ("stray token", re.compile(r"error: stray")),
    ("preprocessor/macro", re.compile(r"error: (#error|macro|.*preprocessor)")),
    ("type/conversion", re.compile(r"error: (invalid conversion|cannot convert|no match)")),
    ("warning-as-failure", re.compile(r"warning:")),
    ("multiline (In function ...)", re.compile(r": In (function|member function|instantiation)")),
]

# Compiler-bug signals (the only true bugs a compile-only oracle can surface).
_BUG_RE = re.compile(
    r"internal compiler error|segmentation fault|please submit a full bug report",
    re.IGNORECASE,
)


@dataclass
class RunStats:
    label: str
    path: str
    total_files: int = 0
    validated: int = 0
    safe: int = 0
    failed: int = 0
    unique: int = 0
    sizes: list = field(default_factory=list)
    fail_categories: Counter = field(default_factory=Counter)
    bug_hits: int = 0
    wall_seconds: float = 0.0
    best_prompt_score: str | None = None  # from prompts/scores.txt if present

    @property
    def safe_rate(self) -> float:
        return self.safe / self.validated if self.validated else 0.0

    @property
    def unique_rate(self) -> float:
        return self.unique / self.total_files if self.total_files else 0.0

    @property
    def avg_size(self) -> float:
        return statistics.mean(self.sizes) if self.sizes else 0.0

    @property
    def per_hour(self) -> float:
        hrs = self.wall_seconds / 3600
        return self.total_files / hrs if hrs > 0 else 0.0


def analyze(label: str, path: str) -> RunStats:
    st = RunStats(label=label, path=path)
    fuzz_files = glob.glob(os.path.join(path, "*.fuzz"))
    st.total_files = len(fuzz_files)

    # unique programs by content hash + size distribution
    hashes = set()
    for f in fuzz_files:
        try:
            data = open(f, "rb").read()
        except OSError:
            continue
        st.sizes.append(len(data))
        hashes.add(hashlib.md5(data).hexdigest())
    st.unique = len(hashes)

    # parse validation log
    vlog = os.path.join(path, "log_validation.txt")
    if os.path.isfile(vlog):
        with open(vlog, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "Validating" in line:
                    st.validated += 1
                elif "is safe" in line:
                    st.safe += 1
                elif "failed validation" in line:
                    st.failed += 1
                    msg = line.split("error message:", 1)[-1]
                    for name, pat in _FAIL_PATTERNS:
                        if pat.search(msg):
                            st.fail_categories[name] += 1
                            break
                    else:
                        st.fail_categories["other"] += 1
                if _BUG_RE.search(line):
                    st.bug_hits += 1

    # wall-clock: first .fuzz mtime -> latest log mtime
    mtimes = [os.path.getmtime(f) for f in fuzz_files]
    log_mtimes = [
        os.path.getmtime(os.path.join(path, n))
        for n in ("log_validation.txt", "log_generation.txt")
        if os.path.isfile(os.path.join(path, n))
    ]
    if mtimes:
        end = max(log_mtimes) if log_mtimes else max(mtimes)
        st.wall_seconds = max(0.0, end - min(mtimes))

    # autoprompting score (only present on the autoprompt arm)
    scores = os.path.join(path, "prompts", "scores.txt")
    if os.path.isfile(scores):
        st.best_prompt_score = open(scores, encoding="utf-8").read().strip()

    return st


def _fmt_hms(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h{m:02d}m"


def print_comparison(runs: list[RunStats]) -> None:
    w = 26
    cols = "".join(f"{r.label[:18]:>18}" for r in runs)
    print(f"\n{'METRIC':<{w}}{cols}")
    print("-" * (w + 18 * len(runs)))

    def row(name, vals):
        print(f"{name:<{w}}" + "".join(f"{v:>18}" for v in vals))

    row("programs generated", [r.total_files for r in runs])
    row("validated", [r.validated for r in runs])
    row("compiled safe", [r.safe for r in runs])
    row("compile-success rate", [f"{r.safe_rate*100:.1f}%" for r in runs])
    row("failed (invalid prog)", [r.failed for r in runs])
    row("unique programs", [r.unique for r in runs])
    row("unique ratio", [f"{r.unique_rate*100:.1f}%" for r in runs])
    row("avg program size (B)", [f"{r.avg_size:.0f}" for r in runs])
    row("wall-clock", [_fmt_hms(r.wall_seconds) for r in runs])
    row("programs/hour", [f"{r.per_hour:.0f}" for r in runs])
    row("COMPILER-BUG signals", [r.bug_hits for r in runs])

    # failure-category breakdown (union of categories across runs)
    cats = sorted({c for r in runs for c in r.fail_categories})
    if cats:
        print(f"\n{'FAILURE CATEGORY':<{w}}{cols}")
        print("-" * (w + 18 * len(runs)))
        for c in cats:
            row(c, [r.fail_categories.get(c, 0) for r in runs])

    for r in runs:
        if r.best_prompt_score:
            print(f"\n[{r.label}] autoprompt scores:\n{r.best_prompt_score}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    runs = []
    for arg in argv[1:]:
        if "=" in arg and not os.path.exists(arg):
            label, path = arg.split("=", 1)
        else:
            label, path = os.path.basename(arg.rstrip("/")), arg
        if not os.path.isdir(path):
            print(f"WARN: skipping non-directory: {path}", file=sys.stderr)
            continue
        runs.append(analyze(label, path))
    if not runs:
        print("No valid run directories given.", file=sys.stderr)
        return 1
    print_comparison(runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
