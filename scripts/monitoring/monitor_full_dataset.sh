#!/usr/bin/env bash
# Keep the full CyberGym snapshot download alive and emit a 30-minute heartbeat.
set -u -o pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="${ROOT_DIR}/cybergym_data"
RUN_DIR="${ROOT_DIR}/.runs/prepare"
DOWNLOAD_LOG="${RUN_DIR}/full-dataset.log"
MONITOR_LOG="${RUN_DIR}/dataset-monitor.log"
REVISION="bde190ded494e52bc684b66073b436c9d992c7c6"

mkdir -p "${RUN_DIR}"
# Large LFS files are redirected to Hugging Face/Xet CDN hosts. The prior
# host-only bypass accidentally sent those redirects through the local proxy.
# This download is deliberately direct end-to-end.
export NO_PROXY="*"
export no_proxy="*"
export HF_ENDPOINT="https://hf-mirror.com"
export HF_HUB_DISABLE_XET=1
export HF_HUB_DOWNLOAD_TIMEOUT=120

download_loop() {
    local attempt=0
    while true; do
        attempt=$((attempt + 1))
        printf '[%s] download attempt %s\n' "$(date -u +%FT%TZ)" "${attempt}" >> "${MONITOR_LOG}"
        if "${ROOT_DIR}/.venv/bin/python" -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='sunblaze-ucb/cybergym', repo_type='dataset', revision='${REVISION}', local_dir='${DATA_DIR}', max_workers=1)" >> "${DOWNLOAD_LOG}" 2>&1; then
            printf '[%s] complete\n' "$(date -u +%FT%TZ)" >> "${MONITOR_LOG}"
            return 0
        fi
        printf '[%s] transient download failure; retaining cache and retrying in 20s\n' "$(date -u +%FT%TZ)" >> "${MONITOR_LOG}"
        sleep 20
    done
}

download_loop &
worker_pid=$!
trap 'kill "${worker_pid}" 2>/dev/null || true; exit 0' INT TERM EXIT

previous_bytes="$(du -sb "${DATA_DIR}" 2>/dev/null | awk '{print $1}')"
slow_samples=0
minute=0
while kill -0 "${worker_pid}" 2>/dev/null; do
    sleep 60
    current_bytes="$(du -sb "${DATA_DIR}" 2>/dev/null | awk '{print $1}')"
    delta_bytes=$((current_bytes - previous_bytes))
    previous_bytes="${current_bytes}"
    minute=$((minute + 1))
    if (( delta_bytes < 262144 )); then
        slow_samples=$((slow_samples + 1))
    else
        slow_samples=0
    fi
    printf '[%s] speed: bytes_per_minute=%s slow_samples=%s\n' "$(date -u +%FT%TZ)" "${delta_bytes}" "${slow_samples}" >> "${MONITOR_LOG}"
    # Five consecutive minutes below 256 KiB/min are treated as a stall.
    if (( slow_samples >= 5 )); then
        printf '[%s] stalled download detected; restarting from cache\n' "$(date -u +%FT%TZ)" >> "${MONITOR_LOG}"
        kill "${worker_pid}" 2>/dev/null || true
        wait "${worker_pid}" 2>/dev/null || true
        download_loop &
        worker_pid=$!
        slow_samples=0
    fi
    if (( minute % 30 == 0 )); then
        size="$(du -sh "${DATA_DIR}" 2>/dev/null | awk '{print $1}')"
        files="$(find "${DATA_DIR}/data" -type f 2>/dev/null | wc -l)"
        printf '[%s] heartbeat: size=%s files=%s worker_pid=%s\n' "$(date -u +%FT%TZ)" "${size:-0}" "${files}" "${worker_pid}" >> "${MONITOR_LOG}"
    fi
done

wait "${worker_pid}"
