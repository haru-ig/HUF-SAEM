"""Per-program compiler options ("option-aware" Phase 1 / Phase 2).

A program may carry the GCC options it should be compiled with in its first
line:

    // huf-options: -O3 -ffast-math -mavx2

Every stage reads it -- the fuzzer's validation, the differential oracle, the
coverage replays, the Phase 1 checks -- so options travel with the program
through copies and symlinks and can never get out of sync. A program without
the header compiles exactly as before.

Options must pass an allowlist: they have to change what GCC does with the
code (optimisation level, pass switches, float semantics, x86 CPU features,
debug info, OpenMP, LTO, sanitizers, macros) without breaking the pipeline
(no dump/plugin/profile-use/output-file options, no other targets' -m
options, no -std: the language level stays C++23).

Step 0 (experiments/option_check/): recompiling 652 regression seeds with
their own declared options added +32,409 GCC lines (34.6% -> 37.5%); ~80% of
that came from options other than the -O level.
"""

from __future__ import annotations

import functools
import re
import subprocess
from typing import List, Optional, Sequence, Tuple

HEADER = "// huf-options:"
_HEADER_RE = re.compile(r"\A[ \t]*//[ \t]*huf-options:[ \t]*([^\n]*)\n?")

# --------------------------------------------------------------------------- #
# header
# --------------------------------------------------------------------------- #


def parse_options(code: str) -> List[str]:
    """Options from the program's header line ([] when there is none)."""
    m = _HEADER_RE.match(code or "")
    return m.group(1).split() if m else []


def strip_options(code: str) -> str:
    """The program without its options header."""
    return _HEADER_RE.sub("", code or "", count=1)


def set_options(code: str, opts: Sequence[str]) -> str:
    """The program with exactly `opts` in its header (no header if empty)."""
    body = strip_options(code)
    return f"{HEADER} {' '.join(opts)}\n{body}" if opts else body


def read_options(path: str) -> List[str]:
    """Options from a program file's header, reading only its first line."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return parse_options(f.readline() + "\n")
    except OSError:
        return []


# --------------------------------------------------------------------------- #
# allowlist
# --------------------------------------------------------------------------- #

_LEVEL = re.compile(r"^-O([0-3sgz]|fast)?$")

# x86 CPU names and ISA extensions GCC 14 accepts for x86-64.
_X86_ARCH = (r"x86-64(-v[234])?|native|core2|core-avx2|core-avx-i|corei7|corei7-avx|slm|nehalem|westmere|sandybridge|ivybridge|haswell|"
             r"broadwell|skylake|skylake-avx512|cascadelake|cooperlake|cannonlake|icelake-client|"
             r"icelake-server|tigerlake|sapphirerapids|alderlake|rocketlake|znver[1-4]|"
             r"btver[12]|bdver[1-4]|silvermont|goldmont|goldmont-plus|tremont|atom|bonnell|"
             r"k8|k8-sse3|amdfam10|opteron|athlon64|generic|intel")
# -mtune may also name 32-bit-era CPUs: it only changes scheduling, never the ISA.
_X86_TUNE_ONLY = r"i386|i486|i586|i686|pentium\w*|k6\w*|athlon\w*|geode|lakemont"
_X86_ISA = (r"sse|sse2|sse3|ssse3|sse4|sse4\.1|sse4\.2|sse4a|avx|avx2|avx512\w+|avxvnni\w*|fma|fma4|"
            r"bmi|bmi2|lzcnt|popcnt|f16c|movbe|aes|pclmul|sha|adx|abm|tbm|xop|rdrnd|rdseed|"
            r"vpclmulqdq|gfni|vaes|cx16|sahf|mmx|3dnow|crc32|evex512")
_M_ALLOW = re.compile(
    rf"^-m(arch|tune)=({_X86_ARCH})$|^-mtune=({_X86_TUNE_ONLY})$|^-m(no-)?({_X86_ISA})$|"
    r"^-mcmodel=(small|medium)$|^-m(no-)?(tls-direct-seg-refs|force-indirect-call)$|"
    r"^-mprefer-vector-width=(128|256|512|none)$|^-mfpmath=(sse|387|sse,387|both)$|"
    r"^-m(no-)?(red-zone|omit-leaf-frame-pointer|stackrealign|vzeroupper|recip|"
    r"fentry|align-stringops|inline-all-stringops|inline-stringops-dynamically)$|"
    r"^-mstringop-strategy=\S+$|^-mbranch-cost=\d+$|^-mlarge-data-threshold=\d+$"
)

# -f options that break the pipeline (write extra files, need inputs we do
# not have, stop before code generation, or only change diagnostics).
_F_DENY = re.compile(
    r"^-f(no-)?(dump|plugin|profile-use|profile-generate|profile-arcs|auto-profile|test-coverage|"
    r"coverage|self-test|compare-debug|diagnostics|message-length|max-errors|opt-info|"
    r"save-optimization-record|syntax-only|preprocessed|directives-only|working-directory|"
    r"pch|modules|module-|callgraph-info|stack-usage|record-gcc-switches|verbose-asm|"
    r"debug-prefix-map|file-prefix-map|macro-prefix-map|lto-report|report-bug|"
    r"abi-version|show-column|ellide|elide|ext-numeric-literals|"
    r"live-patching|instrument-functions|split-stack|"
    r"concepts-ts|contracts|coroutines-ts|gnu-tm)"
)
_F_FORM = re.compile(r"^-f[a-z0-9][a-z0-9_+.,=-]*$")
_G_ALLOW = re.compile(r"^-g([0-3]|dwarf(-[2-5])?|column-info|no-column-info|"
                      r"statement-frontiers|variable-location-views|inline-points|"
                      r"strict-dwarf|no-strict-dwarf|record-gcc-switches|no-record-gcc-switches)?$")
_D_ALLOW = re.compile(r"^-[DU][A-Za-z_][A-Za-z0-9_]*(=[^\s'\"\\]*)?$")
_SANITIZE = re.compile(r"^-fsanitize=(undefined|address|signed-integer-overflow|bounds|"
                       r"bounds-strict|shift|alignment|null|return|vla-bound|float-cast-overflow|"
                       r"pointer-overflow|builtin|object-size|unreachable|integer-divide-by-zero)"
                       r"(,(undefined|address|signed-integer-overflow|bounds|shift|alignment|null|"
                       r"return|vla-bound|float-cast-overflow|pointer-overflow|builtin|object-size|"
                       r"unreachable|integer-divide-by-zero))*$")


def is_allowed(opt: str) -> bool:
    if _LEVEL.match(opt) or _D_ALLOW.match(opt) or _G_ALLOW.match(opt) or _M_ALLOW.match(opt):
        return True
    if opt.startswith("-fsanitize"):
        return bool(_SANITIZE.match(opt))
    return bool(_F_FORM.match(opt)) and not _F_DENY.match(opt)


def sanitize(opts: Sequence[str]) -> Tuple[List[str], List[str]]:
    """(kept, dropped): allowlisted options, de-duplicated, order kept."""
    kept, dropped, seen = [], [], set()
    for o in opts:
        if o in seen:
            continue
        seen.add(o)
        (kept if is_allowed(o) else dropped).append(o)
    return kept, dropped


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #


def level_of(opts: Sequence[str]) -> Optional[str]:
    """The effective -O level in opts (the last one wins, as in GCC)."""
    levels = [o for o in opts if _LEVEL.match(o)]
    return levels[-1] if levels else None


def without_level(opts: Sequence[str]) -> List[str]:
    return [o for o in opts if not _LEVEL.match(o)]


# Options that legitimately change floating-point results, so outputs at
# different -O levels may differ without any compiler bug.
_VALUE_CHANGING = re.compile(
    r"^-Ofast$|^-f(fast-math|unsafe-math-optimizations|associative-math|reciprocal-math|"
    r"finite-math-only|no-signed-zeros|cx-limited-range|cx-fortran-rules|"
    r"excess-precision=fast|fp-contract=fast|no-rounding-math|no-trapping-math)$|"
    r"^-mrecip|^-mfpmath=387"
)


def value_changing(opts: Sequence[str]) -> bool:
    return any(_VALUE_CHANGING.match(o) for o in opts)


@functools.lru_cache(maxsize=1)
def host_cpu_flags() -> frozenset:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("flags"):
                    return frozenset(line.split(":", 1)[1].split())
    except OSError:
        pass
    return frozenset()


# GCC predefined macro -> /proc/cpuinfo flag
_ISA_MACROS = {
    "__AVX512F__": "avx512f", "__AVX512BW__": "avx512bw", "__AVX512VL__": "avx512vl",
    "__AVX512DQ__": "avx512dq", "__AVX512CD__": "avx512cd", "__AVX512VNNI__": "avx512_vnni",
    "__AVX512BF16__": "avx512_bf16", "__AVX512FP16__": "avx512_fp16", "__AVX512IFMA__": "avx512ifma",
    "__AVX512VBMI__": "avx512vbmi", "__AVX512VBMI2__": "avx512_vbmi2", "__AVX512BITALG__": "avx512_bitalg",
    "__AVX512VPOPCNTDQ__": "avx512_vpopcntdq", "__AVXVNNI__": "avx_vnni",
    "__AVX2__": "avx2", "__AVX__": "avx", "__FMA__": "fma", "__FMA4__": "fma4", "__BMI__": "bmi1",
    "__BMI2__": "bmi2", "__LZCNT__": "abm", "__POPCNT__": "popcnt", "__F16C__": "f16c",
    "__MOVBE__": "movbe", "__AES__": "aes", "__PCLMUL__": "pclmulqdq", "__SHA__": "sha_ni",
    "__ADX__": "adx", "__XOP__": "xop", "__TBM__": "tbm", "__GFNI__": "gfni", "__VAES__": "vaes",
    "__VPCLMULQDQ__": "vpclmulqdq", "__SSE4_1__": "sse4_1", "__SSE4_2__": "sse4_2",
    "__SSSE3__": "ssse3", "__SSE4A__": "sse4a", "__RDRND__": "rdrand", "__RDSEED__": "rdseed",
}


@functools.lru_cache(maxsize=512)
def _isa_needed(cpu_opts: Tuple[str, ...], compiler: str) -> frozenset:
    try:
        r = subprocess.run([compiler, "-x", "c++", "-E", "-dM", *cpu_opts, "-"], input="",
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    defined = set(re.findall(r"#define (__\w+__) 1", r.stdout))
    return frozenset(flag for mac, flag in _ISA_MACROS.items() if mac in defined)


def runs_on_host(opts: Sequence[str], compiler: str = "gcc14-g++") -> bool:
    """Can a binary built with these options execute on this machine?

    Asks the compiler which ISA extensions the options enable (its predefined
    macros) and checks each against /proc/cpuinfo -- e.g. -march=skylake-avx512
    needs avx512f, which an AMD Zen 2 host lacks.
    """
    cpu = tuple(o for o in opts if o.startswith("-m"))
    if not cpu:
        return True
    host = host_cpu_flags()
    return all(f in host for f in _isa_needed(cpu, compiler))


def oracle_mode(opts: Sequence[str], compiler: str = "gcc14-g++") -> str:
    """How the differential oracle may treat a program with these options.

    full         -- compile at every level, run, compare outputs (as before)
    crash_only   -- options legitimately change results: compile, no comparison
    compile_only -- binaries cannot run here (CPU features): compile only
    """
    if not runs_on_host(opts, compiler):
        return "compile_only"
    if value_changing(opts):
        return "crash_only"
    return "full"


def main(argv: Optional[Sequence[str]] = None) -> None:
    """`python -m Fuzz4All.huf_saem.options DIR`: print `path<TAB>options` for
    every DIR/*.fuzz, one line each (options allowlisted; empty if none).
    Lets shell scripts (coverage replays) read a whole corpus in one pass."""
    import glob
    import os
    import sys
    for d in (argv if argv is not None else sys.argv[1:]):
        for path in sorted(glob.glob(os.path.join(d, "*.fuzz"))):
            opts, _ = sanitize(read_options(path))
            print(f"{path}\t{' '.join(opts)}")


if __name__ == "__main__":
    main()
