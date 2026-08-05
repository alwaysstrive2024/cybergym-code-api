#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "Usage: $0 MODEL_REPOSITORY SERVED_MODEL_NAME HOST_PORT" >&2
    exit 2
fi

model_repository="$1"
served_model_name="$2"
host_port="$3"
gpu_id="${GPU_ID:-0}"
container_name="cybergym-vllm-${served_model_name//[^A-Za-z0-9_.-]/-}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
hf_cache_dir="${HF_CACHE_DIR:-${repo_root}/.cache/huggingface}"

if docker container inspect "${container_name}" >/dev/null 2>&1; then
    echo "Refusing to replace existing container: ${container_name}" >&2
    exit 1
fi

mkdir -p "${hf_cache_dir}"

exec docker run --rm \
    --name "${container_name}" \
    --gpus "device=${gpu_id}" \
    --ipc=host \
    --publish "${host_port}:8000" \
    --volume "${hf_cache_dir}:/root/.cache/huggingface" \
    --env HF_HOME=/root/.cache/huggingface \
    --env HF_HUB_ENABLE_HF_TRANSFER=1 \
    --env HF_XET_HIGH_PERFORMANCE=1 \
    vllm/vllm-openai:v0.26.0 \
    "${model_repository}" \
    --served-model-name "${served_model_name}" \
    --host 0.0.0.0 \
    --port 8000 \
    --language-model-only \
    --dtype bfloat16 \
    --quantization bitsandbytes \
    --load-format bitsandbytes \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.88 \
    --max-num-seqs 8 \
    --generation-config vllm \
    --seed 20260727 \
    --trust-remote-code \
    --reasoning-parser qwen3 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_coder \
    --disable-log-requests
