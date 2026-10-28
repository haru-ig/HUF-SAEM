# How to Run HUF-SAEM

## Original Fuzz4All command

```bash
python Fuzz4All/fuzz.py --config {config_file.yaml} main_with_config \
    --folder outputs/fuzzing_outputs \
    --batch_size {batch_size} \
    --model_name {model_name} \
    --target {target_name}
```

## HUF-SAEM command

The command is identical. The only difference is that you pass a config file that contains a `huf_saem:` block with the phases you want enabled.

```bash
python Fuzz4All/fuzz.py --config config/cpp_huf_saem.yaml main_with_config \
    --folder outputs/huf_saem_outputs \
    --batch_size {batch_size} \
    --model_name {model_name} \
    --target {target_name}
```

A ready-to-use template config is provided at `config/cpp_huf_saem.yaml`.

---

## Enabling phases

Open `config/cpp_huf_saem.yaml` (or a copy of it) and set `enabled: true` under whichever phases you want:

```yaml
huf_saem:
  phase1:
    enabled: true          # Source-Aware Autoprompting
    source_dir: /path/to/llvm/lib/Transforms

  phase2:
    enabled: true          # Bug-Report-Driven Mutator Synthesis
    csv_path: bugs/clang.csv
    mutate_ratio: 0.20

  phase3:
    enabled: true          # Cloze-Masked Evolutionary Seed Preservation
    cloze_threshold: 0.30

  phase4:
    enabled: true          # Constraint-Solving Feedback Loop
    solver_interval: 100
```

Phases not listed (or with `enabled: false`) are skipped — the fuzzer runs exactly as vanilla Fuzz4All.

---

## Prerequisites

Install the additional HUF-SAEM dependencies:

```bash
pip install tree-sitter>=0.21.0 tree-sitter-languages>=1.10.0 \
            chromadb>=0.4.24 sentence-transformers>=2.6.0 PyGithub>=2.1.1
```

Or install everything at once:

```bash
pip install -r requirements.txt
```

Set your OpenAI API key (required for phases 1, 2, 3, and 4 — all use GPT-4o):

```bash
export OPENAI_API_KEY=sk-...
```

For Phase 2 GitHub ingestion (optional):

```bash
export GITHUB_TOKEN=ghp_...
```

For Phase 4 coverage on C/C++ targets, ensure `gcc` and `gcov` are installed:

```bash
gcc --version && gcov --version
```

---

## Example: run all 4 phases on a C++ target

```bash
python Fuzz4All/fuzz.py --config config/cpp_huf_saem.yaml main_with_config \
    --folder outputs/cpp_huf_saem \
    --batch_size 5 \
    --model_name ollama/deepseek-coder-v2 \
    --target cpp
```

## Example: run vanilla Fuzz4All (no HUF-SAEM phases)

Any existing config without a `huf_saem:` block runs exactly as before:

```bash
python Fuzz4All/fuzz.py --config config/full_run/cpp_23.yaml main_with_config \
    --folder outputs/full_run/cpp \
    --batch_size 30 \
    --model_name bigcode/starcoderbase \
    --target cpp
```

---

## Bug oracle (Tier 1)

By default a generated program is judged by a single compile at the default
optimisation level. That can only ever reveal a compiler crash, and only when
the compiler's exit status happens to be unusual — so wrong-code bugs are
invisible and ICEs are filed as ordinary compile errors.

Two things change that:

**Crash detection is always on.** The compiler's own output is scanned for an
internal compiler error, a signal death, or a compile hang, regardless of exit
status (GCC exits 1 on a typical ICE — the same status as a syntax error).
Hits are logged as `has potential error!` in `log.txt`. This only affects how
a result is classified, never what is generated, so runs stay comparable with
earlier ones.

**Differential execution is opt-in** via an `oracle:` block:

```yaml
oracle:
  enabled: true
  mode: differential          # or compile_only for crash detection alone
  compilers: [g++, clang++]
  opt_levels: ["-O0", "-O1", "-O2", "-O3"]
  std: c++23
  sanitize: true              # discard divergences caused by UB in the program
  run_timeout: 5
  bug_dir: null               # defaults to <output_folder>/bugs_found
```

Each program is compiled under every (compiler, -O level) pair, every binary is
run, and stdout + exit status are compared. A divergence is a miscompilation
candidate. Before anything is reported it must survive two gates:

1. **determinism** — the reference binary is run twice; a program that prints
   an address or a timestamp is dropped;
2. **UB** — the program is rebuilt with ASan+UBSan and run; if the sanitizers
   complain, the divergence is the program's fault, not the compiler's.

Findings land in `<bug_dir>/<kind>/<hash>/` as `program.cpp`, `report.txt` and
`report.json`. Kinds: `ice`, `compiler_signal`, `compile_hang`, `wrong_code`,
`hang_divergence`, `reject_divergence`.

Ready-made config:

```bash
python Fuzz4All/fuzz.py --config config/cpp_huf_saem_bughunt_10k.yaml main_with_config \
    --folder outputs/huf_saem_bughunt \
    --batch_size 5 \
    --model_name ollama/deepseek-coder-v2 \
    --target g++
```

The oracle costs roughly 8 compiles and 5 program runs per input, so this arm
generates far fewer inputs per hour than the coverage ablation arms — don't mix
its throughput numbers into the coverage comparison.

### Mining corpora you already have

Every run under `outputs/` was validated with the old compile-only oracle, so
no one has ever looked for a wrong-code bug in those programs. Re-check them
without re-fuzzing:

```bash
python tools/run_oracle.py outputs/huf_saem_1phase_10k_run2 \
    --compilers g++ clang++ --opt-levels -O0 -O1 -O2 -O3 -j 6
```

The run directory is never modified; artefacts go to `--out`
(default `outputs/oracle_findings`). Exit status is 1 when something was found.
