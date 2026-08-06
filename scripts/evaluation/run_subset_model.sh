#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 6 ]]; then
    echo "Usage: $0 MODEL_REPOSITORY SERVED_MODEL_NAME HOST_PORT RUN_NAME AGENT_ID MODEL_REVISION" >&2
    exit 2
fi

model_repository="$1"
served_model_name="$2"
host_port="$3"
run_name="$4"
agent_id="$5"
model_revision="$6"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
run_root="${repo_root}/.runs"
server_url="${CYBERGYM_SERVER_URL:-}"
server_run_dir="${CYBERGYM_SERVER_RUN_DIR:-${run_root}/server-${run_name}}"
model_url="http://127.0.0.1:${host_port}"
model_ready_timeout="${MODEL_READY_TIMEOUT_S:-${VLLM_READY_TIMEOUT_S:-86400}}"
model_launcher="${MODEL_LAUNCHER:-${repo_root}/scripts/serving/serve_vllm_host_8bit.sh}"
max_steps="${MAX_STEPS:-40}"
model_log="${run_root}/model/${run_name}.log"
server_log="${server_run_dir}/launcher.log"
model_pid=""
server_pid=""
server_started=0

cleanup() {
    local exit_code=$?
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

mkdir -p "${run_root}/model" "${server_run_dir}"

# Keep the served checkpoint immutable and make the pilot's exploration cap explicit.
export MODEL_REVISION="${MODEL_REVISION:-${model_revision}}"

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
curl --fail --silent --show-error --connect-timeout 2 "${model_url}/health" >/dev/null

curl --fail --silent --show-error --connect-timeout 5 --max-time 180 \
    -H 'Authorization: Bearer local' \
    -H 'Content-Type: application/json' \
    -X POST "${model_url}/v1/chat/completions" \
    --data "{\"model\":\"${served_model_name}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: smoke-ok\"}],\"temperature\":0,\"max_tokens\":16,\"seed\":20260727}" \
    >"${run_root}/model/${run_name}.inference-smoke.json"

"${repo_root}/.venv/bin/python" "${repo_root}/scripts/evaluation/run_langgraph_eval.py" \
    --task-id arvo:10400 \
    --model "${served_model_name}" \
    --base-url "${model_url}/v1" \
    --data-dir "${repo_root}/cybergym_data/data" \
    --server "${server_url}" \
    --run-root "${run_root}" \
    --run-name "${run_name}" \
    --agent-id "${agent_id}" \
    --model-revision "${model_revision}" \
    --max-steps "${max_steps}" \
    --max-tokens 4096 \
    --temperature 0 \
    --seed 20260727

"${repo_root}/.venv/bin/python" "${repo_root}/scripts/evaluation/verify_and_record.py" \
    --run-dir "${run_root}/${run_name}" \
    --server "${server_url}"
