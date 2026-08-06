#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "Usage: $0 CONTROLLER_PID LIMIT_GIB LOG_FILE" >&2
    exit 2
fi

controller_pid="$1"
limit_gib="$2"
log_file="$3"
limit_kib=$((limit_gib * 1024 * 1024))

while kill -0 "${controller_pid}" 2>/dev/null; do
    total_kib=$(awk '/MemTotal:/ {print $2}' /proc/meminfo)
    available_kib=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)
    used_kib=$((total_kib - available_kib))
    printf '%(%Y-%m-%dT%H:%M:%SZ)T used_gib=%.2f limit_gib=%s\n' -1 \
        "$(awk -v k="${used_kib}" 'BEGIN {print k / 1024 / 1024}')" "${limit_gib}" >>"${log_file}"
    if (( used_kib >= limit_kib )); then
        printf '%(%Y-%m-%dT%H:%M:%SZ)T RAM_GUARD_TRIGGERED stopping_pid=%s\n' -1 "${controller_pid}" >>"${log_file}"
        kill -TERM "${controller_pid}" 2>/dev/null || true
        exit 1
    fi
    sleep 5
done
