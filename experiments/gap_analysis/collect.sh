#!/usr/bin/env bash
# Final (non-cumulative-curve) coverage of one corpus, kept on disk for line-level
# analysis. Same instrumented GCC, flags and lcov filtering as
# replay_coverage_curve.sh, but 6-way parallel and a single capture at the end.
# Usage: collect.sh <corpus_dir> <label>   -> experiments/gap_analysis/cov_<label>_gcc.info
set -uo pipefail
DIR="${1:?corpus}"; LABEL="${2:?label}"
OUT=/home/ec2-user/HUF-SAEM/experiments/gap_analysis
PREFIX=/home/ec2-user/gcc-cov; BUILD=/home/ec2-user/gcc-cov-build; SRC=/home/ec2-user/gcc-cov-src
GXX="$PREFIX/bin/g++"; GCOV=/usr/bin/gcov
LCOV_OPTS="--ignore-errors gcov,source,mismatch,unused,empty,negative,unsupported --rc geninfo_unexecuted_blocks=1 --rc branch_coverage=1"
ts() { date -u '+%F %T'; }
echo "[$(ts)] reset counters"; ( cd "$BUILD" && find . -name '*.gcda' -delete )
echo "[$(ts)] compiling $(ls "$DIR"/*.fuzz | wc -l) programs, 6-way, -std=c++23 -O2 -c"
# path<TAB>options per program (`// huf-options:` header, allowlisted); a
# program's own options follow -O2, so its -O level wins.
( cd /home/ec2-user/HUF-SAEM && /opt/pytorch/bin/python -m Fuzz4All.huf_saem.options "$DIR" ) > "$OUT/jobs_$LABEL.txt"
echo "[$(ts)] programs with their own options: $(awk -F'\t' '$2!=""' "$OUT/jobs_$LABEL.txt" | wc -l)"
xargs -P 6 -d '\n' -I{} bash -c 'IFS=$'"'"'\t'"'"' read -r f o <<< "{}"; timeout 60 '"$GXX"' -x c++ -std=c++23 -O2 $o -c "$f" -o /tmp/gap_obj_$$.o >/dev/null 2>&1 && echo OK || echo FAIL' < "$OUT/jobs_$LABEL.txt" > "$OUT/compile_$LABEL.txt"
rm -f /tmp/gap_obj_*.o
echo "[$(ts)] ok=$(grep -c OK "$OUT/compile_$LABEL.txt") fail=$(grep -c FAIL "$OUT/compile_$LABEL.txt")"
echo "[$(ts)] capture"
lcov --gcov-tool "$GCOV" --capture --directory "$BUILD" $LCOV_OPTS --output-file "$OUT/cov_${LABEL}_raw.info" >/dev/null 2>&1
lcov $LCOV_OPTS --extract "$OUT/cov_${LABEL}_raw.info" "$SRC/gcc/*" "$BUILD/gcc/*" --output-file "$OUT/cov_${LABEL}_gcc.info" >/dev/null 2>&1
lcov $LCOV_OPTS --summary "$OUT/cov_${LABEL}_gcc.info" 2>&1 | grep -E 'lines|functions|branches'
echo "[$(ts)] COLLECT DONE"
