#!/usr/bin/env python3
"""Branch-coverage counterpart to the line-coverage plots.

Branch coverage (~19%) has far more headroom than line coverage (~29%) and is
more sensitive to whether generated inputs exercise deep conditionals/paths in
GCC -- the thing Phase 1 (source-aware autoprompting) is meant to improve. This
draws br_pct against the same three axes as the line-coverage scripts:

  branch_vs_inputs.png      x = #fuzzing inputs generated (raw, size-confounded)
  branch_vs_time.png        x = wall-clock hours
  branch_vs_codevolume.png  x = cumulative MB of code generated (size-normalized)

  python plot_branch_coverage.py
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
    return cum


def load(csv_path, corpus_dir):
    cum = cumulative_bytes(corpus_dir)
    with open(csv_path) as f:
        rows = list(csv.DictReader(f))
    xs_inputs = [int(r["rank"]) for r in rows]
    xs_hours = [float(r["elapsed_sec"]) / 3600.0 for r in rows]
    xs_mb = [cum[int(r["rank"])] / 1048576.0 for r in rows]
    ys = [float(r["br_pct"]) for r in rows]
    return xs_inputs, xs_hours, xs_mb, ys


def draw(series, xidx, xlabel, title, out, xlim=None):
    fig, ax = plt.subplots(figsize=(8, 5))
    for label, color, xs, y in ((s[0], s[1], s[2][xidx], s[3]) for s in series):
        ax.plot(xs, y, marker="o", ms=3, lw=1.8, color=color, label=label)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("GCC branch coverage (%)")
    ax.set_title(title)
    if xlim:
        ax.set_xlim(*xlim)
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(REPO, out), dpi=150)
    print("wrote", out)


def main():
    series = []
    for csv_name, corpus_dir, label, color in ARMS:
        csv_path = os.path.join(REPO, csv_name)
        cdir = os.path.join(REPO, corpus_dir)
        if not (os.path.exists(csv_path) and os.path.isdir(cdir)):
            print(f"skip (missing): {csv_path} / {cdir}")
            continue
        xi, xh, xm, y = load(csv_path, cdir)
        series.append((label, color, (xi, xh, xm), y))

    draw(series, 0, "Fuzzing inputs generated",
         "Branch coverage vs. #inputs (GCC 14.3.1, -O2)",
         "branch_vs_inputs.png", xlim=(0, 10000))
    draw(series, 1, "Wall-clock time (hours)",
         "Branch coverage vs. time (GCC 14.3.1, -O2)",
         "branch_vs_time.png", xlim=(0, 24))
    draw(series, 2, "Cumulative code generated (MB)",
         "Branch coverage vs. code volume (size-normalized, GCC 14.3.1, -O2)",
         "branch_vs_codevolume.png")


if __name__ == "__main__":
    main()
