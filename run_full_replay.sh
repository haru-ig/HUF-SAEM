#!/usr/bin/env bash
# Full coverage-curve replay for both ablation arms. SERIAL by necessity: both arms
# share /home/ec2-user/gcc-cov-build, so their .gcda merges would collide if run
# concurrently. run2 (10k) first, then baseline (5856).
set -uo pipefail
cd /home/ec2-user/HUF-SAEM
echo "=== [$(date -u '+%F %T')] START run2 (Phase 1 ON) ==="
./replay_coverage_curve.sh outputs/huf_saem_1phase_10k_run2 run2 0 250
echo "=== [$(date -u '+%F %T')] START baseline (Phase 1 OFF) ==="
./replay_coverage_curve.sh outputs/huf_saem_baseline_10k baseline 0 250
echo "=== [$(date -u '+%F %T')] ALL DONE ==="
