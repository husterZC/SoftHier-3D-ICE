#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${ROOT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
SOFTHIER_DIR="${SOFTHIER_DIR:-$ROOT_DIR/SoftHier}"
DICE_DIR="${DICE_DIR:-$ROOT_DIR/3D-ICE}"
DICE_BIN_DIR="${DICE_BIN_DIR:-$DICE_DIR/bin}"
BUILD_3DICE="${BUILD_3DICE:-1}"
PYTHON="${PYTHON:-python3}"
MAKE_CMD="${MAKE:-make}"
CHECK_ONLY=0

log() {
    printf '[bootstrap] %s\n' "$*"
}

die() {
    printf '[bootstrap] error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<USAGE
Usage: Interface_scripts/co-simulation/bootstrap.sh [--check-only]

Initializes the coupled SoftHier/3D-ICE environment:
  - initializes SoftHier and nested submodules;
  - applies/verifies SoftHier's own GVSOC/PULP patches;
  - verifies the runtime power-capture hook needed for co-simulation;
  - prepares the SoftHier environment;
  - builds or verifies 3D-ICE client/server binaries.

Environment overrides:
  SOFTHIER_DIR=$SOFTHIER_DIR
  DICE_DIR=$DICE_DIR
  DICE_BIN_DIR=$DICE_BIN_DIR
  BUILD_3DICE=$BUILD_3DICE
USAGE
}

while (($#)); do
    case "$1" in
        --check-only)
            CHECK_ONLY=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            die "unknown argument: $1"
            ;;
    esac
done

need_cmd() {
    command -v "$1" >/dev/null 2>&1
}

check_host_tools() {
    local missing=()
    local cmd

    for cmd in git make cmake gcc bison flex unzip awk tail "$PYTHON"; do
        if ! need_cmd "$cmd"; then
            missing+=("$cmd")
        fi
    done

    if ! need_cmd csh && ! need_cmd tcsh; then
        missing+=("csh or tcsh")
    fi

    if ((${#missing[@]})); then
        printf '[bootstrap] missing required commands: %s\n' "${missing[*]}" >&2
        printf '[bootstrap] Debian/Ubuntu packages usually needed:\n' >&2
        printf '  sudo apt-get install build-essential cmake bison flex libopenblas-dev csh unzip git python3 python3-venv python3-pip\n' >&2
        die "install missing host dependencies and rerun make bootstrap"
    fi
}

init_submodules() {
    log "Initializing SoftHier submodule"
    git -C "$ROOT_DIR" submodule update --init SoftHier

    log "Initializing 3D-ICE submodule"
    git -C "$ROOT_DIR" submodule update --init 3D-ICE

    log "Initializing nested SoftHier submodules"
    git -C "$SOFTHIER_DIR" submodule update --init --recursive
}

power_hook_present() {
    [[ -f "$SOFTHIER_DIR/core/engine/src/time/time_engine.cpp" ]] || return 1
    [[ -f "$SOFTHIER_DIR/core/engine/CMakeLists.txt" ]] || return 1

    grep -q "power_capture_advance" "$SOFTHIER_DIR/core/engine/src/time/time_engine.cpp" &&
        grep -q "PWR_INTERVAL_PS" "$SOFTHIER_DIR/core/engine/CMakeLists.txt" &&
        grep -q "SOFTHIER_ICE_POWER_TRACE_FILE" "$SOFTHIER_DIR/core/engine/CMakeLists.txt"
}

verify_power_hook() {
    if power_hook_present; then
        log "SoftHier runtime power hook is present"
        return 0
    fi

    return 1
}

apply_softhier_patches() {
    log "Applying SoftHier-provided GVSOC/PULP patches if needed"
    "$MAKE_CMD" -C "$SOFTHIER_DIR" drmasys_apply_patch
}

require_power_hook() {
    if verify_power_hook; then
        return 0
    fi

    die "SoftHier runtime power hook is missing after 'make -C SoftHier drmasys_apply_patch'. Check SoftHier/soft_hier/gvsoc_core.patch and make sure it applies cleanly to SoftHier/core."
}

prepare_softhier_env() {
    log "Preparing SoftHier environment"
    (
        cd "$SOFTHIER_DIR"
        set +u
        # shellcheck source=/dev/null
        source ./sourceme.sh
        set -u
    )
}

build_or_verify_3dice() {
    if [[ -x "$DICE_BIN_DIR/3D-ICE-Server" && -x "$DICE_BIN_DIR/3D-ICE-Client" ]]; then
        log "3D-ICE client/server binaries already exist in $DICE_BIN_DIR"
        return
    fi

    if [[ "$BUILD_3DICE" != "1" ]]; then
        die "3D-ICE binaries are missing and BUILD_3DICE=$BUILD_3DICE"
    fi

    log "Installing/building 3D-ICE client/server"
    SRC_DIR="$DICE_DIR" "$SCRIPT_DIR/3dice_client_server.sh" install

    [[ -x "$DICE_BIN_DIR/3D-ICE-Server" ]] || die "missing 3D-ICE server after build"
    [[ -x "$DICE_BIN_DIR/3D-ICE-Client" ]] || die "missing 3D-ICE client after build"
}

check_host_tools

if ((CHECK_ONLY)); then
    verify_power_hook || die "SoftHier runtime power hook is missing"
    log "Check complete"
    exit 0
fi

init_submodules
prepare_softhier_env
require_power_hook
build_or_verify_3dice

log "Bootstrap complete"
