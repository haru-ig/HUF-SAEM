#!/bin/bash
# After the v2 control: measure it, then compare all four arms.
cd /home/ec2-user/HUF-SAEM
while tmux has-session -t =v2ctlrun 2>/dev/null; do sleep 60; done
source /opt/pytorch/bin/activate
G=experiments/gap_analysis
bash $G/collect.sh $PWD/outputs/phase2_v2_control_run1 v2control
for arm in v2control control4990 p1pilot tseeds; do
  echo "=== $arm: rules vs v2 control ==="
  python3 experiments/phase1_targets/compare_pilot.py $G/cov_${arm}_gcc.info $G/cov_v2control_gcc.info $G/cov_phase2_10k_gcc.info /dev/null 2>/dev/null | sed -n 1,7p
done
echo "=== areas ==="
python3 $G/analyze_gaps.py v1control=$G/cov_control4990_gcc.info v2control=$G/cov_v2control_gcc.info tgt_mutation=$G/cov_p1pilot_gcc.info tgt_seeds=$G/cov_tseeds_gcc.info | sed -n 3,24p
echo "V2 CONTROL ANALYSIS DONE"
