#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
port="${PORT:-18666}"
run_dir="${CYBERGYM_SERVER_RUN_DIR:-${repo_root}/.runs/server}"

mkdir -p "${run_dir}"
cd "${repo_root}"

exec "${repo_root}/.venv/bin/python" -m cybergym.server \
    --host 127.0.0.1 \
    --port "${port}" \
    --mask_map_path "${repo_root}/mask_map.json" \
    --log_dir "${run_dir}" \
    --db_path "${run_dir}/poc.db"
