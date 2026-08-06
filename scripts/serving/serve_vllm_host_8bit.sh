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
gpu_ids="${GPU_IDS:-${gpu_id}}"
model_revision="${MODEL_REVISION:-}"
gpu_memory_utilization="${VLLM_GPU_MEMORY_UTILIZATION:-0.78}"
max_num_seqs="${VLLM_MAX_NUM_SEQS:-1}"
max_model_len="${VLLM_MAX_MODEL_LEN:-32768}"
tensor_parallel_size="${VLLM_TENSOR_PARALLEL_SIZE:-1}"
# An explicitly empty value selects native checkpoint loading.  Keep the
# legacy bitsandbytes default only when the variable is truly unset.
quantization="${VLLM_QUANTIZATION-bitsandbytes}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
hf_cache_dir="${HF_CACHE_DIR:-${repo_root}/.cache/huggingface}"
vllm_python="${VLLM_PYTHON:-${repo_root}/.venv-vllm-image/bin/python}"

if [[ ! -x "${vllm_python}" ]]; then
    echo "vLLM Python is not available at ${vllm_python}" >&2
    exit 1
fi

if ! "${vllm_python}" -c 'import vllm' >/dev/null 2>&1; then
    echo "vLLM cannot be imported by ${vllm_python}" >&2
    exit 1
fi

mkdir -p "${hf_cache_dir}"

export CUDA_VISIBLE_DEVICES="${gpu_ids}"
export HF_HOME="${hf_cache_dir}"
export HF_HUB_ENABLE_HF_TRANSFER=1
export HF_XET_HIGH_PERFORMANCE=1
export VLLM_ENGINE_READY_TIMEOUT_S="${VLLM_ENGINE_READY_TIMEOUT_S:-86400}"

revision_args=()
if [[ -n "${model_revision}" ]]; then
    revision_args=(--revision "${model_revision}")
fi

quantization_args=()
if [[ -n "${quantization}" ]]; then
    quantization_args=(--quantization "${quantization}" --load-format "${quantization}")
fi

exec "${vllm_python}" -c 'from vllm.entrypoints.cli.main import main; main()' serve "${model_repository}" \
    "${revision_args[@]}" \
    --served-model-name "${served_model_name}" \
    --host 127.0.0.1 \
    --port "${host_port}" \
    --language-model-only \
    --dtype bfloat16 \
    "${quantization_args[@]}" \
    --max-model-len "${max_model_len}" \
    --gpu-memory-utilization "${gpu_memory_utilization}" \
    --max-num-seqs "${max_num_seqs}" \
    --tensor-parallel-size "${tensor_parallel_size}" \
    --generation-config vllm \
    --seed 20260727 \
    --trust-remote-code \
    --reasoning-parser qwen3 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_coder
