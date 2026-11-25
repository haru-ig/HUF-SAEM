#!/usr/bin/env python3
"""Step 0 of option-aware Phase 1: which regression seeds declare usable options.

Writes jobs_O2.txt (every seed, plain -O2) and jobs_opts.txt (seeds with their
own declared options; -O2 added when they set no -O level), one line per job:
  <seed path>\t<options>
"""
import json, re, os
HERE = os.path.dirname(os.path.abspath(__file__))
SEEDS = "/home/ec2-user/HUF-SAEM/experiments/phase2_regression_seeds"
KEEP = re.compile(r"^(-O[0-3sgz]?|-Ofast|-f[a-z0-9][a-z0-9_=+,.-]*|-m[a-z0-9][a-z0-9_=+,.-]*|-D\S+)$")
DROP = re.compile(r"^(-fdump|-fplugin|-fprofile-use|-fauto-profile|-fself-test|-fcompare-debug|"
                  r"-m(32|x32|16|abi|cmodel=large)|-fno-show|-fmessage|-fdiagnostics|-fmax-errors|-fopt-info)")
rows = [json.loads(l) for l in open(f"{SEEDS}/manifest.jsonl")]
acc = [r for r in rows if r.get("accepted")]
with open(f"{HERE}/jobs_O2.txt", "w") as a, open(f"{HERE}/jobs_opts.txt", "w") as b:
    n = 0
    for r in acc:
        path = f"{SEEDS}/seeds/{r['id']}.cpp"
        a.write(f"{path}\t-O2\n")
        opts = [o for o in r.get("dg_flags", []) if KEEP.match(o) and not DROP.match(o)]
        if not opts:
            continue
        if not any(o.startswith("-O") for o in opts):
            opts = ["-O2"] + opts
        if opts == ["-O2"]:
            continue                      # identical to the baseline compile
        b.write(f"{path}\t{' '.join(opts)}\n")
        n += 1
print(f"{len(acc)} seeds; {n} with options that differ from plain -O2")
