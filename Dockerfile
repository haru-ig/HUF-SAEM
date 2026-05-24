FROM fuzz4all/fuzz4all:v3

# Replace the original Fuzz4All source with HUF-SAEM.
# The base image has Fuzz4All installed as an editable package at /home/Fuzz4All,
# so copying HUF-SAEM over that path is enough — no re-registration needed.
RUN rm -rf /home/Fuzz4All
COPY . /home/Fuzz4All

WORKDIR /home/Fuzz4All

# Install all dependencies (including HUF-SAEM additions).
# Pin httpx<0.28 after the install to keep openai 1.6.1 working — chromadb
# pulls in httpx 0.28+ which removed the 'proxies' kwarg the old SDK uses.
RUN /root/anaconda3/envs/fuzz4all/bin/pip install --no-cache-dir -r requirements.txt && \
    /root/anaconda3/envs/fuzz4all/bin/pip install --no-cache-dir "httpx<0.28"

# Clone LLVM source for Phase 1 source-aware autoprompting.
RUN git clone --depth 1 https://github.com/llvm/llvm-project /home/llvm-project

# Install zstd (required by the Ollama installer), then install Ollama.
RUN apt-get update && apt-get install -y --no-install-recommends zstd && \
    rm -rf /var/lib/apt/lists/* && \
    curl -fsSL https://ollama.com/install.sh | sh

# Pre-pull deepseek-coder-v2 so containers don't need to download it.
# NOTE: deepseek-coder-v2 is ~9 GB — this significantly increases the image size.
RUN /bin/bash -c "ollama serve & until ollama list > /dev/null 2>&1; do sleep 1; done && ollama pull deepseek-coder-v2"

WORKDIR /home/Fuzz4All
