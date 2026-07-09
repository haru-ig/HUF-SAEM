#!/usr/bin/env python3
"""Size-normalized coverage plot: line coverage vs. cumulative code volume generated.

The per-#inputs comparison is confounded by program size (baseline generates
~3.5x bigger programs, so each input touches more of GCC). This replots the same
coverage y-series against the cumulative *bytes of code generated* instead of the
input count, so both arms are compared at equal "code volume" — a fairer axis for
judging whether Phase 1 helps.

Cumulative bytes are built by ordering each corpus's N.fuzz files by mtime
(== generation order, same order the replay used) and prefix-summing their sizes;
each coverage-CSV checkpoint `rank` then maps to the cumulative bytes after that
many programs. Run after the replays exist:

  python plot_coverage_normalized.py
"""
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO = "/home/ec2-user/HUF-SAEM"
ARMS = [
    ("coverage_curve_run2.csv",     "outputs/huf_saem_1phase_10k_run2", "Phase 1 ON (run2)",      "#2563eb"),
    ("coverage_curve_baseline.csv", "outputs/huf_saem_baseline_10k",    "Phase 1 OFF (baseline)", "#ea580c"),
]


def cumulative_bytes(corpus_dir):
    """Prefix sum of .fuzz sizes in mtime (generation) order. Index i = bytes after i files."""
    entries = []
    with os.scandir(corpus_dir) as it:
        for e in it:
            if e.name.endswith(".fuzz"):
                st = e.stat()
                entries.append((st.st_mtime, st.st_size))
    entries.sort(key=lambda t: t[0])
    cum, running = [0], 0
    for _, size in entries:
        running += size
        cum.append(running)
    return cum  # len = n_files + 1; cum[rank] = bytes after `rank` programs


def load(csv_path, corpus_dir):
    cum = cumulative_bytes(corpus_dir)
    with open(csv_path) as f:
        rows = list(csv.DictReader(f))
    xs_mb = [cum[int(r["rank"])] / 1048576.0 for r in rows]
    ys = [float(r["lines_pct"]) for r in rows]
    return xs_mb, ys


def main():
    series = []
    for csv_name, corpus_dir, label, color in ARMS:
        csv_path = os.path.join(REPO, csv_name)
        cdir = os.path.join(REPO, corpus_dir)
        if not (os.path.exists(csv_path) and os.path.isdir(cdir)):
            print(f"skip (missing): {csv_path} / {cdir}")
            continue
        xm, y = load(csv_path, cdir)
        series.append((label, color, xm, y))

    fig, ax = plt.subplots(figsize=(8, 5))
    for label, color, xm, y in series:
        ax.plot(xm, y, marker="o", ms=3, lw=1.8, color=color, label=label)
    ax.set_xlabel("Cumulative code generated (MB)")
    ax.set_ylabel("GCC line coverage (%)")
    ax.set_title("Line coverage vs. code volume (size-normalized, GCC 14.3.1, -O2)")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    out = os.path.join(REPO, "coverage_vs_codevolume.png")
    fig.savefig(out, dpi=150)
    print("wrote", out)


if __name__ == "__main__":
    main()
