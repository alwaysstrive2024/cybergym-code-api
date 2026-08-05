#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# The shared API batch runner uses EVAL_SAMPLE_SIZE=0 for a complete manifest.
# EVAL_CONCURRENCY defaults to 1 and can be raised for remote API backends.
EVAL_SAMPLE_SIZE=0 exec bash "${repo_root}/scripts/evaluation/run_api_subset_test_50.sh" "$@"
