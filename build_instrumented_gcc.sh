#!/usr/bin/env bash
# Build a COVERAGE-INSTRUMENTED GCC 14 (cc1plus emits gcov data every time it
# runs) for the Phase-1 coverage ablation. Matches the system SUT gcc14-g++
# (14.2.x). GPU-independent: runs in parallel with the autoprompt campaign.
#
# Output compiler: /home/ec2-user/gcc-cov/bin/g++
# Coverage artifacts (.gcno now, .gcda after the compiler runs) live in $BUILD.
# Leaves the existing /home/ec2-user/gcc (Phase-1 source dir, trunk) untouched.
set -uo pipefail

SRC=/home/ec2-user/gcc-cov-src
BUILD=/home/ec2-user/gcc-cov-build
PREFIX=/home/ec2-user/gcc-cov
BRANCH=releases/gcc-14

ts() { date -u '+%Y-%m-%d %H:%M:%S'; }

echo "[gccbuild $(ts)] starting"

# 1. Clone the GCC 14 release branch (single-branch, full history -> buildable).
if [ ! -d "$SRC" ]; then
    echo "[gccbuild $(ts)] cloning $BRANCH ..."
    git clone --single-branch --branch "$BRANCH" \
        https://github.com/gcc-mirror/gcc "$SRC" || { echo "CLONE FAILED"; exit 1; }
else
    echo "[gccbuild $(ts)] source already present at $SRC"
fi

# 2. GMP / MPFR / MPC prerequisites (in-tree).
echo "[gccbuild $(ts)] downloading prerequisites ..."
( cd "$SRC" && ./contrib/download_prerequisites ) || { echo "PREREQ FAILED"; exit 1; }

# 3. Out-of-tree configure with coverage instrumentation (=opt -> -O2 + gcov),
#    no bootstrap (stage1 only, built by host gcc) and no multilib for speed.
echo "[gccbuild $(ts)] configuring ..."
rm -rf "$BUILD" && mkdir -p "$BUILD" && cd "$BUILD"
"$SRC/configure" \
    --prefix="$PREFIX" \
    --enable-languages=c,c++ \
    --disable-bootstrap \
    --disable-multilib \
    --enable-coverage=opt || { echo "CONFIGURE FAILED"; exit 1; }

# 4. Build + install.
echo "[gccbuild $(ts)] building (make -j8) ..."
make -j8 || { echo "MAKE FAILED"; exit 1; }
echo "[gccbuild $(ts)] installing ..."
make install || { echo "INSTALL FAILED"; exit 1; }

# 5. Smoke test: the instrumented g++ compiles and emits .gcda when run.
echo "[gccbuild $(ts)] smoke test ..."
echo 'int main(){return 0;}' > /tmp/cov_smoke.cpp
"$PREFIX/bin/g++" -std=c++23 /tmp/cov_smoke.cpp -o /tmp/cov_smoke \
    && echo "[gccbuild $(ts)] OK: $($PREFIX/bin/g++ --version | head -1)" \
    || echo "[gccbuild $(ts)] SMOKE TEST FAILED"
GCDA=$(find "$BUILD" -name '*.gcda' 2>/dev/null | wc -l)
echo "[gccbuild $(ts)] .gcda files present after one compile: $GCDA"
echo "[gccbuild $(ts)] DONE. Instrumented compiler: $PREFIX/bin/g++"
