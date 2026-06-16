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

Use the **Deep Learning OSS Nvidia Driver AMI GPU PyTorch 2.11 (Amazon Linux 2023)** — it comes with NVIDIA drivers, CUDA, and PyTorch pre-installed at `/opt/pytorch`. **Note:** this AMI does not ship conda; the setup uses the pre-installed `/opt/pytorch` Python 3.13 venv directly. Docker and `nvidia-container-toolkit` are not required. (Docker is optional if you want to sandbox the fuzzer; see the note at the bottom of this section.)

### 1. Clone the repo

```bash
git clone https://github.com/haru-ig/HUF-SAEM.git
cd HUF-SAEM
```

### 2. Activate the pre-installed Python environment

```bash
source /opt/pytorch/bin/activate
```

Add `source /opt/pytorch/bin/activate` to `~/.bashrc` to activate automatically on login.

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
pip install -e .
```

### 4. Install target compilers

```bash
sudo dnf install -y gcc gcc-c++ clang
```

(`gcov` is included with `gcc` on Amazon Linux 2023.)

### 5. Clone GCC source (required for Phase 1)

```bash
git clone --depth 1 https://github.com/gcc-mirror/gcc /home/ec2-user/gcc
```

Set `phase1.source_dir: /home/ec2-user/gcc` in your config to point Phase 1 at the full GCC source tree.

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
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker $USER && newgrp docker

# Install nvidia-container-toolkit
curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
  | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo
sudo dnf install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Verify
docker run --rm --runtime=nvidia --gpus all ubuntu nvidia-smi
```

---

## Running HUF-SAEM

See `how_to_run.md` for the full command reference. Quick start:

```bash
source /opt/pytorch/bin/activate
python Fuzz4All/fuzz.py --config config/cpp_huf_saem_1phase_enabled.yaml main_with_config \
    --folder outputs/huf_saem_outputs \
    --batch_size 5 \
    --model_name ollama/deepseek-coder-v2 \
    --target g++
```
