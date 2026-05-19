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
