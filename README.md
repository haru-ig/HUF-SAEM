# HUF-SAEM: Hybrid Universal Fuzzing with Source-Aware Autoprompting and LLM-Synthesized Evolutionary Mutators

HUF-SAEM is a compiler fuzzer built on top of [Fuzz4All](https://github.com/fuzz4all/fuzz4all) that combines black-box universal generation with white-box analytical depth. It extends Fuzz4All's dual-LLM autoprompting loop with four independently opt-in phases that address the core limitations of pure generative fuzzing: shallow optimization coverage, low throughput, and semantic invalidity in strict languages.

## Architecture

HUF-SAEM adds four phases on top of Fuzz4All's core fuzzing loop. Each phase is independently enabled via config:

**Phase 1 — Source-Aware Contextual Autoprompting**
Scans the compiler's own source code (e.g. `llvm/lib/Transforms`) using tree-sitter, extracts deeply nested conditional logic, and uses GPT-4o to reverse-engineer the input conditions that trigger specific optimization passes. The resulting constraints are injected directly into Fuzz4All's autoprompting matrix, turning generic documentation-driven prompts into targeted, source-aware instructions.

**Phase 2 — Bug-Report-Driven Metamorphic Mutator Synthesis**
Ingests closed bug reports from the target compiler's GitHub issue tracker or local CSV files. A GPT-4o agent analyzes each bug's triggering code and synthesizes a standalone Python AST mutation script using tree-sitter. These mutators run deterministically at CPU speed, giving the fuzzer high-throughput mutation without an LLM call on every iteration.

**Phase 3 — Cloze-Masked Evolutionary Seed Preservation**
Maintains a ChromaDB vector store of valid seed programs. When the compilation success rate drops below a configurable threshold (e.g. 30% for Rust's borrow checker), the system switches to cloze-fill mode: it masks portions of known-valid seeds and prompts the LLM to fill in the blanks. Multiple evolutionary islands with distinct biases (e.g. memory allocation, concurrency) run in parallel to prevent generative collapse.

**Phase 4 — Active Constraint-Solving Feedback Loop**
Instruments the target with `gcov` and monitors branch coverage. When a branch is repeatedly reached but never traversed, a GPT-4o agent analyzes the blocking condition and generates an input specifically crafted to satisfy it, breaking through local optima that random generation cannot escape.

## Setup

HUF-SAEM is designed to run on an AWS EC2 **g5.2xlarge** (NVIDIA A10G, 24 GB VRAM) using the AWS Deep Learning AMI. See `CLAUDE.md` for complete step-by-step environment setup instructions.

### Quick install

```bash
git clone https://github.com/haru-ig/HUF-SAEM.git
cd HUF-SAEM
conda create -n huf-saem python=3.12 -y
conda activate huf-saem
pip install -r requirements.txt
pip install -e .
```

### Install target compilers

```bash
sudo apt install -y gcc g++ clang gcov
```

### Install Ollama and pull the generation model

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull deepseek-coder-v2
```

### Environment variables

```bash
export OPENAI_API_KEY=sk-...       # required for all phases (GPT-4o)
export GITHUB_TOKEN=ghp_...        # optional — Phase 2 GitHub ingestion only
```

## Running

```bash
conda activate huf-saem
python Fuzz4All/fuzz.py --config config/cpp_huf_saem_4phases_enabled.yaml main_with_config \
    --folder outputs/huf_saem_cpp \
    --batch_size 5 \
    --model_name ollama/deepseek-coder-v2 \
    --target g++
```

See `how_to_run.md` for the full command reference and per-phase configuration guide.

## Configuration

Three ready-to-use config files are provided:

| Config | Description |
|---|---|
| `config/cpp_huf_saem.yaml` | Base template — all phases disabled (vanilla Fuzz4All behaviour) |
| `config/cpp_huf_saem_1phase_enabled.yaml` | Phase 1 only enabled |
| `config/cpp_huf_saem_4phases_enabled.yaml` | All 4 phases enabled |

Each phase is independently toggled with `enabled: true/false` in the `huf_saem:` block. Phases with `enabled: false` are skipped entirely and the fuzzer behaves as vanilla Fuzz4All.

To create a custom config, copy `config/cpp_huf_saem.yaml` and enable the phases you need:

```yaml
huf_saem:
  phase1:
    enabled: true
    source_dir: /home/llvm-project/llvm/lib/Transforms

  phase2:
    enabled: true
    github_repo: llvm/llvm-project
    mutate_ratio: 0.20

  phase3:
    enabled: true
    cloze_threshold: 0.30

  phase4:
    enabled: true
    solver_interval: 100
```

## Based on

HUF-SAEM is built on top of [Fuzz4All](https://github.com/fuzz4all/fuzz4all), the universal fuzzer presented at ICSE 2024:

```bibtex
@inproceedings{fuzz4all,
  title = {Fuzz4All: Universal Fuzzing with Large Language Models},
  author = {Xia, Chunqiu Steven and Paltenghi, Matteo and Tian, Jia Le and Pradel, Michael and Zhang, Lingming},
  booktitle = {Proceedings of the 46th International Conference on Software Engineering},
  series = {ICSE '24},
  year = {2024},
}
```
