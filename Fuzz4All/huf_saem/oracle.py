"""Tier-1 bug oracle for C/C++ targets.

Two independent detectors, both opt-in through an ``oracle:`` block in the YAML
config so that existing ablation arms keep their exact previous behaviour:

  1. **Crash detection** -- recognises an internal compiler error, a compiler
     killed by a signal, or a compile hang from the compiler's own output,
     regardless of the exit status it reports. GCC exits 1 on a typical ICE,
     the same status it uses for an ordinary syntax error, so an
     exit-code-only oracle files real bugs away with the noise.

  2. **Differential execution** -- compiles the same program under several
     (compiler, -O level) configurations, runs each binary, and compares
     stdout + exit status. A divergence is a wrong-code (miscompilation)
     candidate. Candidates are gated behind a determinism re-run and a
     sanitizer pass, so undefined behaviour in the generated program is not
     reported as a compiler bug.

The oracle never modifies the program it is given and writes its artefacts
only under ``bug_dir``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Crash signatures
# --------------------------------------------------------------------------- #

# Signatures of a compiler that has crashed on us, as opposed to a compiler
# that is correctly rejecting a bad program. Matched against combined
# stdout+stderr, case-insensitively.
_ICE_PATTERNS: Sequence[Tuple[str, re.Pattern]] = (
    ("ice", re.compile(r"internal compiler error", re.I)),
    ("ice", re.compile(r"please submit a full bug report", re.I)),
    ("ice", re.compile(r"please submit a bug report to", re.I)),      # clang
    ("ice", re.compile(r"^\s*Stack dump:", re.I | re.M)),             # clang
    ("ice", re.compile(r"Assertion .* failed\.", re.I)),
    ("ice", re.compile(r"unable to execute command:\s*(Segmentation|Bus|Abort|Killed)", re.I)),
    ("ice", re.compile(r"(cc1plus|cc1|clang|clang\+\+|lto1):.*\b(Segmentation fault|Bus error|Killed)", re.I)),
)

# Things that look alarming but are the compiler behaving correctly.
_ICE_EXCLUSIONS: Sequence[re.Pattern] = (
    re.compile(r"confused by earlier errors, bailing out", re.I),
    re.compile(r"compilation terminated due to -fmax-errors", re.I),
    # A static_assert the generated program wrote itself.
    re.compile(r"error: static assertion failed", re.I),
)


def classify_compiler_output(returncode: int, output: str) -> Optional[str]:
    """Return a crash kind for compiler *output*, or None if it looks normal.

    *returncode* is used only as a secondary signal: a compiler that died from
    a signal (negative status from :mod:`subprocess`, or 128+N from a shell)
    has crashed even when it printed nothing useful.
    """
    text = output or ""
    if not any(p.search(text) for p in _ICE_EXCLUSIONS):
        for kind, pattern in _ICE_PATTERNS:
            if pattern.search(text):
                return kind
    if returncode < 0 or returncode in (134, 136, 139):  # SIGABRT / SIGFPE / SIGSEGV
        return "compiler_signal"
    return None


def ice_signature(output: str) -> str:
    """Stable dedup key for an ICE: the message line with paths/numbers erased."""
    for line in (output or "").splitlines():
        if re.search(r"internal compiler error|Assertion .* failed|error: unable to execute", line, re.I):
            line = re.sub(r"^.*?(internal compiler error)", r"\1", line, flags=re.I)
            line = re.sub(r"\b\d+\b", "N", line)
            line = re.sub(r"\S+/", "", line)
            return line.strip()[:200]
    return hashlib.sha1((output or "").encode("utf-8", "replace")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Subprocess helpers
# --------------------------------------------------------------------------- #


@dataclass
class _Proc:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    elapsed: float

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def _rlimits(mem_mb: Optional[int], cpu_s: int, fsize_mb: int = 64):
    """Build a preexec_fn capping address space, CPU time and file size.

    Generated programs are executed, so a runaway allocation or an endless
    output loop must not take the host down with it. Pass ``mem_mb=None`` to
    leave the address space uncapped (required for ASan, see
    :meth:`DifferentialOracle._sanitizer_report`).
    """
    limits = [
        (resource.RLIMIT_CPU, cpu_s),
        (resource.RLIMIT_FSIZE, fsize_mb * 1024 * 1024),
        (resource.RLIMIT_CORE, 0),
    ]
    if mem_mb is not None:
        limits.append((resource.RLIMIT_AS, mem_mb * 1024 * 1024))

    def _apply() -> None:
        for what, limit in limits:
            try:
                resource.setrlimit(what, (limit, limit))
            except (ValueError, OSError):
                pass

    return _apply


def _run(
    cmd: Sequence[str],
    timeout: float,
    cwd: Optional[str] = None,
    preexec=None,
    env: Optional[Dict[str, str]] = None,
) -> _Proc:
    """Run *cmd*, killing the whole process group if it overruns *timeout*."""
    started = time.time()
    try:
        proc = subprocess.Popen(
            list(cmd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,
            preexec_fn=preexec,
            env=env,
        )
    except OSError as exc:
        return _Proc(127, "", str(exc), False, time.time() - started)

    try:
        out, err = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()
        out, err = proc.communicate()
        timed_out = True

    return _Proc(
        returncode=proc.returncode if proc.returncode is not None else -9,
        stdout=(out or b"").decode("utf-8", "replace"),
        stderr=(err or b"").decode("utf-8", "replace"),
        timed_out=timed_out,
        elapsed=time.time() - started,
    )


def _hash_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def _program_hash(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return _hash_text(f.read())
    except OSError:
        return _hash_text(path)


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #

# Kinds that are worth a human's attention.
BUG_KINDS = frozenset(
    {"ice", "compiler_signal", "compile_hang", "wrong_code", "hang_divergence", "reject_divergence"}
)


@dataclass
class Verdict:
    kind: str
    summary: str = ""
    detail: str = ""
    configs: List[str] = field(default_factory=list)
    signature: str = ""
    saved_to: Optional[str] = None

    @property
    def is_bug(self) -> bool:
        return self.kind in BUG_KINDS

    def __str__(self) -> str:
        head = f"[{self.kind}] {self.summary}"
        return f"{head}\n{self.detail}" if self.detail else head


SAFE = Verdict("safe", "no divergence")


# --------------------------------------------------------------------------- #
# The oracle
# --------------------------------------------------------------------------- #


@dataclass
class _Config:
    compiler: str
    opt: str

    @property
    def name(self) -> str:
        return f"{os.path.basename(self.compiler)} {self.opt}"


class DifferentialOracle:
    """Multi-configuration compile-and-run oracle for C/C++ programs."""

    def __init__(
        self,
        compilers: Sequence[str] = ("g++",),
        opt_levels: Sequence[str] = ("-O0", "-O1", "-O2", "-O3"),
        std: str = "c++23",
        extra_flags: Sequence[str] = ("-w",),
        compile_timeout: float = 30.0,
        run_timeout: float = 5.0,
        hang_threshold: float = 20.0,
        sanitize: bool = True,
        sanitize_timeout: float = 20.0,
        cross_compiler_reject_is_bug: bool = False,
        mem_limit_mb: int = 2048,
        bug_dir: Optional[str] = None,
        jobs: int = 4,
        language: str = "cpp",
    ) -> None:
        self.compilers = [c for c in compilers if shutil.which(c)]
        self.opt_levels = list(opt_levels)
        self.std = std
        self.extra_flags = list(extra_flags)
        self.compile_timeout = compile_timeout
        self.run_timeout = run_timeout
        self.hang_threshold = hang_threshold
        self.sanitize = sanitize
        self.sanitize_timeout = sanitize_timeout
        self.cross_compiler_reject_is_bug = cross_compiler_reject_is_bug
        self.mem_limit_mb = mem_limit_mb
        self.bug_dir = bug_dir
        self.jobs = max(1, jobs)
        self.language = language
        self._seen_signatures: set = set()
        self.stats: Dict[str, int] = {}

        self.configs = [
            _Config(c, o) for c in self.compilers for o in self.opt_levels
        ]

    @property
    def available(self) -> bool:
        return len(self.configs) >= 1

    # -- compile / run primitives ------------------------------------------

    def _compile(self, source: str, cfg: _Config, out_path: str, extra: Sequence[str] = ()) -> _Proc:
        cmd = [
            cfg.compiler,
            "-x",
            "c++" if self.language == "cpp" else "c",
            f"-std={self.std}",
            cfg.opt,
            *self.extra_flags,
            *extra,
            source,
            "-o",
            out_path,
        ]
        return _run(cmd, timeout=self.compile_timeout)

    def _execute(self, exe: str, cwd: str) -> _Proc:
        return _run(
            [exe],
            timeout=self.run_timeout,
            cwd=cwd,
            preexec=_rlimits(self.mem_limit_mb, int(self.run_timeout) + 1),
        )

    @staticmethod
    def _fingerprint(proc: _Proc) -> str:
        """What we compare across configurations: exit status + stdout."""
        if proc.timed_out:
            return "<TIMEOUT>"
        return f"rc={proc.returncode}\n{proc.stdout.rstrip()}"

    # -- main entry point ---------------------------------------------------

    def check(self, source_file: str) -> Verdict:
        """Run *source_file* through every configuration and judge the result."""
        if not self.available:
            return SAFE

        workdir = tempfile.mkdtemp(prefix="huf_oracle_")
        try:
            verdict = self._check_in(source_file, workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        self.stats[verdict.kind] = self.stats.get(verdict.kind, 0) + 1
        if verdict.is_bug:
            verdict.saved_to = self._save(source_file, verdict)
        return verdict

    def _check_in(self, source_file: str, workdir: str) -> Verdict:
        # ---- 1. compile under every configuration (in parallel) -----------
        def compile_one(idx_cfg):
            idx, cfg = idx_cfg
            exe = os.path.join(workdir, f"a{idx}.out")
            return cfg, exe, self._compile(source_file, cfg, exe)

        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            compiled = list(pool.map(compile_one, enumerate(self.configs)))

        # ---- 2. compiler crashes and hangs --------------------------------
        for cfg, _exe, proc in compiled:
            # Order matters: our own timeout kill lands as SIGKILL, which the
            # crash classifier would otherwise read as a compiler signal death.
            if proc.timed_out:
                return Verdict(
                    kind="compile_hang",
                    summary=f"{cfg.name} did not finish within {self.compile_timeout:g}s",
                    detail=proc.output.strip()[:2000],
                    configs=[cfg.name],
                    signature=f"hang:{cfg.name}:{_program_hash(source_file)}",
                )
            crash = classify_compiler_output(proc.returncode, proc.output)
            if crash:
                return Verdict(
                    kind=crash,
                    summary=f"{cfg.name} crashed while compiling",
                    detail=proc.output.strip()[:4000],
                    configs=[cfg.name],
                    signature=ice_signature(proc.output),
                )

        for cfg, _exe, proc in compiled:
            if proc.returncode == 0 and proc.elapsed >= self.hang_threshold:
                return Verdict(
                    kind="compile_hang",
                    summary=f"{cfg.name} took {proc.elapsed:.1f}s to compile",
                    detail="(slow-compile candidate; still produced an object)",
                    configs=[cfg.name],
                    signature=f"slow:{cfg.name}:{_program_hash(source_file)}",
                )

        # ---- 3. acceptance divergence -------------------------------------
        ok = [(cfg, exe) for cfg, exe, proc in compiled if proc.returncode == 0]
        rejected = [(cfg, proc) for cfg, _exe, proc in compiled if proc.returncode != 0]

        if not ok:
            return Verdict("invalid", "rejected by every configuration",
                           rejected[0][1].output.strip()[:2000] if rejected else "")

        if rejected:
            # The same compiler accepting at one -O level and rejecting at
            # another is a genuine signal; two different compilers disagreeing
            # usually just means the program leans on an extension.
            per_compiler = {}
            for cfg, _exe, proc in compiled:
                per_compiler.setdefault(cfg.compiler, []).append((cfg, proc))
            for compiler, entries in per_compiler.items():
                codes = {cfg.opt: proc.returncode for cfg, proc in entries}
                if len(set(bool(c) for c in codes.values())) > 1:
                    bad = [cfg.name for cfg, proc in entries if proc.returncode != 0]
                    return Verdict(
                        kind="reject_divergence",
                        summary=f"{os.path.basename(compiler)} accepts at some -O levels but rejects at {', '.join(bad)}",
                        detail="\n".join(
                            f"--- {cfg.name} (rc={proc.returncode}) ---\n{proc.output.strip()[:800]}"
                            for cfg, proc in entries
                        ),
                        configs=[cfg.name for cfg, _ in entries],
                        signature=f"reject:{compiler}:{','.join(sorted(bad))}",
                    )
            if self.cross_compiler_reject_is_bug:
                return Verdict(
                    kind="reject_divergence",
                    summary="compilers disagree on whether the program is valid",
                    detail="\n".join(
                        f"--- {cfg.name} (rc={proc.returncode}) ---\n{proc.output.strip()[:800]}"
                        for cfg, proc in rejected
                    ),
                    configs=[cfg.name for cfg, _ in rejected],
                    signature="reject:cross",
                )
            # Otherwise drop the rejecting configs and compare the rest.

        if len(ok) < 2:
            return SAFE

        # ---- 4. run every binary ------------------------------------------
        ref_cfg, ref_exe = ok[0]
        ref_proc = self._execute(ref_exe, workdir)
        if ref_proc.timed_out:
            # The program does not terminate even unoptimised, so it has no
            # reference behaviour to compare against. Bail out before paying
            # run_timeout for every remaining configuration.
            return Verdict("nonterminating", f"{ref_cfg.name} binary did not terminate")

        runs: List[Tuple[_Config, _Proc]] = [(ref_cfg, ref_proc)]
        runs += [(cfg, self._execute(exe, workdir)) for cfg, exe in ok[1:]]

        # ---- 5. determinism gate ------------------------------------------
        second = self._execute(ref_exe, workdir)
        if self._fingerprint(second) != self._fingerprint(ref_proc):
            return Verdict("nondeterministic",
                           f"{ref_cfg.name} is not reproducible across two runs")

        # ---- 6. compare ----------------------------------------------------
        groups: Dict[str, List[str]] = {}
        for cfg, proc in runs:
            groups.setdefault(self._fingerprint(proc), []).append(cfg.name)
        if len(groups) == 1:
            return SAFE

        timed_out = [cfg.name for cfg, proc in runs if proc.timed_out]
        kind = "hang_divergence" if timed_out else "wrong_code"

        # ---- 7. UB gate ----------------------------------------------------
        if self.sanitize:
            ub = self._sanitizer_report(ref_cfg.compiler, source_file, workdir)
            if ub is not None:
                return Verdict("ub", "divergence explained by UB in the program",
                               ub[:2000])

        detail_lines = []
        for fingerprint, names in groups.items():
            shown = fingerprint if len(fingerprint) <= 1500 else fingerprint[:1500] + "\n...[truncated]"
            detail_lines.append(f"--- {', '.join(names)} ---\n{shown}")
        summary = (
            f"{len(groups)} distinct behaviours across {len(runs)} configurations"
            + (f"; timed out under {', '.join(timed_out)}" if timed_out else "")
        )
        return Verdict(
            kind=kind,
            summary=summary,
            detail="\n".join(detail_lines),
            configs=[name for names in groups.values() for name in names],
            # Keyed on the program itself: the summary only carries counts, so
            # hashing it made unrelated divergences collide and _save() dropped
            # every one after the first.
            signature=f"{kind}:{_program_hash(source_file)}",
        )

    # -- UB gate ------------------------------------------------------------

    def _sanitizer_report(self, compiler: str, source: str, workdir: str) -> Optional[str]:
        """Return the sanitizer's complaint, or None if the program looks clean.

        A divergence in a program with undefined behaviour says nothing about
        the compiler, so anything the sanitizers flag is discarded.
        """
        exe = os.path.join(workdir, "san.out")
        cfg = _Config(compiler, "-O1")
        proc = self._compile(
            source,
            cfg,
            exe,
            extra=["-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"],
        )
        if proc.returncode != 0 or proc.timed_out:
            return None  # sanitizers unavailable here; fall through to reporting
        # No address-space cap here: ASan reserves terabytes of virtual memory
        # for its shadow at startup and aborts under RLIMIT_AS with a message
        # containing "AddressSanitizer", which the check below would misread
        # as UB -- silently discarding every divergence. ASan's own allocator
        # limit bounds real memory use instead.
        run = _run(
            [exe],
            timeout=self.sanitize_timeout,
            cwd=workdir,
            preexec=_rlimits(None, int(self.sanitize_timeout) + 1),
            env={
                **os.environ,
                "ASAN_OPTIONS": f"hard_rss_limit_mb={max(self.mem_limit_mb, 8192)}:detect_leaks=0",
            },
        )
        text = run.output
        if re.search(r"runtime error:|AddressSanitizer|UndefinedBehaviorSanitizer|LeakSanitizer", text):
            return text.strip()
        return None

    # -- artefacts ----------------------------------------------------------

    def _save(self, source_file: str, verdict: Verdict) -> Optional[str]:
        if not self.bug_dir:
            return None
        if verdict.signature and verdict.signature in self._seen_signatures:
            return None
        self._seen_signatures.add(verdict.signature)

        try:
            with open(source_file, "r", encoding="utf-8", errors="replace") as f:
                program = f.read()
        except OSError:
            return None

        uid = _hash_text(program)
        dest = os.path.join(self.bug_dir, verdict.kind, uid)
        try:
            os.makedirs(dest, exist_ok=True)
            with open(os.path.join(dest, "program.cpp"), "w", encoding="utf-8") as f:
                f.write(program)
            with open(os.path.join(dest, "report.txt"), "w", encoding="utf-8") as f:
                f.write(str(verdict) + "\n")
            with open(os.path.join(dest, "report.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "kind": verdict.kind,
                        "summary": verdict.summary,
                        "signature": verdict.signature,
                        "configs": verdict.configs,
                        "source": os.path.abspath(source_file),
                        "std": self.std,
                        "compilers": self.compilers,
                        "opt_levels": self.opt_levels,
                        "detail": verdict.detail,
                    },
                    f,
                    indent=2,
                )
        except OSError:
            return None
        return dest


# --------------------------------------------------------------------------- #
# Construction from config
# --------------------------------------------------------------------------- #


def oracle_from_config(config_dict: Dict, default_compiler: str = "g++") -> Optional[DifferentialOracle]:
    """Build an oracle from the ``oracle:`` block, or None when it is off."""
    cfg = (config_dict or {}).get("oracle") or {}
    if not cfg.get("enabled", False):
        return None
    if cfg.get("mode", "differential") == "compile_only":
        return None  # crash detection alone; it lives in the target

    language = config_dict.get("target", {}).get("language", "cpp")
    output_folder = config_dict.get("fuzzing", {}).get("output_folder", ".")
    bug_dir = cfg.get("bug_dir") or os.path.join(output_folder, "bugs_found")

    compilers = cfg.get("compilers") or [default_compiler]
    oracle = DifferentialOracle(
        compilers=compilers,
        opt_levels=cfg.get("opt_levels") or ["-O0", "-O1", "-O2", "-O3"],
        std=cfg.get("std", "c++23" if language == "cpp" else "c17"),
        extra_flags=cfg.get("extra_flags") or ["-w"],
        compile_timeout=float(cfg.get("compile_timeout", 30)),
        run_timeout=float(cfg.get("run_timeout", 5)),
        hang_threshold=float(cfg.get("hang_threshold", 20)),
        sanitize=bool(cfg.get("sanitize", True)),
        sanitize_timeout=float(cfg.get("sanitize_timeout", 20)),
        cross_compiler_reject_is_bug=bool(cfg.get("cross_compiler_reject_is_bug", False)),
        mem_limit_mb=int(cfg.get("mem_limit_mb", 2048)),
        bug_dir=bug_dir,
        jobs=int(cfg.get("jobs", 4)),
        language=language,
    )
    if not oracle.available:
        missing = [c for c in compilers if not shutil.which(c)]
        raise ValueError(
            f"oracle.enabled is true but none of the configured compilers exist: {missing}"
        )
    return oracle
