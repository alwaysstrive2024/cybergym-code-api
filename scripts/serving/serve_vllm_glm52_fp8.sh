#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "Usage: $0 MODEL_REPOSITORY SERVED_MODEL_NAME HOST_PORT" >&2
    exit 2
fi

model_repository="$1"
served_model_name="$2"
host_port="$3"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
vllm_python="${VLLM_PYTHON:-${repo_root}/.venv-vllm-image/bin/python}"
hf_cache_dir="${HF_CACHE_DIR:-${repo_root}/.cache/huggingface}"
model_revision="${MODEL_REVISION:-}"
gpu_ids="${CUDA_VISIBLE_DEVICES:-0,1}"

# Each matched FP8 checkpoint is 703.7 GiB before vLLM runtime/KV-cache
# overhead.  Ten 96-GiB GPUs are the safe default; callers may override only
# when they have independently sized a different GPU topology.
minimum_gpu_count="${GLM_MIN_GPU_COUNT:-10}"
minimum_total_vram_mib="${GLM_MIN_TOTAL_VRAM_MIB:-900000}"

if [[ ! -x "${vllm_python}" ]] || ! "${vllm_python}" -c 'import vllm' >/dev/null 2>&1; then
    echo "A GLM-5.2-compatible vLLM Python environment is required: ${vllm_python}" >&2
    exit 1
fi

IFS=',' read -r -a selected_gpus <<<"${gpu_ids}"
gpu_count="${#selected_gpus[@]}"
if (( gpu_count < minimum_gpu_count )); then
    echo "GLM-5.2-FP8 requires at least ${minimum_gpu_count} selected GPUs by default; got ${gpu_count}." >&2
    echo "This guard prevents downloading a 703.7-GiB checkpoint that cannot be served." >&2
    exit 1
fi

total_vram_mib=$(CUDA_VISIBLE_DEVICES="${gpu_ids}" nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits \
    | awk '{sum += $1} END {print sum + 0}')
if (( total_vram_mib < minimum_total_vram_mib )); then
    echo "GLM-5.2-FP8 requires at least ${minimum_total_vram_mib} MiB aggregate VRAM by default; got ${total_vram_mib} MiB." >&2
    exit 1
fi

if [[ -z "${VLLM_TOOL_CALL_PARSER:-}" ]]; then
    echo "Set VLLM_TOOL_CALL_PARSER to a parser verified for this GLM/vLLM version before CyberGym execution." >&2
    exit 2
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

exec "${vllm_python}" -c 'from vllm.entrypoints.cli.main import main; main()' serve "${model_repository}" \
    "${revision_args[@]}" \
    --served-model-name "${served_model_name}" \
    --host 127.0.0.1 \
    --port "${host_port}" \
    --dtype bfloat16 \
    --quantization fp8 \
    --tensor-parallel-size "${gpu_count}" \
    --enable-expert-parallel \
    --max-model-len "${GLM_MAX_MODEL_LEN:-32768}" \
    --gpu-memory-utilization "${GLM_GPU_MEMORY_UTILIZATION:-0.90}" \
    --generation-config vllm \
    --seed 20260728 \
    --trust-remote-code \
    --enable-auto-tool-choice \
    --tool-call-parser "${VLLM_TOOL_CALL_PARSER}"
