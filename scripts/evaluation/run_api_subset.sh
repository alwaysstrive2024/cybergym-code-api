#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "Usage: $0 PROFILE_ENV TASKS_FILE BATCH_NAME" >&2
    exit 2
fi

profile_file="$1"
tasks_file="$2"
batch_name="$3"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
run_root="${repo_root}/outputs"
batch_dir="${run_root}/${batch_name}"
task_runs_dir="${batch_dir}/tasks"
logs_dir="${batch_dir}/logs"
server_url="${CYBERGYM_SERVER_URL:-}"
server_run_dir="${CYBERGYM_SERVER_RUN_DIR:-${batch_dir}/server}"
data_dir="${CYBERGYM_DATA_DIR:-${repo_root}/cybergym_data/data}"
server_pid=""
server_started=0
batch_exit=0
failed_file="${batch_dir}/failed_tasks.txt"
report_script="${repo_root}/scripts/evaluation/summarize_api_batch.py"

[[ -f "${profile_file}" && -f "${tasks_file}" ]] || { echo "Profile or task manifest does not exist" >&2; exit 2; }
[[ "$(basename "${batch_name}")" == "${batch_name}" ]] || { echo "BATCH_NAME must be a single path component" >&2; exit 2; }

# shellcheck disable=SC1090
source "${profile_file}"
: "${API_MODE:=chat_completions}"
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

cleanup() {
    local exit_code=$?
    if [[ -f "${tasks_file}" && -x "${repo_root}/.venv/bin/python" ]]; then
        "${repo_root}/.venv/bin/python" "${report_script}" \
            --tasks-file "${tasks_file}" --run-root "${run_root}" \
            --batch-name "${batch_name}" --output-dir "${batch_dir}" || true
    fi
    if [[ "${server_started}" -eq 1 ]] && [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
        kill -TERM "${server_pid}" 2>/dev/null || true
        wait "${server_pid}" 2>/dev/null || true
    fi
    if [[ -n "${claude_bridge_pid}" ]] && kill -0 "${claude_bridge_pid}" 2>/dev/null; then
        kill -TERM "${claude_bridge_pid}" 2>/dev/null || true
        wait "${claude_bridge_pid}" 2>/dev/null || true
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

refresh_report() {
    "${repo_root}/.venv/bin/python" "${report_script}" \
        --tasks-file "${tasks_file}" --run-root "${run_root}" \
        --batch-name "${batch_name}" --output-dir "${batch_dir}"
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

while IFS= read -r task_id || [[ -n "${task_id}" ]]; do
    [[ -z "${task_id}" || "${task_id}" == \#* ]] && continue
    task_slug="${task_id//:/__}"
    run_name="${task_slug}"
    run_dir="${task_runs_dir}/${run_name}"
    agent_id="${batch_name}-${task_slug}"
    task_log="${logs_dir}/${task_slug}.log"
    if [[ -f "${run_dir}/summary.json" && -f "${run_dir}/verification.json" ]]; then echo "Skipping completed ${task_id}"; continue; fi
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
    refresh_report
    if [[ "${eval_exit}" -ne 0 || "${verify_exit}" -ne 0 ]]; then
        echo "Task ${task_id} finished with eval=${eval_exit}, verify=${verify_exit}; continuing batch" >&2
        batch_exit=1
    fi
done <"${tasks_file}"

exit "${batch_exit}"
