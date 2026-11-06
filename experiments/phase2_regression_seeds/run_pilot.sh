#!/bin/bash
# Phase-2 pilot: generate mutants, then judge them with the tier-1 oracle.
set -o pipefail
cd /home/ec2-user/HUF-SAEM
source /opt/pytorch/bin/activate
D=experiments/phase2_regression_seeds
python $D/mutate_seeds.py --max-seeds 300 --per-seed 10 --workers 2 2>&1 | tee -a $D/mutate.log || { echo "MUTATE FAILED" | tee -a $D/mutate.log; exit 1; }
python $D/evaluate_mutants.py -j 6 2>&1 | tee -a $D/eval.log || { echo "EVAL FAILED" | tee -a $D/eval.log; exit 1; }
echo "PILOT DONE" | tee -a $D/eval.log
