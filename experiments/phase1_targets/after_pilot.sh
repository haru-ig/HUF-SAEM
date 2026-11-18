#!/bin/bash
# After the targeted pilot: measure pilot + control coverage (serially: shared
# instrumented build), then compare match.pd rules and target hits.
cd /home/ec2-user/HUF-SAEM
while tmux has-session -t =p1pilot 2>/dev/null; do sleep 60; done
source /opt/pytorch/bin/activate
G=experiments/gap_analysis
# control = first 4990 programs of the 10k run, in generation order
C=experiments/phase1_targets/control_4990; rm -rf $C; mkdir -p $C
ls -tr outputs/huf_saem_phase2_regression_10k_run1/*.fuzz | head -4990 | while read f; do ln -s "$PWD/$f" "$C/$(basename $f)"; done
echo "control files: $(ls $C | wc -l)"
bash $G/collect.sh $PWD/outputs/phase1_targets_pilot_run1 p1pilot
bash $G/collect.sh $PWD/$C control4990
python3 experiments/phase1_targets/compare_pilot.py $G/cov_p1pilot_gcc.info $G/cov_control4990_gcc.info $G/cov_phase2_10k_gcc.info outputs/phase1_targets_pilot_run1/phase2_provenance.jsonl
echo "PILOT ANALYSIS DONE"
