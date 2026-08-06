#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
batch_name="${1:-qwen36-official-full-r3-differential}"
model_repo="Qwen/Qwen3.6-35B-A3B"
model_name="qwen36-35b-a3b-official-8bit"
model_revision="995ad96eacd98c81ed38be0c5b274b04031597b0"
model_port="${QWEN_MODEL_PORT:-18080}"
batch_dir="${repo_root}/.runs/${batch_name}"
tasks_file="${batch_dir}/all_tasks.txt"
failed_file="${batch_dir}/failed_tasks.txt"
# This is also the cache root used by serve_vllm_host_8bit.sh.  The official
# 71GB snapshot is already present here as 26 symlinked safetensor shards.
model_cache="${HF_CACHE_DIR:-${repo_root}/.cache/huggingface}"

[[ "$(basename "${batch_name}")" == "${batch_name}" ]] || { echo "batch name must be one path component" >&2; exit 2; }
mkdir -p "${batch_dir}"

# This batch must never inherit the DeepSeek (or any other model's) validator.
# A resumed r3 uses this same service directory and PoC database; earlier r1/r2
# artifacts remain untouched and are not considered completed r3 tasks.
unset CYBERGYM_SERVER_URL CYBERGYM_SERVER_PORT
export CYBERGYM_SERVER_RUN_DIR="${batch_dir}/validation-server"
export QWEN_DIFFERENTIAL_SUBMIT=1

# The installed cache only contained tokenizer/config files.  Downloading by
# immutable revision makes this restartable and avoids silently changing model
# weights during a multi-day full benchmark.
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
# Large files can redirect away from hf-mirror.com.  Disable every proxy for
# the whole download so a redirected CDN request cannot silently use Clash.
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export NO_PROXY="*"
export no_proxy="*"
export HF_CACHE_DIR="${model_cache}"
export HF_HUB_ENABLE_HF_TRANSFER=1
export HF_XET_HIGH_PERFORMANCE=1

hf download "${model_repo}" --revision "${model_revision}" --cache-dir "${model_cache}/hub" --max-workers 4

# Keep the evaluated population immutable and auditable.  tasks.json is the
# complete local CyberGym manifest, not merely one of the source subsets.
rg -o '"task_id": "[^"]+"' "${repo_root}/cybergym_data/tasks.json" \
    | sed -E 's/"task_id": "([^"]+)"/\1/' >"${tasks_file}"
[[ "$(wc -l <"${tasks_file}")" -eq 1507 ]] || { echo "unexpected full task count" >&2; exit 1; }

# A blank MAX_TOKENS means run_langgraph_eval.py receives no --max-tokens flag.
# vLLM remains bounded by its 32k context window; that is a capacity safeguard,
# not an API output-token cap.
# In Qwen differential mode MAX_STEPS is the total model-call budget: calls
# 1-199 explore, and call 200 is the final submission opportunity.
export MAX_STEPS=200
export MODEL_REQUEST_TIMEOUT_S=1200
export GPU_IDS="${GPU_IDS:-0,1}"
export VLLM_TENSOR_PARALLEL_SIZE=2
export VLLM_QUANTIZATION=""
export VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.88}"
export VLLM_MAX_NUM_SEQS=1
export FAILURE_TASKS_FILE="${failed_file}"

exec bash "${repo_root}/scripts/evaluation/run_source_subset_model.sh" \
    "${model_repo}" "${model_name}" "${model_port}" "${batch_name}" \
    "${batch_name}" "${model_revision}" "${tasks_file}"
