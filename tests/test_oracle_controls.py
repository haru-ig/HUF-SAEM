"""Control programs for the differential oracle.

The oracle is only useful if it does two things at once: report programs whose
behaviour really does depend on the optimisation level, and stay quiet about
programs whose own undefined behaviour explains the difference. Both halves
have failed silently before -- a missing libasan made the UB gate pass
everything, and running with stdin closed turned ordinary `std::cin >> n`
programs into a flood of fake miscompilations -- and in both cases the output
looked plausible. A dead detector and a clean corpus are indistinguishable
from the summary line, so each half is pinned here with a program whose
verdict is known in advance.

Needs GCC 12+ on PATH (gcc14-g++ on the Amazon Linux 2023 host); skipped
otherwise, since the uninitialised-read gate cannot run without it.
"""

from __future__ import annotations

import shutil

import pytest

from Fuzz4All.huf_saem.oracle import DifferentialOracle

GCC12_PLUS = ["gcc14-g++", "g++-14", "g++-13", "g++-12"]


def _compiler() -> str:
    for candidate in GCC12_PLUS:
        if shutil.which(candidate):
            probe = DifferentialOracle(compilers=[candidate])
            if probe.compilers and probe.supports_uninit_gate(candidate):
                return candidate
    pytest.skip("no GCC 12+ on PATH; the uninitialised-read gate cannot run")


@pytest.fixture(scope="module")
def oracle():
    return DifferentialOracle(compilers=[_compiler()], bug_dir=None)


def _check(oracle, tmp_path, name, source):
    path = tmp_path / f"{name}.fuzz"
    path.write_text(source)
    return oracle.check(str(path))


# -- sensitivity: real divergence must still be reported ---------------------

def test_optimisation_dependent_output_is_reported(oracle, tmp_path):
    """A defined program whose output depends on -O level is a wrong_code hit.

    __builtin_constant_p is the honest stand-in for a miscompilation: it
    folds to 0 unoptimised and 1 once the optimiser can see through x, with
    no undefined behaviour anywhere. That makes it the one control that
    proves the UB gates did not blind the oracle -- it is deterministic,
    sanitizer-clean and insensitive to -ftrivial-auto-var-init, so nothing
    legitimately suppresses it.
    """
    verdict = _check(oracle, tmp_path, "constant_p", """
#include <cstdio>
int main() {
    int x = 42;
    printf("constant_p=%d\\n", __builtin_constant_p(x * 2));
    return 0;
}
""")
    assert verdict.kind == "wrong_code", verdict.summary
    assert verdict.is_bug


def test_hang_only_when_optimised_is_reported(oracle, tmp_path):
    verdict = _check(oracle, tmp_path, "hang_opt", """
#include <cstdio>
int main() {
#ifdef __OPTIMIZE__
    while (1) { }
#endif
    printf("done\\n");
    return 0;
}
""")
    assert verdict.kind == "hang_divergence", verdict.summary


def test_compiler_crash_is_reported_as_ice(oracle, tmp_path):
    """Deep nesting overflows cc1plus's stack under the oracle's own flags.

    GCC exits 1 on an ICE, the same status as a syntax error, so this pins
    the output-scanning classifier rather than the exit code.
    """
    source = "int main() { return " + "(" * 60000 + "1" + ")" * 60000 + "; }\n"
    verdict = _check(oracle, tmp_path, "deep_nesting", source)
    assert verdict.kind in {"ice", "compiler_signal"}, verdict.summary


# -- specificity: the program's own UB must not be reported ------------------

def test_failed_input_read_is_not_a_miscompilation(oracle, tmp_path):
    """The exact false positive that made the first full run worthless.

    With stdin closed the extraction fails, n keeps whatever the stack held,
    and each -O level prints something different -- 50 of these were filed as
    wrong_code before the oracle was given a real input stream. The `n <= 0`
    guard matters: it is what let this shape slip past the
    -ftrivial-auto-var-init gate, because zero and pattern both land on the
    error branch while the uninstrumented build finds a positive value.
    """
    verdict = _check(oracle, tmp_path, "reads_stdin", """
#include <iostream>
#include <vector>
#include <algorithm>
int main() {
    int n;
    std::cout << "Enter the number of elements: ";
    std::cin >> n;
    if (n <= 0) {
        std::cerr << "Invalid number of elements." << std::endl;
        return 1;
    }
    std::vector<int> numbers(n);
    for (int i = 0; i < n; ++i) std::cin >> numbers[i];
    std::cout << *std::max_element(numbers.begin(), numbers.end()) << "\\n";
    return 0;
}
""")
    assert not verdict.is_bug, f"input-dependent program reported as {verdict.kind}"


def test_address_leak_is_not_a_miscompilation(oracle, tmp_path):
    """A printed pointer value differs by -O level because the layout moved.

    The determinism gate is meant to drop these, but it compares two runs of
    the same binary -- which only varies if the address does. gcc14-g++ links
    non-PIE by default on this host, pinning the literal at link time, so the
    gate passed and the program was filed as wrong_code. Building PIE lets
    ASLR move it between runs, which is what the gate is looking for.
    """
    verdict = _check(oracle, tmp_path, "address_leak", """
#include <cstdio>
int main() {
    const char* s = "Hello, World!";
    printf("%d\\n", (int)(long long)s);
    return 0;
}
""")
    assert not verdict.is_bug, f"address leak reported as {verdict.kind}"


def test_well_defined_program_is_safe(oracle, tmp_path):
    verdict = _check(oracle, tmp_path, "plain", """
#include <cstdio>
int main() {
    long s = 0;
    for (int i = 0; i < 1000; ++i) s += i * i;
    printf("%ld\\n", s);
    return 0;
}
""")
    assert verdict.kind == "safe", verdict.summary


# -- the gates themselves ----------------------------------------------------

def test_uninit_gate_is_actually_available(oracle, tmp_path):
    """Guards the failure mode where a gate silently stops running.

    supports_uninit_gate() returning False is not an error -- it is how an
    older compiler is handled -- so nothing downstream fails when the gate
    disappears. The whole corpus just goes quiet.
    """
    assert oracle.supports_uninit_gate(oracle.compilers[0])


def test_stdin_is_fed_by_default(oracle, tmp_path):
    """Pins the fix itself: programs must not run with stdin closed."""
    assert oracle.stdin_data, "oracle would run programs with stdin closed"
    verdict = _check(oracle, tmp_path, "echo_stdin", """
#include <iostream>
int main() {
    int n = -1;
    std::cin >> n;
    std::cout << (std::cin.fail() ? "no input" : "got input") << "\\n";
    return 0;
}
""")
    assert verdict.kind == "safe", verdict.summary
