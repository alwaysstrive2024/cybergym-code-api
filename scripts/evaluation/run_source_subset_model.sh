#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 6 || $# -gt 7 ]]; then
    echo "Usage: $0 MODEL_REPOSITORY SERVED_MODEL_NAME HOST_PORT BATCH_NAME AGENT_PREFIX MODEL_REVISION [TASKS_FILE]" >&2
    exit 2
fi

model_repository="$1"
served_model_name="$2"
host_port="$3"
batch_name="$4"
agent_prefix="$5"
model_revision="$6"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tasks_file="${7:-${repo_root}/scripts/manifests/source_subset_tasks.txt}"
run_root="${repo_root}/.runs"
batch_dir="${run_root}/${batch_name}"
server_url="${CYBERGYM_SERVER_URL:-}"
server_run_dir="${CYBERGYM_SERVER_RUN_DIR:-${run_root}/server-${batch_name}}"
model_url="http://127.0.0.1:${host_port}"
model_ready_timeout="${MODEL_READY_TIMEOUT_S:-86400}"
max_steps="${MAX_STEPS:-40}"
model_request_timeout="${MODEL_REQUEST_TIMEOUT_S:-660}"
model_launcher="${MODEL_LAUNCHER:-${repo_root}/scripts/serving/serve_vllm_host_8bit.sh}"
model_log="${run_root}/model/${batch_name}.log"
server_log="${server_run_dir}/launcher.log"
data_dir="${CYBERGYM_DATA_DIR:-${repo_root}/cybergym_data/data}"
failure_tasks_file="${FAILURE_TASKS_FILE:-}"
qwen_differential_submit="${QWEN_DIFFERENTIAL_SUBMIT:-0}"
model_pid=""
server_pid=""
server_started=0
batch_exit=0
: "${EVAL_CONCURRENCY:=1}"
: "${EVAL_MEMORY_BUDGET_GIB:=150}"
: "${EVAL_MEMORY_RESERVE_PER_TASK_GIB:=8}"
declare -A worker_tasks=()

# The launcher reads this environment variable, while the evaluator records the
# same immutable value from the positional argument below.
export MODEL_REVISION="${MODEL_REVISION:-${model_revision}}"

if [[ "$(basename "${batch_name}")" != "${batch_name}" ]]; then
    echo "BATCH_NAME must be a single path component" >&2
    exit 2
fi
if [[ ! -f "${tasks_file}" ]]; then
    echo "Task manifest not found: ${tasks_file}" >&2
    exit 2
fi
if [[ "${qwen_differential_submit}" != "0" && "${qwen_differential_submit}" != "1" ]]; then
    echo "QWEN_DIFFERENTIAL_SUBMIT must be 0 or 1" >&2
    exit 2
fi
[[ "${EVAL_CONCURRENCY}" =~ ^[1-9][0-9]*$ ]] || { echo "EVAL_CONCURRENCY must be a positive integer" >&2; exit 2; }
[[ "${EVAL_MEMORY_BUDGET_GIB}" =~ ^[1-9][0-9]*$ ]] || { echo "EVAL_MEMORY_BUDGET_GIB must be a positive integer" >&2; exit 2; }
[[ "${EVAL_MEMORY_RESERVE_PER_TASK_GIB}" =~ ^[1-9][0-9]*$ ]] || { echo "EVAL_MEMORY_RESERVE_PER_TASK_GIB must be a positive integer" >&2; exit 2; }

qwen_submit_args=()
if [[ "${qwen_differential_submit}" -eq 1 ]]; then
    qwen_submit_args+=(--differential-submit)
fi

cleanup() {
    local exit_code=$?
    local worker_pid child_pid
    for worker_pid in "${!worker_tasks[@]}"; do
        while IFS= read -r child_pid; do
            [[ -n "${child_pid}" ]] && kill -INT "${child_pid}" 2>/dev/null || true
        done < <(pgrep -P "${worker_pid}" || true)
    done
    sleep 2
    for worker_pid in "${!worker_tasks[@]}"; do
        kill -TERM "${worker_pid}" 2>/dev/null || true
        wait "${worker_pid}" 2>/dev/null || true
    done
    if [[ -n "${model_pid}" ]] && kill -0 "${model_pid}" 2>/dev/null; then
        kill -TERM "${model_pid}" 2>/dev/null || true
        wait "${model_pid}" 2>/dev/null || true
    fi
    if [[ "${server_started}" -eq 1 ]] && [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
        kill -TERM "${server_pid}" 2>/dev/null || true
        wait "${server_pid}" 2>/dev/null || true
    fi
    exit "${exit_code}"
}
trap cleanup EXIT INT TERM

mkdir -p "${batch_dir}" "${run_root}/model" "${server_run_dir}"

if [[ -z "${server_url}" ]]; then
    server_port="${CYBERGYM_SERVER_PORT:-}"
    if [[ -z "${server_port}" ]]; then
        server_port="$(${repo_root}/.venv/bin/python -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
    fi
    server_url="http://127.0.0.1:${server_port}"
    PORT="${server_port}" CYBERGYM_SERVER_RUN_DIR="${server_run_dir}" \
        bash "${repo_root}/scripts/serving/start_cybergym_server.sh" >"${server_log}" 2>&1 &
    server_pid=$!
    server_started=1
    for _ in $(seq 1 30); do
        if curl --fail --silent --show-error --connect-timeout 2 "${server_url}/openapi.json" >/dev/null; then
            break
        fi
        if ! kill -0 "${server_pid}" 2>/dev/null; then
            tail -n 80 "${server_log}" >&2 || true
            exit 1
        fi
        sleep 2
    done
fi
curl --fail --silent --show-error --connect-timeout 2 "${server_url}/openapi.json" >/dev/null

env -u VLLM_READY_TIMEOUT_S bash "${model_launcher}" \
    "${model_repository}" "${served_model_name}" "${host_port}" >>"${model_log}" 2>&1 &
model_pid=$!

model_ready_deadline=$((SECONDS + model_ready_timeout))
while true; do
    if curl --fail --silent --show-error --connect-timeout 2 "${model_url}/health" >/dev/null; then
        break
    fi
    if ! kill -0 "${model_pid}" 2>/dev/null; then
        tail -n 120 "${model_log}" >&2 || true
        exit 1
    fi
    if (( SECONDS >= model_ready_deadline )); then
        echo "Timed out waiting ${model_ready_timeout}s for ${model_url}/health" >&2
        tail -n 120 "${model_log}" >&2 || true
        exit 1
    fi
    sleep 2
done

curl --fail --silent --show-error --connect-timeout 5 --max-time 180 \
    -H 'Authorization: Bearer local' \
    -H 'Content-Type: application/json' \
    -X POST "${model_url}/v1/chat/completions" \
    --data "{\"model\":\"${served_model_name}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: smoke-ok\"}],\"temperature\":0,\"max_tokens\":16,\"seed\":20260727}" \
    >"${batch_dir}/inference-smoke.json"

memory_working_set_bytes() {
    if [[ -r /sys/fs/cgroup/memory.stat ]]; then
        awk '$1 == "anon" || $1 == "kernel" || $1 == "shmem" || $1 == "sock" { total += $2 } END { printf "%.0f\n", total }' \
            /sys/fs/cgroup/memory.stat
    else
        awk '/^MemTotal:/ { total=$2 } /^MemAvailable:/ { available=$2 } END { print (total-available)*1024 }' /proc/meminfo
    fi
}

memory_allows_new_worker() {
    local gib=$((1024 * 1024 * 1024))
    local current budget reserved
    current="$(memory_working_set_bytes)"
    budget=$((EVAL_MEMORY_BUDGET_GIB * gib))
    reserved=$(( (${#worker_tasks[@]} + 1) * EVAL_MEMORY_RESERVE_PER_TASK_GIB * gib ))
    (( current + reserved <= budget ))
}

wait_for_worker() {
    local finished_pid="" task_id="" worker_exit=0
    if wait -n -p finished_pid; then worker_exit=0; else worker_exit=$?; fi
    task_id="${worker_tasks[${finished_pid}]:-unknown}"
    unset 'worker_tasks['"${finished_pid}"']'
    if (( worker_exit != 0 )); then
        echo "Task ${task_id} worker exited with status ${worker_exit}; continuing batch" >&2
        batch_exit=1
    fi
}

run_one_task() {
    trap - EXIT INT TERM
    local task_id="$1"
    local task_slug run_name run_dir agent_id task_log eval_exit verify_exit
    task_slug="${task_id//:/__}"
    run_name="${batch_name}--${task_slug}"
    run_dir="${run_root}/${run_name}"
    agent_id="${agent_prefix}-${task_slug}"
    task_log="${batch_dir}/${task_slug}.log"
    if [[ -f "${run_dir}/summary.json" && -f "${run_dir}/verification.json" ]]; then
        echo "Skipping completed ${task_id} (${run_name})"
        return 0
    fi
    if [[ -e "${run_dir}" ]]; then
        echo "Refusing to overwrite incomplete run directory: ${run_dir}" >&2
        batch_exit=1
        return 1
    fi
    echo "Running ${task_id} as ${run_name}"
    set +e
    "${repo_root}/.venv/bin/python" "${repo_root}/scripts/evaluation/run_langgraph_eval.py" \
        --task-id "${task_id}" \
        --model "${served_model_name}" \
        --base-url "${model_url}/v1" \
        --data-dir "${data_dir}" \
        --server "${server_url}" \
        --run-root "${run_root}" \
        --run-name "${run_name}" \
        --agent-id "${agent_id}" \
        --model-revision "${model_revision}" \
        --max-steps "${max_steps}" \
        --temperature 0 \
        --seed 20260727 \
        --request-timeout "${model_request_timeout}" \
        "${qwen_submit_args[@]}" \
        >>"${task_log}" 2>&1
    eval_exit=$?
    if [[ -f "${run_dir}/config.json" ]]; then
        "${repo_root}/.venv/bin/python" "${repo_root}/scripts/evaluation/verify_and_record.py" \
            --run-dir "${run_dir}" \
            --server "${server_url}" \
            >>"${task_log}" 2>&1
        verify_exit=$?
    else
        verify_exit=1
    fi
    if [[ -n "${failure_tasks_file}" ]]; then
        "${repo_root}/.venv/bin/python" "${repo_root}/scripts/evaluation/record_failed_task.py" \
            --task-id "${task_id}" --run-dir "${run_dir}" --output "${failure_tasks_file}" \
            --eval-exit "${eval_exit}" --verify-exit "${verify_exit}" >>"${task_log}" 2>&1 || true
    fi
    set -e
    if [[ "${eval_exit}" -ne 0 || "${verify_exit}" -ne 0 ]]; then
        echo "Task ${task_id} finished with eval=${eval_exit}, verify=${verify_exit}; continuing batch" >&2
        return 1
    fi
    return 0
}

echo "Task concurrency=${EVAL_CONCURRENCY}, memory budget=${EVAL_MEMORY_BUDGET_GIB} GiB, per-task reserve=${EVAL_MEMORY_RESERVE_PER_TASK_GIB} GiB"
while IFS= read -r task_id || [[ -n "${task_id}" ]]; do
    [[ -z "${task_id}" || "${task_id}" == \#* ]] && continue
    while (( ${#worker_tasks[@]} >= EVAL_CONCURRENCY )); do wait_for_worker; done
    while ! memory_allows_new_worker; do
        if (( ${#worker_tasks[@]} > 0 )); then wait_for_worker; else sleep 10; fi
    done
    run_one_task "${task_id}" &
    worker_pid=$!
    worker_tasks["${worker_pid}"]="${task_id}"
    echo "Started ${task_id} as worker ${worker_pid} (${#worker_tasks[@]}/${EVAL_CONCURRENCY} active)"
done <"${tasks_file}"

while (( ${#worker_tasks[@]} > 0 )); do wait_for_worker; done

exit "${batch_exit}"
