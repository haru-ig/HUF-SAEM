#!/usr/bin/env python3
"""Merge seed sets (manifest.jsonl + seeds/) into one set Phase 2 can load.

Usage: merge_seed_sets.py OUT_DIR SET_DIR [SET_DIR ...]
Seeds are symlinked, manifests concatenated; duplicate ids keep the first.
"""
import json, os, sys

out, sets = sys.argv[1], sys.argv[2:]
os.makedirs(os.path.join(out, "seeds"), exist_ok=True)
rows, seen = [], set()
for d in sets:
    d = os.path.abspath(d)
    n = 0
    for line in open(os.path.join(d, "manifest.jsonl")):
        r = json.loads(line)
        if r.get("accepted"):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            link = os.path.join(out, "seeds", f"{r['id']}.cpp")
            if not os.path.lexists(link):
                os.symlink(os.path.join(d, "seeds", f"{r['id']}.cpp"), link)
            n += 1
        rows.append(r)
    print(f"{d}: {n} accepted seeds")
with open(os.path.join(out, "manifest.jsonl"), "w") as f:
    f.writelines(json.dumps(r) + "\n" for r in rows)
print(f"merged: {len(seen)} accepted seeds -> {out}")
