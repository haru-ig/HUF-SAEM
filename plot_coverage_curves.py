#!/usr/bin/env python3
"""Plot the two coverage-curve graphs from the replay CSVs.

  1. line coverage vs. #fuzzing inputs generated  (x cap 10000)
  2. line coverage vs. wall-clock time in hours    (x cap 24h)

Both arms share one y-series each (cumulative line coverage after program k);
we just plot it against two different x columns. Run after the two replays finish:

  python plot_coverage_curves.py
"""
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO = "/home/ec2-user/HUF-SAEM"
ARMS = [
    ("coverage_curve_run2.csv",     "Phase 1 ON (run2)",  "#2563eb"),
    ("coverage_curve_baseline.csv", "Phase 1 OFF (baseline)", "#ea580c"),
]


def load(path):
    with open(path) as f:
        rows = list(csv.DictReader(f))
    xs_inputs = [int(r["rank"]) for r in rows]
    xs_hours = [float(r["elapsed_sec"]) / 3600.0 for r in rows]
    ys = [float(r["lines_pct"]) for r in rows]
    return xs_inputs, xs_hours, ys


def main():
    series = []
    for fname, label, color in ARMS:
        path = os.path.join(REPO, fname)
        if not os.path.exists(path):
            print(f"skip (missing): {path}")
            continue
        xi, xh, y = load(path)
        series.append((label, color, xi, xh, y))

    # Graph 1: coverage vs #inputs
    fig, ax = plt.subplots(figsize=(8, 5))
    for label, color, xi, xh, y in series:
        ax.plot(xi, y, marker="o", ms=3, lw=1.8, color=color, label=label)
    ax.set_xlabel("Fuzzing inputs generated")
    ax.set_ylabel("GCC line coverage (%)")
    ax.set_title("Line coverage vs. #inputs (GCC 14.3.1 self-coverage, -O2)")
    ax.set_xlim(0, 10000)
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    out1 = os.path.join(REPO, "coverage_vs_inputs.png")
    fig.savefig(out1, dpi=150)
    print("wrote", out1)

    # Graph 2: coverage vs time
    fig, ax = plt.subplots(figsize=(8, 5))
    for label, color, xi, xh, y in series:
        ax.plot(xh, y, marker="o", ms=3, lw=1.8, color=color, label=label)
    ax.set_xlabel("Wall-clock time (hours)")
    ax.set_ylabel("GCC line coverage (%)")
    ax.set_title("Line coverage vs. time (GCC 14.3.1 self-coverage, -O2)")
    ax.set_xlim(0, 24)
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    out2 = os.path.join(REPO, "coverage_vs_time.png")
    fig.savefig(out2, dpi=150)
    print("wrote", out2)


if __name__ == "__main__":
    main()
