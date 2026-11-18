#!/bin/bash
# After the target-seed arm: measure coverage, compare with control + pilot.
cd /home/ec2-user/HUF-SAEM
while tmux has-session -t =tseedrun 2>/dev/null; do sleep 60; done
source /opt/pytorch/bin/activate
G=experiments/gap_analysis
bash $G/collect.sh $PWD/outputs/phase1_target_seeds_run1 tseeds
echo "=== rules: target-seed arm (vs control) ==="
python3 experiments/phase1_targets/compare_pilot.py $G/cov_tseeds_gcc.info $G/cov_control4990_gcc.info $G/cov_phase2_10k_gcc.info outputs/phase1_target_seeds_run1/phase2_provenance.jsonl
echo "=== rules: targeted-mutation pilot (vs control), for reference ==="
python3 experiments/phase1_targets/compare_pilot.py $G/cov_p1pilot_gcc.info $G/cov_control4990_gcc.info $G/cov_phase2_10k_gcc.info outputs/phase1_targets_pilot_run1/phase2_provenance.jsonl | head -7
echo "=== areas ==="
python3 $G/analyze_gaps.py control=$G/cov_control4990_gcc.info pilot=$G/cov_p1pilot_gcc.info tseeds=$G/cov_tseeds_gcc.info | sed -n 3,24p
echo "TSEEDS ANALYSIS DONE"
