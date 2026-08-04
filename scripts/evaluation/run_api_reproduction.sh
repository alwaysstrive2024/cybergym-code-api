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
failed_file="${batch_dir}/failed_tasks.txt"
report_script="${repo_root}/scripts/evaluation/summarize_api_batch.py"
server_pid=""
server_started=0
batch_exit=0

[[ -f "${profile_file}" ]] || { echo "Profile does not exist: ${profile_file}" >&2; exit 2; }
[[ -f "${tasks_file}" ]] || { echo "Task manifest does not exist: ${tasks_file}" >&2; exit 2; }
[[ "$(basename "${batch_name}")" == "${batch_name}" ]] || { echo "BATCH_NAME must be a single path component" >&2; exit 2; }

# shellcheck disable=SC1090
source "${profile_file}"
: "${API_BASE_URL:?profile must set API_BASE_URL}"
: "${API_MODEL:?profile must set API_MODEL}"
: "${API_KEY_ENV:?profile must set API_KEY_ENV}"
: "${API_MODE:=chat_completions}"
: "${API_REQUEST_TIMEOUT_S:=900}"
: "${API_REQUEST_RETRIES:=0}"
: "${API_MAX_STEPS:=40}"
: "${API_MAX_TOKENS:=4096}"
: "${API_TEMPERATURE:=0}"
[[ -n "${!API_KEY_ENV:-}" ]] || {
    echo "Required API key environment variable is not set: ${API_KEY_ENV}" >&2
    exit 2
}
[[ -x "${repo_root}/.venv/bin/python" ]] || {
    echo "Missing .venv; run: uv sync --extra agent --extra server" >&2
    exit 2
}
[[ -d "${data_dir}" ]] || {
    echo "CyberGym dataset is missing: ${data_dir}" >&2
    exit 2
}
if [[ -n "${server_url}" ]]; then
    [[ -n "${CYBERGYM_API_KEY:-}" ]] || {
        echo "CYBERGYM_API_KEY is required when CYBERGYM_SERVER_URL points to an existing server" >&2
        exit 2
    }
elif [[ -z "${CYBERGYM_API_KEY:-}" ]]; then
    CYBERGYM_API_KEY="$("${repo_root}/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')"
    export CYBERGYM_API_KEY
fi

cleanup() {
    local exit_code=$?
    if [[ "${server_started}" -eq 1 ]] && [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
        kill -TERM "${server_pid}" 2>/dev/null || true
        wait "${server_pid}" 2>/dev/null || true
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
        --tasks-file "${tasks_file}" \
        --run-root "${run_root}" \
        --batch-name "${batch_name}" \
        --output-dir "${batch_dir}"
}

if [[ -z "${server_url}" ]]; then
    server_port="${CYBERGYM_SERVER_PORT:-}"
    if [[ -z "${server_port}" ]]; then
        server_port="$("${repo_root}/.venv/bin/python" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
    fi
    server_url="http://127.0.0.1:${server_port}"
    PORT="${server_port}" CYBERGYM_SERVER_RUN_DIR="${server_run_dir}" \
        bash "${repo_root}/scripts/serving/start_cybergym_server.sh" >"${server_run_dir}/launcher.log" 2>&1 &
    server_pid=$!
    server_started=1
    for _ in $(seq 1 30); do
        if curl --fail --silent --connect-timeout 2 "${server_url}/openapi.json" >/dev/null 2>&1; then
            break
        fi
        if ! kill -0 "${server_pid}" 2>/dev/null; then
            tail -n 80 "${server_run_dir}/launcher.log" >&2
            exit 1
        fi
        sleep 2
    done
fi
curl --fail --silent --show-error --connect-timeout 2 "${server_url}/openapi.json" >/dev/null

refresh_report
while IFS= read -r task_id || [[ -n "${task_id}" ]]; do
    [[ -z "${task_id}" || "${task_id}" == \#* ]] && continue
    task_slug="${task_id//:/__}"
    run_dir="${task_runs_dir}/${task_slug}"
    task_log="${logs_dir}/${task_slug}.log"
    agent_id="${batch_name}-${task_slug}"

    if [[ -f "${run_dir}/summary.json" && -f "${run_dir}/verification.json" ]]; then
        echo "Skipping completed ${task_id}"
        continue
    fi
    if [[ -e "${run_dir}" ]]; then
        archive_dir="${batch_dir}/incomplete-runs/${task_slug}-$(date -u +%Y%m%dT%H%M%SZ)"
        mkdir -p "$(dirname "${archive_dir}")"
        mv "${run_dir}" "${archive_dir}"
    fi

    echo "Running ${task_id} with ${API_MODEL}"
    set +e
    "${repo_root}/.venv/bin/python" "${repo_root}/scripts/evaluation/run_langgraph_eval.py" \
        --task-id "${task_id}" \
        --model "${API_MODEL}" \
        --base-url "${API_BASE_URL}" \
        --api-key-env "${API_KEY_ENV}" \
        --api-mode "${API_MODE}" \
        --data-dir "${data_dir}" \
        --server "${server_url}" \
        --run-root "${task_runs_dir}" \
        --run-name "${task_slug}" \
        --agent-id "${agent_id}" \
        --max-steps "${API_MAX_STEPS}" \
        --max-tokens "${API_MAX_TOKENS}" \
        --temperature "${API_TEMPERATURE}" \
        --request-timeout "${API_REQUEST_TIMEOUT_S}" \
        --request-retries "${API_REQUEST_RETRIES}" >>"${task_log}" 2>&1
    eval_exit=$?

    if [[ -f "${run_dir}/config.json" ]]; then
        "${repo_root}/.venv/bin/python" "${repo_root}/scripts/evaluation/verify_and_record.py" \
            --run-dir "${run_dir}" --server "${server_url}" >>"${task_log}" 2>&1
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
