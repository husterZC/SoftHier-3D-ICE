#!/usr/bin/env bash
# Shared selection for bootstrap, root builds, and coupled runs.
DICE_BUILD_TARGET="${DICE_BUILD_TARGET:-all}"
DICE_DIR="${DICE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/3D-ICE}"
DICE_BIN_DIR="${DICE_BIN_DIR:-$DICE_DIR/bin}"
case "$DICE_BUILD_TARGET" in
    all) DICE_BINARY_SUFFIX="" ;;
    cpu) DICE_BINARY_SUFFIX="-cpu" ;;
    *) printf 'Unsupported DICE_BUILD_TARGET: %s (use all or cpu)\n' "$DICE_BUILD_TARGET" >&2; exit 2 ;;
esac
if [[ "$DICE_BUILD_TARGET" != cpu && -f "$DICE_DIR/sources/cpu_amgcl_solver.cpp" ]]; then
    printf 'Select DICE_BUILD_TARGET=cpu for the new 3D-ICE backend.\n' >&2
    exit 2
fi
SERVER_BIN="$DICE_BIN_DIR/3D-ICE-Server$DICE_BINARY_SUFFIX"
CLIENT_BIN="$DICE_BIN_DIR/3D-ICE-Client$DICE_BINARY_SUFFIX"

dice_build_or_verify() {
    local need_client="${1:-1}" update="${2:-0}" missing=0
    [[ -x "$SERVER_BIN" ]] || missing=1
    if [[ "$need_client" == 1 && ! -x "$CLIENT_BIN" ]]; then missing=1; fi
    if [[ "${BUILD_3DICE:-1}" == 1 ]]; then
        if [[ "$DICE_BUILD_TARGET" == cpu ]]; then
            "${MAKE:-make}" -C "$DICE_DIR" -j2 cpu
        elif [[ "$missing" == 1 ]]; then
            SRC_DIR="$DICE_DIR" bash "$(dirname "${BASH_SOURCE[0]}")/3dice_client_server.sh" install
        elif [[ "$update" == 1 ]]; then
            "${MAKE:-make}" -C "$DICE_DIR" -j2
        fi
    fi
    if [[ ! -x "$SERVER_BIN" || ( "$need_client" == 1 && ! -x "$CLIENT_BIN" ) ]]; then
        printf 'Missing selected 3D-ICE binaries in %s (target %s).\n' "$DICE_BIN_DIR" "$DICE_BUILD_TARGET" >&2
        return 1
    fi
    printf '3D-ICE backend: %s\n' "$SERVER_BIN"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    set -euo pipefail
    dice_build_or_verify 1
fi
