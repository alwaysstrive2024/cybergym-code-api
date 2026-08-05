#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "Usage: $0 PROFILE_ENV TASKS_FILE BATCH_NAME" >&2
    exit 2
fi

profile_file="$1"
source_tasks_file="$2"
batch_name="$3"
sample_size="${EVAL_SAMPLE_SIZE:-50}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
run_root="${repo_root}/outputs"
batch_dir="${run_root}/${batch_name}"
if [[ "${sample_size}" == "0" ]]; then
    tasks_file="${source_tasks_file}"
else
    tasks_file="${batch_dir}/sampled_tasks_${sample_size}.txt"
fi
task_runs_dir="${batch_dir}/tasks"
logs_dir="${batch_dir}/logs"
server_url="${CYBERGYM_SERVER_URL:-}"
server_run_dir="${CYBERGYM_SERVER_RUN_DIR:-${batch_dir}/server}"
data_dir="${CYBERGYM_DATA_DIR:-${repo_root}/cybergym_data/data}"
server_pid=""
server_started=0
batch_exit=0
declare -A worker_tasks=()
failed_file="${batch_dir}/failed_tasks.txt"
report_script="${repo_root}/scripts/evaluation/summarize_api_batch.py"

[[ -f "${profile_file}" && -f "${source_tasks_file}" ]] || { echo "Profile or task manifest does not exist" >&2; exit 2; }
[[ "$(basename "${batch_name}")" == "${batch_name}" ]] || { echo "BATCH_NAME must be a single path component" >&2; exit 2; }
[[ "${sample_size}" =~ ^[0-9]+$ ]] || { echo "EVAL_SAMPLE_SIZE must be a non-negative integer" >&2; exit 2; }

# shellcheck disable=SC1090
source "${profile_file}"
: "${API_MODE:=chat_completions}"
: "${API_OMIT_TOP_P:=false}"
: "${API_REQUEST_TIMEOUT_S:=900}"
: "${API_MAX_STEPS:=40}"
: "${API_MAX_TOKENS:=4096}"
: "${API_TEMPERATURE:=0}"
: "${API_REQUEST_RETRIES:=0}"
: "${USE_CLAUDE_CODE_AGENT:=false}"
: "${CLAUDE_CODE_PROVIDER:=bridge}"
: "${CLAUDE_CODE_API_KEY_ENV:=ANTHROPIC_API_KEY}"
: "${CLAUDE_CODE_SESSION_TURN_BUDGET:=12}"
: "${API_CONTEXT_TOKEN_BUDGET:=24576}"
: "${API_MAX_TOOL_RESULT_CHARS:=12288}"
: "${API_RESPONSE_COMPACTION_TURNS:=12}"
: "${EVAL_CONCURRENCY:=4}"
: "${EVAL_MEMORY_BUDGET_GIB:=150}"
: "${EVAL_MEMORY_RESERVE_PER_TASK_GIB:=8}"
: "${API_GATEWAY_502_LIMIT:=20}"
[[ "${EVAL_CONCURRENCY}" =~ ^[1-9][0-9]*$ ]] || { echo "EVAL_CONCURRENCY must be a positive integer" >&2; exit 2; }
[[ "${EVAL_MEMORY_BUDGET_GIB}" =~ ^[1-9][0-9]*$ ]] || { echo "EVAL_MEMORY_BUDGET_GIB must be a positive integer" >&2; exit 2; }
[[ "${EVAL_MEMORY_RESERVE_PER_TASK_GIB}" =~ ^[1-9][0-9]*$ ]] || { echo "EVAL_MEMORY_RESERVE_PER_TASK_GIB must be a positive integer" >&2; exit 2; }
[[ "${API_GATEWAY_502_LIMIT}" =~ ^[1-9][0-9]*$ ]] || { echo "API_GATEWAY_502_LIMIT must be a positive integer" >&2; exit 2; }
case "${USE_CLAUDE_CODE_AGENT}" in
    true|false) ;;
    *) echo "USE_CLAUDE_CODE_AGENT must be exactly true or false" >&2; exit 2 ;;
esac
if [[ "${USE_CLAUDE_CODE_AGENT}" == "false" || "${CLAUDE_CODE_PROVIDER}" == "bridge" ]]; then
    : "${API_BASE_URL:?profile must set API_BASE_URL}"
    : "${API_MODEL:?profile must set API_MODEL}"
    : "${API_KEY_ENV:?profile must set API_KEY_ENV}"
    [[ -n "${!API_KEY_ENV:-}" ]] || { echo "Required API key environment variable is not set: ${API_KEY_ENV}" >&2; exit 2; }
elif [[ "${USE_CLAUDE_CODE_AGENT}" == "true" && "${CLAUDE_CODE_PROVIDER}" == "anthropic" ]]; then
    : "${CLAUDE_CODE_MODEL:?profile must set CLAUDE_CODE_MODEL for official Anthropic mode}"
    [[ -n "${!CLAUDE_CODE_API_KEY_ENV:-}" ]] || {
        echo "Required Anthropic API key environment variable is not set: ${CLAUDE_CODE_API_KEY_ENV}" >&2
        exit 2
    }
else
    echo "CLAUDE_CODE_PROVIDER must be bridge or anthropic" >&2
    exit 2
fi
case "${API_OMIT_TOP_P}" in
    true|false) ;;
    *) echo "API_OMIT_TOP_P must be exactly true or false" >&2; exit 2 ;;
esac
top_p_args=()
if [[ "${API_OMIT_TOP_P}" == "true" ]]; then
    top_p_args+=(--omit-top-p)
fi
[[ -x "${repo_root}/.venv/bin/python" ]] || {
    echo "Missing .venv; run: uv sync --extra agent --extra server" >&2
    exit 2
}
[[ -d "${data_dir}" ]] || {
    echo "CyberGym dataset is missing: ${data_dir}" >&2
    exit 2
}

# A locally launched verifier needs a shared management credential, but it is
# an implementation detail of this batch. Generate it in memory and pass it to
# the server and verification subprocesses through their inherited environment.
# An explicitly configured remote verifier still requires the organizer's key.
if [[ -z "${server_url}" ]]; then
    if [[ -z "${CYBERGYM_API_KEY:-}" ]]; then
        CYBERGYM_API_KEY="$("${repo_root}/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')"
        export CYBERGYM_API_KEY
    fi
elif [[ -z "${CYBERGYM_API_KEY:-}" ]]; then
    echo "CYBERGYM_API_KEY is required when CYBERGYM_SERVER_URL points to an existing server" >&2
    exit 2
fi

claude_bridge_pid=""
claude_bridge_url=""
claude_bridge_log="${logs_dir}/claude-code-gateway.log"

stop_process() {
    local pid="$1"
    local attempt
    kill -TERM "${pid}" 2>/dev/null || true
    for attempt in $(seq 1 10); do
        kill -0 "${pid}" 2>/dev/null || return 0
        sleep 1
    done
    kill -KILL "${pid}" 2>/dev/null || true
    wait "${pid}" 2>/dev/null || true
}

cleanup() {
    local exit_code=$?
    local worker_pid child_pid attempt
    # Stop each evaluator first so its Python finally block can remove the
    # Docker sandbox. Killing the wrapper shell first would orphan the agent.
    for worker_pid in "${!worker_tasks[@]}"; do
        while IFS= read -r child_pid; do
            [[ -n "${child_pid}" ]] && kill -INT "${child_pid}" 2>/dev/null || true
        done < <(pgrep -P "${worker_pid}" || true)
    done
    for attempt in $(seq 1 10); do
        local children_alive=0
        for worker_pid in "${!worker_tasks[@]}"; do
            if pgrep -P "${worker_pid}" >/dev/null 2>&1; then
                children_alive=1
                break
            fi
        done
        (( children_alive == 0 )) && break
        sleep 1
    done
    for worker_pid in "${!worker_tasks[@]}"; do
        while IFS= read -r child_pid; do
            [[ -n "${child_pid}" ]] && kill -TERM "${child_pid}" 2>/dev/null || true
        done < <(pgrep -P "${worker_pid}" || true)
        kill -TERM "${worker_pid}" 2>/dev/null || true
        wait "${worker_pid}" 2>/dev/null || true
    done
    if [[ -f "${tasks_file}" && -x "${repo_root}/.venv/bin/python" ]]; then
        "${repo_root}/.venv/bin/python" "${report_script}" \
            --tasks-file "${tasks_file}" --run-root "${run_root}" \
            --batch-name "${batch_name}" --output-dir "${batch_dir}" || true
    fi
    if [[ "${server_started}" -eq 1 ]] && [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
        stop_process "${server_pid}"
    fi
    if [[ -n "${claude_bridge_pid}" ]] && kill -0 "${claude_bridge_pid}" 2>/dev/null; then
        stop_process "${claude_bridge_pid}"
    fi
    exit "${exit_code}"
}
trap cleanup EXIT INT TERM

mkdir -p "${batch_dir}" "${task_runs_dir}" "${logs_dir}" "${server_run_dir}"
exec 9>"${batch_dir}/run.lock"
if ! flock -n 9; then
    echo "Another runner is already using batch ${batch_name}" >&2
    exit 1
fi

# Keep a sampled manifest with the batch so retries evaluate the same tasks.
# EVAL_SAMPLE_SIZE=0 evaluates the supplied manifest directly.
if (( sample_size > 0 )) && [[ ! -f "${tasks_file}" ]]; then
    candidate_count="$(awk 'NF && $0 !~ /^[[:space:]]*#/ { count++ } END { print count + 0 }' "${source_tasks_file}")"
    if (( candidate_count < sample_size )); then
        echo "Task manifest contains only ${candidate_count} usable tasks; ${sample_size} are required" >&2
        exit 2
    fi
    awk 'NF && $0 !~ /^[[:space:]]*#/' "${source_tasks_file}" | shuf -n "${sample_size}" >"${tasks_file}"
    echo "Randomly sampled ${sample_size} tasks from ${source_tasks_file} into ${tasks_file}"
elif (( sample_size > 0 )); then
    sampled_count="$(wc -l <"${tasks_file}")"
    [[ "${sampled_count}" -eq "${sample_size}" ]] || {
        echo "Existing sampled manifest must contain exactly ${sample_size} tasks: ${tasks_file}" >&2
        exit 2
    }
    echo "Reusing ${sample_size} sampled tasks from ${tasks_file}"
else
    echo "Using complete task manifest ${tasks_file}"
fi

refresh_report() {
    "${repo_root}/.venv/bin/python" "${report_script}" \
        --tasks-file "${tasks_file}" --run-root "${run_root}" \
        --batch-name "${batch_name}" --output-dir "${batch_dir}"
}

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
    local finished_pid=""
    local task_id=""
    local worker_exit=0
    if wait -n -p finished_pid; then
        worker_exit=0
    else
        worker_exit=$?
    fi
    task_id="${worker_tasks[${finished_pid}]:-unknown}"
    unset 'worker_tasks['"${finished_pid}"']'
    if (( worker_exit != 0 )); then
        echo "Task ${task_id} worker exited with status ${worker_exit}; continuing batch" >&2
        batch_exit=1
    fi
    if [[ "${USE_CLAUDE_CODE_AGENT}" == "true" && "${CLAUDE_CODE_PROVIDER}" == "bridge" && -f "${claude_bridge_log}" ]]; then
        local gateway_502_count
        gateway_502_count="$(grep -Ec 'POST /v1/messages.* (429 Too Many Requests|502 Bad Gateway)' "${claude_bridge_log}" || true)"
        if (( gateway_502_count >= API_GATEWAY_502_LIMIT )); then
            echo "Circuit breaker: Claude gateway returned ${gateway_502_count} HTTP 429/502 responses (limit ${API_GATEWAY_502_LIMIT})" >&2
            exit 75
        fi
    fi
}

refresh_report
if [[ "${USE_CLAUDE_CODE_AGENT}" == "true" && "${CLAUDE_CODE_PROVIDER}" == "bridge" ]]; then
    if [[ "${API_MODE}" != "chat_completions" ]]; then
        echo "Claude Code bridge currently requires API_MODE=chat_completions" >&2
        exit 2
    fi
    "${repo_root}/.venv/bin/python" -c 'import claude_agent_sdk' >/dev/null
    claude_bridge_port="$(${repo_root}/.venv/bin/python -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
    claude_bridge_url="http://127.0.0.1:${claude_bridge_port}"
    export CYBERGYM_CLAUDE_GATEWAY_TOKEN
    CYBERGYM_CLAUDE_GATEWAY_TOKEN="$(${repo_root}/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    CYBERGYM_UPSTREAM_BASE_URL="${API_BASE_URL}" \
    CYBERGYM_UPSTREAM_MODEL="${API_MODEL}" \
    CYBERGYM_UPSTREAM_API_KEY="${!API_KEY_ENV}" \
    CYBERGYM_CLAUDE_GATEWAY_TOKEN="${CYBERGYM_CLAUDE_GATEWAY_TOKEN}" \
        "${repo_root}/.venv/bin/python" -m cybergym.agents.anthropic_bridge \
        --port "${claude_bridge_port}" >"${claude_bridge_log}" 2>&1 &
    claude_bridge_pid=$!
    for _ in $(seq 1 30); do
        if curl --fail --silent --connect-timeout 2 "${claude_bridge_url}/health" >/dev/null 2>&1; then break; fi
        if ! kill -0 "${claude_bridge_pid}" 2>/dev/null; then
            tail -n 80 "${claude_bridge_log}" >&2 || true
            exit 1
        fi
        sleep 1
    done
    curl --fail --silent --show-error --connect-timeout 2 "${claude_bridge_url}/health" >/dev/null
fi
if [[ -z "${server_url}" ]]; then
    server_port="${CYBERGYM_SERVER_PORT:-}"
    if [[ -z "${server_port}" ]]; then
        server_port="$(${repo_root}/.venv/bin/python -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
    fi
    server_url="http://127.0.0.1:${server_port}"
    echo "Starting isolated CyberGym server for batch ${batch_name} at ${server_url}"
    PORT="${server_port}" CYBERGYM_SERVER_RUN_DIR="${server_run_dir}" \
        bash "${repo_root}/scripts/serving/start_cybergym_server.sh" >"${server_run_dir}/launcher.log" 2>&1 &
    server_pid=$!
    server_started=1
    for _ in $(seq 1 30); do
        if curl --fail --silent --connect-timeout 2 "${server_url}/openapi.json" >/dev/null 2>&1; then break; fi
        if ! kill -0 "${server_pid}" 2>/dev/null; then tail -n 80 "${server_run_dir}/launcher.log" >&2; exit 1; fi
        sleep 2
    done
else
    echo "Using existing CyberGym server at ${server_url}"
fi
curl --fail --silent --show-error --connect-timeout 2 "${server_url}/openapi.json" >/dev/null

run_one_task() {
    trap - EXIT INT TERM
    local task_id="$1"
    local task_slug run_name run_dir agent_id task_log archive_dir eval_exit verify_exit
    [[ -z "${task_id}" || "${task_id}" == \#* ]] && return 0
    task_slug="${task_id//:/__}"
    run_name="${task_slug}"
    run_dir="${task_runs_dir}/${run_name}"
    agent_id="${batch_name}-${task_slug}"
    task_log="${logs_dir}/${task_slug}.log"
    if [[ -f "${run_dir}/summary.json" && -f "${run_dir}/verification.json" ]]; then echo "Skipping completed ${task_id}"; return 0; fi
    if [[ -e "${run_dir}" ]]; then
        archive_dir="${batch_dir}/incomplete-runs/${task_slug}-$(date -u +%Y%m%dT%H%M%SZ)"
        mkdir -p "$(dirname "${archive_dir}")"
        echo "Archiving incomplete ${task_id} run to ${archive_dir}"
        mv "${run_dir}" "${archive_dir}"
    fi
    if [[ "${USE_CLAUDE_CODE_AGENT}" == "true" ]]; then
        if [[ "${CLAUDE_CODE_PROVIDER}" == "anthropic" ]]; then
            echo "Running ${task_id} with official Claude Code Agent and ${CLAUDE_CODE_MODEL}"
        else
            echo "Running ${task_id} with Claude Code Agent bridge and ${API_MODEL}"
        fi
    else
        echo "Running ${task_id} with LangGraph Agent and ${API_MODEL}"
    fi
    set +e
    if [[ "${USE_CLAUDE_CODE_AGENT}" == "true" ]]; then
        if [[ "${CLAUDE_CODE_PROVIDER}" == "bridge" ]]; then
            env -u "${API_KEY_ENV}" \
                "${repo_root}/.venv/bin/python" "${repo_root}/scripts/evaluation/run_claude_code_eval.py" \
                --task-id "${task_id}" --model "${API_MODEL}" --provider bridge \
                --anthropic-base-url "${claude_bridge_url}" \
                --data-dir "${data_dir}" --server "${server_url}" --run-root "${task_runs_dir}" \
                --run-name "${run_name}" --agent-id "${agent_id}" \
                --max-turns "${API_MAX_STEPS}" --session-turn-budget "${CLAUDE_CODE_SESSION_TURN_BUDGET}" \
                --timeout "${API_REQUEST_TIMEOUT_S}" --max-tool-result-chars "${API_MAX_TOOL_RESULT_CHARS}" \
                --differential-submit >>"${task_log}" 2>&1
        else
            env -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN \
                "${repo_root}/.venv/bin/python" "${repo_root}/scripts/evaluation/run_claude_code_eval.py" \
                --task-id "${task_id}" --model "${CLAUDE_CODE_MODEL}" --provider anthropic \
                --api-key-env "${CLAUDE_CODE_API_KEY_ENV}" \
                --data-dir "${data_dir}" --server "${server_url}" --run-root "${task_runs_dir}" \
                --run-name "${run_name}" --agent-id "${agent_id}" \
                --max-turns "${API_MAX_STEPS}" --session-turn-budget "${CLAUDE_CODE_SESSION_TURN_BUDGET}" \
                --timeout "${API_REQUEST_TIMEOUT_S}" --max-tool-result-chars "${API_MAX_TOOL_RESULT_CHARS}" \
                --differential-submit >>"${task_log}" 2>&1
        fi
    else
        "${repo_root}/.venv/bin/python" "${repo_root}/scripts/evaluation/run_langgraph_eval.py" \
            --task-id "${task_id}" --model "${API_MODEL}" --base-url "${API_BASE_URL}" \
            --api-key-env "${API_KEY_ENV}" --api-mode "${API_MODE}" \
            --data-dir "${data_dir}" --server "${server_url}" --run-root "${task_runs_dir}" \
            --run-name "${run_name}" --agent-id "${agent_id}" \
            --max-steps "${API_MAX_STEPS}" --max-tokens "${API_MAX_TOKENS}" \
            --context-token-budget "${API_CONTEXT_TOKEN_BUDGET}" \
            --max-tool-result-chars "${API_MAX_TOOL_RESULT_CHARS}" \
            --response-compaction-turns "${API_RESPONSE_COMPACTION_TURNS}" \
            --temperature "${API_TEMPERATURE}" --seed 20260731 \
            "${top_p_args[@]}" \
            --request-timeout "${API_REQUEST_TIMEOUT_S}" --request-retries "${API_REQUEST_RETRIES}" >>"${task_log}" 2>&1
    fi
    eval_exit=$?
    if [[ -f "${run_dir}/config.json" ]]; then
        "${repo_root}/.venv/bin/python" "${repo_root}/scripts/evaluation/verify_and_record.py" --run-dir "${run_dir}" --server "${server_url}" >>"${task_log}" 2>&1
        verify_exit=$?
    else
        verify_exit=1
    fi
    "${repo_root}/.venv/bin/python" "${repo_root}/scripts/evaluation/record_failed_task.py" \
        --task-id "${task_id}" --run-dir "${run_dir}" --output "${failed_file}" \
        --eval-exit "${eval_exit}" --verify-exit "${verify_exit}" >>"${task_log}" 2>&1 || true
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
    while (( ${#worker_tasks[@]} >= EVAL_CONCURRENCY )); do
        wait_for_worker
    done
    while ! memory_allows_new_worker; do
        current_gib=$(( $(memory_working_set_bytes) / 1024 / 1024 / 1024 ))
        if (( ${#worker_tasks[@]} > 0 )); then
            echo "Memory pressure: ${current_gib} GiB working set; waiting for a worker before starting ${task_id}"
            wait_for_worker
        else
            echo "Memory pressure: ${current_gib} GiB working set; waiting to stay below ${EVAL_MEMORY_BUDGET_GIB} GiB" >&2
            sleep 10
        fi
    done
    run_one_task "${task_id}" &
    worker_pid=$!
    worker_tasks["${worker_pid}"]="${task_id}"
    echo "Started ${task_id} as worker ${worker_pid} (${#worker_tasks[@]}/${EVAL_CONCURRENCY} active)"
done <"${tasks_file}"

while (( ${#worker_tasks[@]} > 0 )); do
    wait_for_worker
done

refresh_report

exit "${batch_exit}"
