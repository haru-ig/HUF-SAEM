#!/usr/bin/env bash
# Coverage-curve REPLAY (no re-fuzzing, no GPU, no LLM).
#
# The fuzzing campaigns are already done: each program is saved as N.fuzz in the
# corpus dir, where N is the generation index and the file mtime is the wall-clock
# moment it was produced. This script recompiles those saved programs IN GENERATION
# ORDER under the instrumented GCC, WITHOUT resetting gcov counters between them, so
# libgcov merges each program's coverage into the cumulative total. At checkpoints it
# runs `lcov --capture` and records cumulative coverage.
#
# The result is ONE ordered y-series (cumulative coverage after program k) that can be
# plotted against TWO x-axes: the generation index (k) -> "coverage vs #inputs", and
# the file mtime -> "coverage vs wall-clock time". One replay feeds both graphs.
#
# We compile with `-x c++` (not the .fuzz suffix) so g++ actually invokes cc1plus --
# see the .fuzz-extension bug that made the earlier one-shot measurement compile only
# the input-independent driver.
#
# Usage:
#   replay_coverage_curve.sh <corpus_dir> <label> [max_files] [checkpoint_interval]
#     corpus_dir           dir of N.fuzz programs
#     label                short tag -> coverage_curve_<label>.csv
#     max_files            0 = all (default 0); >0 truncates to first N by mtime (sanity slices)
#     checkpoint_interval  capture every this many compiles (default 250)
set -uo pipefail

REPO=/home/ec2-user/HUF-SAEM
PREFIX=/home/ec2-user/gcc-cov
BUILD=/home/ec2-user/gcc-cov-build
SRC=/home/ec2-user/gcc-cov-src
GXX="$PREFIX/bin/g++"
GCOV=/usr/bin/gcov                       # --disable-bootstrap stamped GCC-11 .gcda format
CFLAGS="-std=c++23 -O2 -c"
TIMEOUT=60
EXTRACT=("$SRC/gcc/*" "$BUILD/gcc/*")    # GCC's own compiler source only; drop libstdc++ headers
LCOV_OPTS="--ignore-errors gcov,source,mismatch,unused,empty,negative,unsupported --rc geninfo_unexecuted_blocks=1 --rc branch_coverage=1"

DIR="${1:?corpus_dir required}"
LABEL="${2:?label required}"
MAXFILES="${3:-0}"
INTERVAL="${4:-250}"

# Dense early checkpoints: coverage rises steeply in the first few hundred programs,
# so sample there regardless of INTERVAL. After these, checkpoint every INTERVAL.
EARLY=(10 25 50 100 150 250 500 750)

CSV="$REPO/coverage_curve_${LABEL}.csv"
RAW="/tmp/replay_${LABEL}_raw.info"
EXT="/tmp/replay_${LABEL}_gcc.info"
OBJ="/tmp/replay_${LABEL}_obj.o"
ORDER="/tmp/replay_${LABEL}_order.txt"

ts() { date -u '+%Y-%m-%d %H:%M:%S'; }

# Ordered program list: "<mtime_epoch> <path>", sorted by mtime (== generation order).
find "$DIR" -maxdepth 1 -name '*.fuzz' -printf '%T@ %p\n' | sort -n > "$ORDER"
if [[ "$MAXFILES" -gt 0 ]]; then
    head -n "$MAXFILES" "$ORDER" > "${ORDER}.tmp" && mv "${ORDER}.tmp" "$ORDER"
fi
TOTAL=$(wc -l < "$ORDER")
FIRST_MTIME=$(head -1 "$ORDER" | awk '{print $1}')

echo "[replay $(ts)] label=$LABEL dir=$DIR programs=$TOTAL interval=$INTERVAL"
echo "[replay $(ts)] gxx: $($GXX --version | head -1)"

# Capture cumulative coverage and append a CSV row. Args: rank index mtime cum_ok cum_fail
checkpoint() {
    local rank="$1" idx="$2" mtime="$3" cum_ok="$4" cum_fail="$5"
    lcov --gcov-tool "$GCOV" --capture --directory "$BUILD" $LCOV_OPTS \
         --output-file "$RAW" >/dev/null 2>&1
    lcov $LCOV_OPTS --extract "$RAW" "${EXTRACT[@]}" \
         --output-file "$EXT" >/dev/null 2>&1
    local summary; summary=$(lcov $LCOV_OPTS --summary "$EXT" 2>&1)
    # Parse "  lines......: 28.3% (318404 of 1123441 lines)" etc.
    local row
    row=$(echo "$summary" | awk -v r="$rank" -v i="$idx" -v mt="$mtime" -v ft="$FIRST_MTIME" \
        -v ok="$cum_ok" -v fl="$cum_fail" '
        function grab(s,   n){ if (match(s,/([0-9]+) of ([0-9]+)/)){ split(substr(s,RSTART,RLENGTH),a," "); hit=a[1]; tot=a[3] } }
        /lines/     { grab($0); lh=hit; lt=tot; if(match($0,/[0-9.]+%/)) lp=substr($0,RSTART,RLENGTH-1) }
        /functions/ { grab($0); fh=hit; ft2=tot; if(match($0,/[0-9.]+%/)) fp=substr($0,RSTART,RLENGTH-1) }
        /branches/  { grab($0); bh=hit; bt=tot; if(match($0,/[0-9.]+%/)) bp=substr($0,RSTART,RLENGTH-1) }
        END { printf "%s,%s,%.3f,%.3f,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n",
                     r,i,mt,(mt-ft),ok,fl,lh,lt,lp,fh,ft2,fp,bh,bt,bp }')
    echo "$row" >> "$CSV"
    echo "[replay $(ts)] ckpt rank=$rank idx=$idx  lines=$(echo "$row"|cut -d, -f9)%  ok=$cum_ok fail=$cum_fail"
}

echo "[replay $(ts)] resetting counters ..."
( cd "$BUILD" && find . -name '*.gcda' -delete )

echo "rank,file_index,mtime_epoch,elapsed_sec,cum_compiled_ok,cum_compiled_fail,lines_hit,lines_total,lines_pct,funcs_hit,funcs_total,funcs_pct,br_hit,br_total,br_pct" > "$CSV"

rank=0; ok=0; fail=0
while read -r mtime path; do
    rank=$((rank+1))
    idx=$(basename "$path" .fuzz)
    if timeout "$TIMEOUT" "$GXX" -x c++ $CFLAGS "$path" -o "$OBJ" >/dev/null 2>&1; then
        ok=$((ok+1))
    else
        fail=$((fail+1))
    fi
    do_ckpt=0
    (( rank % INTERVAL == 0 )) && do_ckpt=1
    (( rank == TOTAL )) && do_ckpt=1
    for e in "${EARLY[@]}"; do (( rank == e )) && do_ckpt=1; done
    if (( do_ckpt )); then
        checkpoint "$rank" "$idx" "$mtime" "$ok" "$fail"
    fi
done < "$ORDER"

rm -f "$OBJ"
echo "[replay $(ts)] DONE -> $CSV  (compiled ok=$ok fail=$fail of $TOTAL)"
echo; echo "===== $CSV ====="; cat "$CSV"
