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
model_revision="${MODEL_REVISION:-}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
hf_cache_dir="${HF_CACHE_DIR:-${repo_root}/.cache/huggingface}"
transformers_python="${TRANSFORMERS_PYTHON:-${repo_root}/.venv-vllm-image/bin/python}"

if [[ ! -x "${transformers_python}" ]]; then
    echo "Transformers Python is not available at ${transformers_python}" >&2
    exit 1
fi
if ! "${transformers_python}" -c 'import bitsandbytes, fastapi, torch, transformers, uvicorn' >/dev/null 2>&1; then
    echo "Transformers 8-bit bridge dependencies cannot be imported by ${transformers_python}" >&2
    exit 1
fi

mkdir -p "${hf_cache_dir}"
export CUDA_VISIBLE_DEVICES="${gpu_id}"
export HF_HOME="${hf_cache_dir}"
export HF_HUB_ENABLE_HF_TRANSFER=1
export HF_XET_HIGH_PERFORMANCE=1
export PYTHONUNBUFFERED=1

revision_args=()
if [[ -n "${model_revision}" ]]; then
    revision_args=(--revision "${model_revision}")
fi

exec "${transformers_python}" "${repo_root}/scripts/serving/serve_transformers_8bit.py" \
    --model "${model_repository}" \
    --served-model-name "${served_model_name}" \
    --port "${host_port}" \
    --max-generation-seconds "${MODEL_MAX_GENERATION_SECONDS:-600}" \
    "${revision_args[@]}"
