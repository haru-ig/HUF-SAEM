#!/bin/bash
# Wait for the ablation fuzzing run, build the replay corpus
# (Phase-1 run2 programs 0..1999, then every non-seed output of the run, in
# generation order), and replay coverage on it.
cd /home/ec2-user/HUF-SAEM
while tmux has-session -t =p2v2run 2>/dev/null; do sleep 60; done
source /opt/pytorch/bin/activate
python3 - <<'PY'
import json, os, shutil
run = 'outputs/phase2_engine_v2_run1'
src = 'outputs/huf_saem_1phase_10k_run2'
out = 'experiments/phase2_engine_ablation/replay_corpus_v2'
os.makedirs(out, exist_ok=True)
seeds = {json.loads(l)['file'] for l in open(f'{run}/phase2_provenance.jsonl')
         if json.loads(l)['origin'] == 'seed'}
rest = sorted((os.path.join(run, f) for f in os.listdir(run) if f.endswith('.fuzz')),
              key=os.path.getmtime)
rest = [p for p in rest if p not in seeds]          # mutants + fallback generations
files = [f'{src}/{i}.fuzz' for i in range(2000)] + rest
base = 1_800_000_000
for i, p in enumerate(files):
    d = f'{out}/{i:05d}.fuzz'
    shutil.copy(p, d); os.utime(d, (base + i, base + i))
print(f'replay corpus: 2000 phase-1 programs + {len(rest)} run outputs = {len(files)}')
PY
bash replay_coverage_curve.sh /home/ec2-user/HUF-SAEM/experiments/phase2_engine_ablation/replay_corpus_v2 phase2_engine_v2 0 250
echo "V2 REPLAY DONE"
