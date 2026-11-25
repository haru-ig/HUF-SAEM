#!/usr/bin/env bash
# Step 0: coverage of the regression seeds at plain -O2, then (cumulatively,
# no counter reset) after also compiling each option-declaring seed with its
# own options. Same instrumented GCC and lcov filtering as gap_analysis/collect.sh.
set -uo pipefail
H=/home/ec2-user/HUF-SAEM/experiments/option_check
BUILD=/home/ec2-user/gcc-cov-build; SRC=/home/ec2-user/gcc-cov-src
GXX=/home/ec2-user/gcc-cov/bin/g++; GCOV=/usr/bin/gcov
LCOV_OPTS="--ignore-errors gcov,source,mismatch,unused,empty,negative,unsupported --rc geninfo_unexecuted_blocks=1 --rc branch_coverage=1"
ts() { date -u '+%F %T'; }
compile_jobs() {  # $1 jobs file  $2 label
  awk -F'\t' '{print $1"\t"$2}' "$1" | xargs -P 6 -d '\n' -I{} bash -c '
    IFS=$'"'"'\t'"'"' read -r f o <<< "{}"
    if timeout 60 '"$GXX"' -x c++ -std=c++23 -w $o -c "$f" -o /tmp/optchk_$$.o >/dev/null 2>&1; then echo OK; else echo FAIL; fi' > "$H/compile_$2.txt"
  rm -f /tmp/optchk_*.o
  echo "[$(ts)] $2: ok=$(grep -c OK "$H/compile_$2.txt") fail=$(grep -c FAIL "$H/compile_$2.txt")"
}
capture() {  # $1 label
  lcov --gcov-tool "$GCOV" --capture --directory "$BUILD" $LCOV_OPTS --output-file "$H/cov_$1_raw.info" >/dev/null 2>&1
  lcov $LCOV_OPTS --extract "$H/cov_$1_raw.info" "$SRC/gcc/*" "$BUILD/gcc/*" --output-file "$H/cov_$1_gcc.info" >/dev/null 2>&1
  echo "[$(ts)] $1:"; lcov $LCOV_OPTS --summary "$H/cov_$1_gcc.info" 2>&1 | grep -E 'lines|functions|branches'
}
echo "[$(ts)] reset counters"; ( cd "$BUILD" && find . -name '*.gcda' -delete )
compile_jobs "$H/jobs_O2.txt" O2;      capture seeds_O2
compile_jobs "$H/jobs_opts.txt" opts;  capture seeds_O2_plus_opts
echo "[$(ts)] STEP0 DONE"
