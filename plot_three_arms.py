#!/usr/bin/env python3
"""Plot GCC line coverage for three arms against three x-axes.

Arms: baseline (all phases off), Phase 1 only (run2), Phase 1+2 (10k run1).
Graphs (y = GCC line coverage %):
  1. coverage vs. cumulative code volume generated (MB)
  2. coverage vs. #fuzzing inputs generated
  3. coverage vs. wall-clock time (hours)

Run after the replays exist:
  python plot_three_arms.py
"""
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO = "/home/ec2-user/HUF-SAEM"

# (coverage_csv, corpus_dir, label, color)
ARMS = [
    ("coverage_curve_baseline.csv",    "outputs/huf_saem_baseline_10k",     "Baseline (all phases off)", "#ea580c"),
    ("coverage_curve_run2.csv",        "outputs/huf_saem_1phase_10k_run2",  "Phase 1 only",              "#2563eb"),
    ("coverage_curve_phase12_10k.csv", "outputs/huf_saem_phase12_10k_run1", "Phase 1 + 2",               "#16a34a"),
]


def cumulative_bytes(corpus_dir):
    """Prefix sum of .fuzz sizes in mtime (generation) order. cum[rank] = bytes after `rank` files."""
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
    xs_mb = [cum[int(r["rank"])] / 1048576.0 for r in rows]
    xs_inputs = [int(r["rank"]) for r in rows]
    xs_hours = [float(r["elapsed_sec"]) / 3600.0 for r in rows]
    ys = [float(r["lines_pct"]) for r in rows]
    return xs_mb, xs_inputs, xs_hours, ys


def make_plot(series, x_index, xlabel, title, xlim, out_name):
    fig, ax = plt.subplots(figsize=(8, 5))
    for label, color, data in series:
        ax.plot(data[x_index], data[3], marker="o", ms=3, lw=1.8, color=color, label=label)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("GCC line coverage (%)")
    ax.set_title(title)
    if xlim is not None:
        ax.set_xlim(*xlim)
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    out = os.path.join(REPO, out_name)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print("wrote", out)


def main():
    series = []
    for csv_name, corpus_dir, label, color in ARMS:
        csv_path = os.path.join(REPO, csv_name)
        cdir = os.path.join(REPO, corpus_dir)
        if not (os.path.exists(csv_path) and os.path.isdir(cdir)):
            print(f"skip (missing): {csv_path} / {cdir}")
            continue
        series.append((label, color, load(csv_path, cdir)))

    make_plot(series, 0, "Cumulative code generated (MB)",
              "Line coverage vs. code volume (GCC 14.3.1, -O2)",
              None, "three_arms_coverage_vs_codevolume.png")
    make_plot(series, 1, "Fuzzing inputs generated",
              "Line coverage vs. #inputs (GCC 14.3.1, -O2)",
              (0, 10000), "three_arms_coverage_vs_inputs.png")
    make_plot(series, 2, "Wall-clock time (hours)",
              "Line coverage vs. time (GCC 14.3.1, -O2)",
              None, "three_arms_coverage_vs_time.png")


if __name__ == "__main__":
    main()
