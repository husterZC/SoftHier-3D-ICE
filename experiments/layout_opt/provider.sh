#!/usr/bin/env bash
set -euo pipefail
# The coupled runner supplies ROOT_DIR, including when this wrapper is archived.
: "${ROOT_DIR:?ROOT_DIR must name this checkout}"
: "${LAYOUT_OPT_CONFIG:?LAYOUT_OPT_CONFIG must name the archived layout JSON}"
export SOFTHIER_PROVIDER_SCRIPT_DIR="$ROOT_DIR/Interface_scripts/providers/softhier"
provider="$SOFTHIER_PROVIDER_SCRIPT_DIR/provider.sh"
case "${1:-}" in
    export-system)
        [[ "${SOFTHIER_FLOORPLAN:-}" == square_bands ]] || {
            echo 'layout optimization requires square_bands' >&2; exit 2;
        }
        bash "$provider" "$@"
        "${PYTHON:-python3}" "$ROOT_DIR/experiments/layout_opt/native_export.py" \
            --system-config "$SYSTEM_CONFIG_FILE" --layout "$LAYOUT_OPT_CONFIG"
        ;;
    manifest)
        bash "$provider" "$@"
        printf 'LAYOUT_OPT_CONFIG=%q\n' "$LAYOUT_OPT_CONFIG"
        ;;
    *) exec bash "$provider" "$@" ;;
esac
