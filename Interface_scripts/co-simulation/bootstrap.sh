#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${ROOT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
SIMULATOR_PROVIDER="${SIMULATOR_PROVIDER:-}"
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

Initializes the simulator/3D-ICE environment through a provider boundary:
  - asks SIMULATOR_PROVIDER to initialize and verify its simulator;
  - initializes the 3D-ICE submodule;
  - builds or verifies the 3D-ICE client/server binaries.

Environment overrides:
  SIMULATOR_PROVIDER=$SIMULATOR_PROVIDER
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


run_provider() {
    ROOT_DIR="$ROOT_DIR" \
    PYTHON="$PYTHON" \
    MAKE="$MAKE_CMD" \
        "$SIMULATOR_PROVIDER" "$@"
}


init_3dice() {
    log "Initializing 3D-ICE submodule"
    git -C "$ROOT_DIR" submodule update --init 3D-ICE
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
    [[ -x "$DICE_BIN_DIR/3D-ICE-Server" ]] ||
        die "missing 3D-ICE server after build"
    [[ -x "$DICE_BIN_DIR/3D-ICE-Client" ]] ||
        die "missing 3D-ICE client after build"
}


check_host_tools
[[ -n "$SIMULATOR_PROVIDER" ]] ||
    die "SIMULATOR_PROVIDER is required"
[[ -x "$SIMULATOR_PROVIDER" ]] ||
    die "simulator provider is not executable: $SIMULATOR_PROVIDER"

if ((CHECK_ONLY)); then
    run_provider check
    log "Provider check complete"
    exit 0
fi

init_3dice
run_provider bootstrap
build_or_verify_3dice
log "Bootstrap complete"
