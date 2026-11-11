#!/usr/bin/env bash
# Phase-1 coverage ablation, CORRECTED. The previous orchestrate_coverage.sh
# compiled *.fuzz files, but ".fuzz" is not a recognized C++ suffix, so g++
# treated every file as a linker input ("linker input file unused"), NEVER
# invoked cc1plus, and exited 0. Only the driver ran -> both arms produced the
# identical 2503-line / 191-function result (driver code is input-independent).
#
# FIX: compile *.cpp symlinks (outputs/<corpus>_gcc/*.cpp -> ../<corpus>/*.fuzz)
# so g++ recognizes them as C++ and actually runs cc1plus (front-end + -O2
# passes + codegen -- the stages Phase 1 targets). We then capture LINE and
# BRANCH coverage of GCC's OWN source only (filtered to gcc-cov-src/gcc and the
# generated gcc-cov-build/gcc files), dropping libstdc++ headers that cc1plus
# pulls in internally.
set -uo pipefail

REPO=/home/ec2-user/HUF-SAEM
PREFIX=/home/ec2-user/gcc-cov
BUILD=/home/ec2-user/gcc-cov-build
SRC=/home/ec2-user/gcc-cov-src
GXX="$PREFIX/bin/g++"
# Read with the SYSTEM gcov (gcc-11, --disable-bootstrap stamped GCC-11 format).
GCOV=/usr/bin/gcov

RUN2="$REPO/outputs/huf_saem_1phase_10k_run2_trunc5856_gcc"   # Phase 1 = ON
BASE="$REPO/outputs/huf_saem_baseline_10k_gcc"                # Phase 1 = OFF
OUT="$REPO/coverage_comparison_gcc.txt"
CFLAGS="-std=c++23 -O2 -c"
TIMEOUT=60   # real -O2 compiles are far slower than the old no-ops
JOBS=6       # parallel compiles. The instrumented cc1plus is ~3.3s/file at -O2;
             # 8 vCPUs -> 6-way cuts wall-time ~5x. Concurrent .gcda dumps are
             # SAFE: libgcov flocks each counter file and merges on dump
             # (pre-flight verified 616 clean files, lcov reads them w/o error).
             # Each job writes a unique -o object ($$) so objects don't clobber.
# Keep only GCC's own compiler source; -O0-drop libstdc++ headers cc1plus inlines.
EXTRACT=("$SRC/gcc/*" "$BUILD/gcc/*")
LCOV_OPTS="--ignore-errors gcov,source,mismatch,unused,empty,negative,unsupported --rc geninfo_unexecuted_blocks=1 --rc branch_coverage=1"

ts() { date -u '+%Y-%m-%d %H:%M:%S'; }
cd "$REPO"

collect() {  # $1=corpus dir  $2=label  $3=ON/OFF
    local dir="$1" label="$2" phase="$3" n=0 ok=0 fail=0
    local total; total=$(ls -1 "$dir"/*.cpp 2>/dev/null | wc -l)
    echo "[cov $(ts)] [$label] resetting counters ..."
    ( cd "$BUILD" && find . -name '*.gcda' -delete )
    echo "[cov $(ts)] [$label] compiling $total programs (${JOBS}-way parallel, $CFLAGS, ${TIMEOUT}s cap) ..."
    local res="/tmp/cov_res_$label.txt"; : > "$res"
    # unique object per PID ($$) so parallel jobs don't clobber each other's -o.
    ls "$dir"/*.cpp | xargs -P "$JOBS" -I{} bash -c \
        'if timeout '"$TIMEOUT"' "'"$GXX"'" '"$CFLAGS"' "{}" -o /tmp/cov_obj_'"$label"'_$$.o >/dev/null 2>&1; then echo OK; else echo FAIL; fi' \
        >> "$res"
    ok=$(grep -c '^OK'   "$res"); fail=$(grep -c '^FAIL' "$res"); n=$((ok+fail))
    rm -f /tmp/cov_obj_${label}_*.o
    echo "[cov $(ts)] [$label] compiled: ok=$ok fail=$fail total=$n"
    echo "[cov $(ts)] [$label] capturing coverage ..."
    lcov --gcov-tool "$GCOV" --capture --directory "$BUILD" $LCOV_OPTS \
         --output-file "$REPO/cov_${label}_raw.info" >/dev/null 2>&1
    # Filter to GCC's own compiler source only.
    lcov $LCOV_OPTS --extract "$REPO/cov_${label}_raw.info" "${EXTRACT[@]}" \
         --output-file "$REPO/cov_${label}_gcc.info" >/dev/null 2>&1
    {
      echo "===== $label (Phase 1 = $phase) ====="
      echo "programs: total=$n compiled_ok=$ok compile_fail=$fail"
      lcov $LCOV_OPTS --summary "$REPO/cov_${label}_gcc.info" 2>&1 | grep -E 'lines|functions|branches'
      echo
    } >> "$OUT"
}

: > "$OUT"
{ echo "PHASE-1 COVERAGE ABLATION vs REAL GCC 14.3.1 (instrumented cc1plus), $CFLAGS"; \
  echo "filtered to gcc-cov-src/gcc + gcc-cov-build/gcc; line + branch coverage"; \
  echo "generated $(ts)"; echo; } >> "$OUT"

echo "[cov $(ts)] instrumented gcc: $($GXX --version | head -1)"
collect "$RUN2" "run2_phase1" "ON"
collect "$BASE" "baseline"    "OFF"

echo "[cov $(ts)] DONE -> $OUT"
echo; echo "================ RESULT ================"; cat "$OUT"
