#!/usr/bin/env python3
"""Targeted pilot vs control: match.pd rules applied and live target hits."""
import json, sys
sys.path.insert(0, "/home/ec2-user/HUF-SAEM")
from Fuzz4All.huf_saem.phase1_matchpd_targets import extract_rules, rule_index, load_sites, applied_rules

pilot_info, control_info, full10k_info, prov = sys.argv[1:5]
text = open("/home/ec2-user/gcc-cov-src/gcc/match.pd").read()
rules = extract_rules(text); idx = rule_index(rules)
sites = [s for s in load_sites("/home/ec2-user/gcc-cov-build/gcc") if s.simplify]
trace = {idx[s.pd_line].start for s in sites if s.pd_line in idx}
A = {k: applied_rules(sites, idx, [p]) & trace for k, p in
     [("pilot", pilot_info), ("control", control_info), ("full10k", full10k_info)]}
print(f"transform rules applied (of {len(trace)}):")
for k in ("control", "pilot", "full10k"):
    print(f"  {k:8s} {len(A[k]):4d}")
print(f"  pilot rules the FULL 10k corpus never applied : {len(A['pilot'] - A['full10k'])}")
print(f"  control rules the full 10k never applied      : {len(A['control'] - A['full10k'])}  (should be 0)")
print(f"  pilot - control: +{len(A['pilot'] - A['control'])} / -{len(A['control'] - A['pilot'])}")
rows = [json.loads(l) for l in open(prov)]
t = [r for r in rows if r.get("op") == "target"]
print(f"\ntargeted mutants: {len(t)}, compiled {sum(r['result']=='SAFE' for r in t)}, "
      f"hit their target {sum(bool(r.get('target_hit')) for r in t)}, distinct targets hit "
      f"{len({r['target'] for r in t if r.get('target_hit')})} of {len({r['target'] for r in t})} tried")
