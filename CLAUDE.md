# HUF-SAEM — Claude Code Instructions

## Project Overview

HUF-SAEM (Hybrid Universal Fuzzing with Source-Aware Autoprompting and LLM-Synthesized Evolutionary Mutators) is a research compiler fuzzer built on top of [Fuzz4All](https://github.com/fuzz4all/fuzz4all). It uses LLMs to automatically find bugs in compilers (GCC, Clang, LLVM, Rust, deep learning compilers like PyTorch Inductor) by generating and mutating code inputs.

HUF-SAEM extends Fuzz4All with four optional phases:
- **Phase 1** — Source-Aware Autoprompting: reads compiler source code to craft inputs targeting deep optimization passes
- **Phase 2** — Bug-Report-Driven Mutator Synthesis: ingests historical bug reports and auto-generates AST mutation scripts
- **Phase 3** — Cloze-Masked Evolutionary Seed Preservation: prevents semantic invalidity by filling in blanks in known-good seeds, run across parallel evolutionary islands
- **Phase 4** — Constraint-Solving Feedback Loop: uses an LLM to solve branch conditions when fuzzing gets stuck

Key files:
- `config/cpp_huf_saem.yaml` — base config template (all phases disabled)
- `config/cpp_huf_saem_4phases_enabled.yaml` — all 4 phases enabled
- `config/cpp_huf_saem_1phase_enabled.yaml` — Phase 1 only enabled
- `how_to_run.md` — run commands and per-phase configuration guide
- `Fuzz4All/` — core fuzzing engine (inherited from Fuzz4All)

---

## Target Hardware

AWS EC2 **g5.2xlarge** — 1× NVIDIA A10G GPU (24 GB VRAM), 8 vCPUs, 32 GB RAM.

---

## Environment Setup

**If you are reading this on a fresh EC2 instance, follow these steps to set up the environment.**

Use the **AWS Deep Learning AMI (Ubuntu)** — it comes with NVIDIA drivers, CUDA, and conda pre-installed, so Docker and `nvidia-container-toolkit` are not required for this setup. (Docker is optional if you want to sandbox the fuzzer, since it executes LLM-generated code; see the note at the bottom of this section.)

### 1. Clone the repo

```bash
git clone https://github.com/haru-ig/HUF-SAEM.git
cd HUF-SAEM
```

### 2. Create and activate the conda environment

```bash
conda create -n huf-saem python=3.12 -y
conda activate huf-saem
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
pip install -e .
```

### 4. Install target compilers

```bash
sudo apt update
sudo apt install -y gcc g++ clang gcov
```

### 5. Clone LLVM source (required for Phase 1)

```bash
git clone --depth 1 https://github.com/llvm/llvm-project /home/llvm-project
```

Set `phase1.source_dir: /home/llvm-project/llvm/lib/Transforms` in your config to point Phase 1 at it.

### 6. Install Ollama and pull the generation model

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull deepseek-coder-v2   # ~9 GB — this will take a while
```

### 7. Set environment variables

```bash
export OPENAI_API_KEY=sk-...        # required — used by phases 1, 2, 3, 4 (GPT-4o)
export GITHUB_TOKEN=ghp_...         # optional — only needed for Phase 2 GitHub ingestion
```

Add these to `~/.bashrc` to persist across sessions.

### Verify the setup

```bash
python -c "import torch; print(torch.cuda.is_available())"   # should print True
ollama list                                                    # should show deepseek-coder-v2
gcc --version && gcov --version
```

### Optional: Docker sandboxing

The fuzzer executes LLM-generated code directly on the host. If you want process isolation, install Docker and `nvidia-container-toolkit` for GPU passthrough:

```bash
# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

# Install nvidia-container-toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update && sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Verify
docker run --rm --runtime=nvidia --gpus all ubuntu nvidia-smi
```

---

## Running HUF-SAEM

See `how_to_run.md` for the full command reference. Quick start:

```bash
conda activate huf-saem
python Fuzz4All/fuzz.py --config config/cpp_huf_saem_1phase_enabled.yaml main_with_config \
    --folder outputs/huf_saem_outputs \
    --batch_size 5 \
    --model_name ollama/deepseek-coder-v2 \
    --target g++
```
