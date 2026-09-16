#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
STUDY_ID="${STUDY_ID:-$(date +%Y%m%d-%H%M%S)}"
OUTPUT_DIR="${OUTPUT_DIR:-$SCRIPT_DIR/results/$STUDY_ID}"
ANALYSIS_CONDA_ENV="${ANALYSIS_CONDA_ENV:-${SOFTHIER_CONDA_ENV:-py312}}"

cd "$ROOT_DIR"

make co-simulation \
    RUN_NAME=leakage_constant \
    RUN_ID="$STUDY_ID" \
    SOFTHIER_POWER_PROFILE=constant \
    SIMULATOR_LOG_TAIL_LINES=0

make coupled-run \
    RUN_NAME=leakage_temperature_aware \
    RUN_ID="$STUDY_ID" \
    SOFTHIER_POWER_PROFILE=temperature_aware \
    BUILD_SIMULATOR=0 \
    BUILD_3DICE=0 \
    SIMULATOR_LOG_TAIL_LINES=0

analysis_args=(
    "$SCRIPT_DIR/analyze.py"
    --constant-run "$ROOT_DIR/runs/leakage_constant/$STUDY_ID"
    --temperature-aware-run "$ROOT_DIR/runs/leakage_temperature_aware/$STUDY_ID"
    --output-dir "$OUTPUT_DIR"
)

MPLCONFIGDIR="${MPLCONFIGDIR:-/tmp/softhier-leakage-matplotlib}" \
    conda run -n "$ANALYSIS_CONDA_ENV" python "${analysis_args[@]}"

printf 'Study complete: %s\n' "$OUTPUT_DIR"
