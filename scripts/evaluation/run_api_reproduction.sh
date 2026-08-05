#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Retained as a compatibility entry point; execution, reporting, cleanup, and
# task concurrency are provided by the shared API batch runner.
EVAL_SAMPLE_SIZE=0 exec bash "${repo_root}/scripts/evaluation/run_api_subset_test_50.sh" "$@"
