#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${ROOT_DIR:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
SOFTHIER_DIR="${SOFTHIER_DIR:-$ROOT_DIR/SoftHier}"
SOFTHIER_SDK_DIR="${SOFTHIER_SDK_DIR:-$SOFTHIER_DIR/soft_hier_sdk}"
SOFTHIER_SDK_URL="${SOFTHIER_SDK_URL:-git@github.com:pulp-platform/softhier-sdk.git}"
SOFTHIER_SDK_COMMIT="${SOFTHIER_SDK_COMMIT:-1244fdbc34977aff5a6a10ead079053fb5d31d00}"
SOFTHIER_SDK_TOOLCHAIN_SOURCE="${SOFTHIER_SDK_TOOLCHAIN_SOURCE:-}"
SOFTHIER_WORKDIR="${SOFTHIER_WORKDIR:-$SOFTHIER_DIR/.power_interface}"
SOFTHIER_NATIVE_DEPS_DIR="${SOFTHIER_NATIVE_DEPS_DIR:-$SOFTHIER_WORKDIR/dependencies}"
SOFTHIER_SYSTEMC_HOME="${SOFTHIER_SYSTEMC_HOME:-$SOFTHIER_NATIVE_DEPS_DIR/systemc-install}"
SOFTHIER_DRAMSYS_HOME="${SOFTHIER_DRAMSYS_HOME:-$SOFTHIER_NATIVE_DEPS_DIR/dramsys-install}"
SOFTHIER_SYSTEMC_URL="${SOFTHIER_SYSTEMC_URL:-https://github.com/accellera-official/systemc.git}"
SOFTHIER_SYSTEMC_VERSION="${SOFTHIER_SYSTEMC_VERSION:-3.0.1}"
SOFTHIER_DRAMSYS_URL="${SOFTHIER_DRAMSYS_URL:-https://github.com/tukl-msd/DRAMSys.git}"
SOFTHIER_DRAMSYS_COMMIT="${SOFTHIER_DRAMSYS_COMMIT:-8565f18b869c26eab712e3bb6494c4d6ae5dd73f}"
SOFTHIER_DRAMSYS_CMAKE="${SOFTHIER_DRAMSYS_CMAKE:-}"
SOFTHIER_CMAKE_VERSION="${SOFTHIER_CMAKE_VERSION:-3.28.1}"
SOFTHIER_BOOTSTRAP_JOBS="${SOFTHIER_BOOTSTRAP_JOBS:-16}"
SOFTHIER_TARGET="${SOFTHIER_TARGET:-pulp.chips.soft_hier_old.flex_cluster}"
SOFTHIER_CORE_MODEL="${SOFTHIER_CORE_MODEL:-fast}"
SOFTHIER_CONDA_ENV="${SOFTHIER_CONDA_ENV:-py312}"
SOFTHIER_CCACHE_DIR="${SOFTHIER_CCACHE_DIR:-$SOFTHIER_WORKDIR/ccache}"
SIMULATOR_CONFIG="${SIMULATOR_CONFIG:-${CFG:-$SOFTHIER_SDK_DIR/examples/SoftHier/config/arch_NoC1024.py}}"
SIMULATOR_APP="${SIMULATOR_APP:-${APP:-}}"
SIMULATOR_PLATFORM="${SIMULATOR_PLATFORM:-${PLD:-}}"
POWER_INTERVAL_PS="${POWER_INTERVAL_PS:-${PWR_INTERVAL_PS:-100000000}}"
RAW_POWER_TRACE="${RAW_POWER_TRACE:-}"
SYSTEM_CONFIG_FILE="${SYSTEM_CONFIG_FILE:-}"
GEO_FILE="${GEO_FILE:-}"
DEFAULT_POWER_W="${DEFAULT_POWER_W:-${OTHERS_POWER:-0.0}}"
POWER_HOOK_EXECUTABLE="${POWER_HOOK_EXECUTABLE:-}"
POWER_HOOK_CONFIG_FILE="${POWER_HOOK_CONFIG_FILE:-}"
POWER_HOOK_REQUEST_FILE="${POWER_HOOK_REQUEST_FILE:-}"
POWER_HOOK_RESPONSE_FILE="${POWER_HOOK_RESPONSE_FILE:-}"
POWER_HOOK_TRACE_FILE="${POWER_HOOK_TRACE_FILE:-$RAW_POWER_TRACE}"
PYTHON="${PYTHON:-python3}"
MAKE_CMD="${MAKE:-make}"


log() {
    printf '[provider:softhier] %s\n' "$*"
}


die() {
    printf '[provider:softhier] error: %s\n' "$*" >&2
    exit 1
}


usage() {
    cat <<'USAGE'
Usage: provider.sh ACTION

Provider actions:
  name             Print the provider display name.
  default-config   Print the default simulator configuration path.
  bootstrap        Initialize SoftHier and pin the architecture SDK.
  check            Verify the decoupled GVSoC power-hook integration.
  export-system    Write SYSTEM_CONFIG_FILE using SIMULATOR_CONFIG.
  build            Build the configured simulator and workload.
  run              Run with the versioned power hook in the foreground.
  manifest         Print provider-specific run.env entries.

The provider pins softhier-sdk commit
1244fdbc34977aff5a6a10ead079053fb5d31d00 by default. Override
SOFTHIER_SDK_URL only to use a mirror of the same repository.

Bootstrap also prepares SystemC 3.0.1 and the patched DRAMSys source at commit
8565f18b869c26eab712e3bb6494c4d6ae5dd73f under the provider work directory.
USAGE
}


require_file() {
    [[ -f "$1" ]] || die "missing file: $1"
}


require_executable() {
    [[ -x "$1" ]] || die "missing executable: $1"
}


require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}


require_value() {
    local name="$1"
    local value="$2"
    [[ -n "$value" ]] || die "$name is required for this action"
}


git_commit() {
    git -C "$1" rev-parse --short HEAD 2>/dev/null || printf 'unknown'
}


git_branch() {
    git -C "$1" branch --show-current 2>/dev/null || true
}


kv() {
    printf '%s=%q\n' "$1" "$2"
}


source_environment() {
    mkdir -p "$SOFTHIER_CCACHE_DIR"
    export GVSOC_WORKDIR="$SOFTHIER_WORKDIR"
    export CCACHE_DIR="$SOFTHIER_CCACHE_DIR"
    export SYSTEMC_HOME="$SOFTHIER_SYSTEMC_HOME"
    export DRAMSYS_PATH="$SOFTHIER_DIR/add_dramsyslib_patches"

    if command -v gcc-14.2.0 >/dev/null 2>&1; then
        export CC=gcc-14.2.0
    fi
    if command -v g++-14.2.0 >/dev/null 2>&1; then
        export CXX=g++-14.2.0
    fi
    if command -v cmake-3.18.1 >/dev/null 2>&1; then
        export CMAKE=cmake-3.18.1
    fi

    set +u
    if command -v conda >/dev/null 2>&1; then
        local conda_hook
        conda_hook="$(conda shell.bash hook 2>/dev/null || true)"
        if [[ -n "$conda_hook" ]]; then
            eval "$conda_hook"
            conda activate "$SOFTHIER_CONDA_ENV" >/dev/null 2>&1 ||
                die "cannot activate conda environment $SOFTHIER_CONDA_ENV"
        fi
    fi

    # shellcheck source=/dev/null
    source "$SOFTHIER_DIR/sourceme.sh"
    # The SDK script installs its RISC-V toolchain on first use.
    # shellcheck source=/dev/null
    source "$SOFTHIER_SDK_DIR/sourceme.sh"
    set -u

    export PYTHONPATH="$SOFTHIER_SDK_DIR/utilities:${PYTHONPATH:-}"
    # The site environment defaults LIBRARY_PATH to GCC 11. Keep link-time
    # selection aligned with the GCC 14 compiler used for GVSoC and SystemC.
    export LIBRARY_PATH="/usr/pack/gcc-14.2.0-af/lib64:${LIBRARY_PATH:-}"
    export LD_LIBRARY_PATH="/usr/pack/gcc-14.2.0-af/lib64:$SOFTHIER_WORKDIR/install/lib:$SOFTHIER_SYSTEMC_HOME/lib64:$SOFTHIER_DRAMSYS_HOME:$SOFTHIER_DIR/third_party/systemc_install/lib64:$SOFTHIER_DIR/third_party/DRAMSys:${LD_LIBRARY_PATH:-}"
}


sdk_at_pinned_commit() {
    [[ -d "$SOFTHIER_SDK_DIR/.git" ]] || return 1
    [[ "$(git -C "$SOFTHIER_SDK_DIR" rev-parse HEAD 2>/dev/null || true)" == "$SOFTHIER_SDK_COMMIT" ]]
}


prepare_sdk() {
    local cloned=0
    if [[ ! -d "$SOFTHIER_SDK_DIR/.git" ]]; then
        [[ ! -e "$SOFTHIER_SDK_DIR" ]] ||
            die "$SOFTHIER_SDK_DIR exists but is not a Git repository"
        log "Cloning SoftHier SDK from $SOFTHIER_SDK_URL"
        git clone --no-checkout "$SOFTHIER_SDK_URL" "$SOFTHIER_SDK_DIR"
        cloned=1
    fi

    if ! git -C "$SOFTHIER_SDK_DIR" cat-file -e "$SOFTHIER_SDK_COMMIT^{commit}" 2>/dev/null; then
        log "Fetching pinned SoftHier SDK commit $SOFTHIER_SDK_COMMIT"
        git -C "$SOFTHIER_SDK_DIR" fetch origin "$SOFTHIER_SDK_COMMIT"
    fi

    if ((cloned)); then
        git -C "$SOFTHIER_SDK_DIR" checkout --detach "$SOFTHIER_SDK_COMMIT"
    elif ! sdk_at_pinned_commit; then
        if [[ -n "$(git -C "$SOFTHIER_SDK_DIR" status --porcelain)" ]]; then
            die "SoftHier SDK has local changes and is not at $SOFTHIER_SDK_COMMIT"
        fi
        git -C "$SOFTHIER_SDK_DIR" checkout --detach "$SOFTHIER_SDK_COMMIT"
    fi

    if [[ -n "$SOFTHIER_SDK_TOOLCHAIN_SOURCE" &&
          ! -e "$SOFTHIER_SDK_DIR/toolchain" ]]; then
        [[ -d "$SOFTHIER_SDK_TOOLCHAIN_SOURCE" ]] ||
            die "SOFTHIER_SDK_TOOLCHAIN_SOURCE is not a directory: $SOFTHIER_SDK_TOOLCHAIN_SOURCE"
        log "Reusing cached SoftHier toolchain from $SOFTHIER_SDK_TOOLCHAIN_SOURCE"
        ln -s "$SOFTHIER_SDK_TOOLCHAIN_SOURCE" "$SOFTHIER_SDK_DIR/toolchain"
    fi
}


prepare_native_cmake() {
    if [[ -n "$SOFTHIER_DRAMSYS_CMAKE" ]]; then
        require_executable "$SOFTHIER_DRAMSYS_CMAKE"
        PREPARED_NATIVE_CMAKE="$SOFTHIER_DRAMSYS_CMAKE"
        return
    fi

    local machine
    machine="$(uname -m)"
    [[ "$machine" == "x86_64" ]] ||
        die "automatic CMake bootstrap supports x86_64 only; set SOFTHIER_DRAMSYS_CMAKE"

    local tools_dir="$SOFTHIER_NATIVE_DEPS_DIR/tools"
    local dirname="cmake-$SOFTHIER_CMAKE_VERSION-linux-x86_64"
    local archive="$tools_dir/$dirname.tar.gz"
    local url="https://github.com/Kitware/CMake/releases/download/v$SOFTHIER_CMAKE_VERSION/$dirname.tar.gz"
    PREPARED_NATIVE_CMAKE="$tools_dir/$dirname/bin/cmake"

    if [[ ! -x "$PREPARED_NATIVE_CMAKE" ]]; then
        require_command curl
        require_command tar
        mkdir -p "$tools_dir"
        log "Downloading CMake $SOFTHIER_CMAKE_VERSION for native dependencies"
        curl -L --fail --retry 3 --output "$archive.part" "$url"
        mv "$archive.part" "$archive"
        tar -xzf "$archive" -C "$tools_dir"
    fi

    require_executable "$PREPARED_NATIVE_CMAKE"
}


systemc_present() {
    [[ -f "$SOFTHIER_SYSTEMC_HOME/include/systemc.h" &&
       -f "$SOFTHIER_SYSTEMC_HOME/lib64/libsystemc.so" ]]
}


prepare_systemc() {
    if systemc_present; then
        log "SystemC is ready at $SOFTHIER_SYSTEMC_HOME"
        return
    fi

    require_command git
    prepare_native_cmake

    local source_dir="$SOFTHIER_NATIVE_DEPS_DIR/src/systemc"
    local build_dir="$SOFTHIER_NATIVE_DEPS_DIR/build/systemc"
    local cloned=0
    if [[ ! -d "$source_dir/.git" ]]; then
        [[ ! -e "$source_dir" ]] ||
            die "$source_dir exists but is not a Git repository"
        mkdir -p "$(dirname "$source_dir")"
        log "Cloning SystemC from $SOFTHIER_SYSTEMC_URL"
        git clone --no-checkout "$SOFTHIER_SYSTEMC_URL" "$source_dir"
        cloned=1
    fi

    if ! git -C "$source_dir" cat-file -e "$SOFTHIER_SYSTEMC_VERSION^{commit}" 2>/dev/null; then
        log "Fetching SystemC $SOFTHIER_SYSTEMC_VERSION"
        git -C "$source_dir" fetch --tags origin "$SOFTHIER_SYSTEMC_VERSION"
    fi
    local current_commit
    current_commit="$(git -C "$source_dir" rev-parse HEAD 2>/dev/null || true)"
    if ((cloned)) || [[ "$current_commit" != \
        "$(git -C "$source_dir" rev-parse "$SOFTHIER_SYSTEMC_VERSION^{commit}")" ]]; then
        if ((!cloned)) && [[ -n "$(git -C "$source_dir" status --porcelain 2>/dev/null)" ]]; then
            die "SystemC source has local changes at an unexpected commit: $source_dir"
        fi
        git -C "$source_dir" checkout --detach "$SOFTHIER_SYSTEMC_VERSION"
    elif [[ -n "$(git -C "$source_dir" status --porcelain 2>/dev/null)" ]]; then
        die "SystemC source has local changes: $source_dir"
    fi

    log "Building SystemC $SOFTHIER_SYSTEMC_VERSION"
    "$PREPARED_NATIVE_CMAKE" \
        -S "$source_dir" \
        -B "$build_dir" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_CXX_STANDARD=17 \
        -DCMAKE_INSTALL_PREFIX="$SOFTHIER_SYSTEMC_HOME" \
        -DCMAKE_INSTALL_LIBDIR=lib64
    "$PREPARED_NATIVE_CMAKE" --build "$build_dir" \
        --parallel "$SOFTHIER_BOOTSTRAP_JOBS"
    "$PREPARED_NATIVE_CMAKE" --install "$build_dir"

    systemc_present ||
        die "SystemC build completed without the expected headers and library"
}


dramsys_present() {
    local library="$SOFTHIER_DRAMSYS_HOME/libDRAMSys_Simulator.so"
    [[ -f "$library" ]] || return 1

    if command -v ldd >/dev/null 2>&1; then
        env LD_LIBRARY_PATH="$SOFTHIER_SYSTEMC_HOME/lib64:$SOFTHIER_DRAMSYS_HOME:${LD_LIBRARY_PATH:-}" \
            ldd "$library" 2>/dev/null |
            grep -Fq "$SOFTHIER_SYSTEMC_HOME/lib64/libsystemc"
    else
        return 0
    fi
}


apply_dramsys_patch() {
    local source_dir="$1"
    local patch_file="$SOFTHIER_DIR/add_dramsyslib_patches/build_dynlib_from_github_dramsys5/patch"
    require_file "$patch_file"

    if git -C "$source_dir" apply --check "$patch_file" >/dev/null 2>&1; then
        git -C "$source_dir" apply "$patch_file"
    elif git -C "$source_dir" apply --reverse --check "$patch_file" >/dev/null 2>&1; then
        log "DRAMSys integration patch is already applied"
    else
        die "DRAMSys integration patch cannot be applied cleanly in $source_dir"
    fi
}


prepare_dramsys() {
    if dramsys_present; then
        log "DRAMSys is ready at $SOFTHIER_DRAMSYS_HOME"
        return
    fi

    require_command git
    prepare_native_cmake

    local source_dir="$SOFTHIER_NATIVE_DEPS_DIR/src/dramsys"
    local build_dir="$SOFTHIER_NATIVE_DEPS_DIR/build/dramsys"
    local cloned=0
    if [[ ! -d "$source_dir/.git" ]]; then
        [[ ! -e "$source_dir" ]] ||
            die "$source_dir exists but is not a Git repository"
        mkdir -p "$(dirname "$source_dir")"
        log "Cloning DRAMSys from $SOFTHIER_DRAMSYS_URL"
        git clone --no-checkout "$SOFTHIER_DRAMSYS_URL" "$source_dir"
        cloned=1
    fi

    if ! git -C "$source_dir" cat-file -e "$SOFTHIER_DRAMSYS_COMMIT^{commit}" 2>/dev/null; then
        log "Fetching DRAMSys commit $SOFTHIER_DRAMSYS_COMMIT"
        git -C "$source_dir" fetch origin "$SOFTHIER_DRAMSYS_COMMIT"
    fi

    local current_commit
    current_commit="$(git -C "$source_dir" rev-parse HEAD 2>/dev/null || true)"
    if ((cloned)) || [[ "$current_commit" != "$SOFTHIER_DRAMSYS_COMMIT" ]]; then
        if ((!cloned)) && [[ -n "$(git -C "$source_dir" status --porcelain 2>/dev/null)" ]]; then
            die "DRAMSys source has local changes at an unexpected commit: $source_dir"
        fi
        git -C "$source_dir" checkout --detach "$SOFTHIER_DRAMSYS_COMMIT"
    fi
    apply_dramsys_patch "$source_dir"

    log "Building patched DRAMSys commit $SOFTHIER_DRAMSYS_COMMIT"
    env \
        CC="$CC" \
        CXX="$CXX" \
        SYSTEMC_HOME="$SOFTHIER_SYSTEMC_HOME" \
        LD_LIBRARY_PATH="$SOFTHIER_SYSTEMC_HOME/lib64:${LD_LIBRARY_PATH:-}" \
        "$PREPARED_NATIVE_CMAKE" \
            -S "$source_dir" \
            -B "$build_dir" \
            -DCMAKE_BUILD_TYPE=Release \
            -DCMAKE_CXX_FLAGS=-fPIC \
            -DCMAKE_C_FLAGS=-fPIC \
            -DDRAMSYS_USE_DRAMPOWER=ON \
            -DDRAMSYS_USE_FETCH_CONTENT_SYSTEMC=OFF \
            -DSystemCLanguage_DIR="$SOFTHIER_SYSTEMC_HOME/lib64/cmake/SystemCLanguage"
    env \
        CCACHE_DIR="$SOFTHIER_CCACHE_DIR" \
        SYSTEMC_HOME="$SOFTHIER_SYSTEMC_HOME" \
        LD_LIBRARY_PATH="$SOFTHIER_SYSTEMC_HOME/lib64:${LD_LIBRARY_PATH:-}" \
        "$PREPARED_NATIVE_CMAKE" --build "$build_dir" \
            --target simulator \
            --parallel "$SOFTHIER_BOOTSTRAP_JOBS"

    local built_library="$build_dir/lib/libDRAMSys_Simulator.so"
    require_file "$built_library"
    mkdir -p "$SOFTHIER_DRAMSYS_HOME"
    cp "$built_library" "$SOFTHIER_DRAMSYS_HOME/libDRAMSys_Simulator.so"

    dramsys_present ||
        die "DRAMSys build does not resolve against $SOFTHIER_SYSTEMC_HOME"
}


prepare_native_dependencies() {
    prepare_systemc
    prepare_dramsys
}


power_hook_present() {
    [[ -f "$SOFTHIER_DIR/engine/engine/src/power/power_hook.cpp" ]] || return 1
    [[ -f "$SOFTHIER_DIR/engine/engine/include/vp/power/power_hook.hpp" ]] || return 1
    grep -q "gvsoc-power-hook" "$SOFTHIER_DIR/engine/engine/src/power/power_hook.cpp" &&
        grep -q "temperature_set_all" "$SOFTHIER_DIR/engine/engine/src/power/power_hook.cpp" &&
        grep -q "get_component_temperature" "$SOFTHIER_DIR/engine/engine/src/proxy.cpp"
}


check_provider() {
    require_file "$SOFTHIER_DIR/sourceme.sh"
    require_file "$SOFTHIER_DIR/Makefile"
    require_file "$SIMULATOR_CONFIG"
    require_file "$SOFTHIER_SDK_DIR/softhier_old.mk"
    require_file "$SOFTHIER_SYSTEMC_HOME/include/systemc.h"
    require_file "$SOFTHIER_SYSTEMC_HOME/lib64/libsystemc.so"
    require_file "$SOFTHIER_DRAMSYS_HOME/libDRAMSys_Simulator.so"
    dramsys_present ||
        die "DRAMSys does not resolve against provider-managed SystemC; rerun make bootstrap"
    sdk_at_pinned_commit ||
        die "SoftHier SDK is not pinned at $SOFTHIER_SDK_COMMIT"
    power_hook_present ||
        die "decoupled GVSoC power hook is missing from the engine submodule"
    grep -q "soft_hier_sdk/softhier_old.mk" "$SOFTHIER_DIR/Makefile" ||
        die "SoftHier integration branch does not include the pinned SDK makefile"
    log "SoftHier provider and power hook are ready"
}


bootstrap_provider() {
    log "Initializing SoftHier and nested submodules"
    if ! git -C "$SOFTHIER_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        git -C "$ROOT_DIR" submodule update --init SoftHier
    fi
    git -C "$SOFTHIER_DIR" submodule update --init --recursive
    prepare_sdk

    log "Preparing native SoftHier dependencies and SDK environment"
    (
        cd "$SOFTHIER_DIR"
        source_environment
        prepare_native_dependencies
    )
    check_provider
}


export_system() {
    require_file "$SIMULATOR_CONFIG"
    require_value SYSTEM_CONFIG_FILE "$SYSTEM_CONFIG_FILE"
    "$PYTHON" "$SCRIPT_DIR/export_system_config.py" \
        --arch "$SIMULATOR_CONFIG" \
        --output "$SYSTEM_CONFIG_FILE" \
        --default-power-w "$DEFAULT_POWER_W"
}


build_simulator() {
    check_provider
    mkdir -p "$SOFTHIER_WORKDIR"

    local args=(
        sh-old-hs
        "cfg=$SIMULATOR_CONFIG"
        "SOFTHIER_OLD_SW_BUILD=$SOFTHIER_WORKDIR/sw_build"
        "SOFTHIER_OLD_CORE_MODEL=$SOFTHIER_CORE_MODEL"
    )
    if [[ -n "$SIMULATOR_APP" ]]; then
        args+=("app=$SIMULATOR_APP")
    fi

    log "Building target $SOFTHIER_TARGET and workload"
    (
        cd "$SOFTHIER_DIR"
        source_environment
        "$MAKE_CMD" "${args[@]}"
    )
}


run_simulator() {
    check_provider
    require_value POWER_HOOK_EXECUTABLE "$POWER_HOOK_EXECUTABLE"
    require_value POWER_HOOK_CONFIG_FILE "$POWER_HOOK_CONFIG_FILE"
    require_value POWER_HOOK_REQUEST_FILE "$POWER_HOOK_REQUEST_FILE"
    require_value POWER_HOOK_RESPONSE_FILE "$POWER_HOOK_RESPONSE_FILE"
    require_value POWER_HOOK_TRACE_FILE "$POWER_HOOK_TRACE_FILE"
    require_executable "$POWER_HOOK_EXECUTABLE"
    require_file "$POWER_HOOK_CONFIG_FILE"
    require_executable "$SOFTHIER_WORKDIR/install/bin/gvsoc"
    require_file "$SOFTHIER_WORKDIR/sw_build/softhier.elf"

    local args=(
        "--target=$SOFTHIER_TARGET"
        "--binary" "$SOFTHIER_WORKDIR/sw_build/softhier.elf"
        "--core-model=$SOFTHIER_CORE_MODEL"
        "--power-hook-executable" "$POWER_HOOK_EXECUTABLE"
        "--power-hook-config" "$POWER_HOOK_CONFIG_FILE"
        "--power-hook-interval-ps" "$POWER_INTERVAL_PS"
        "--power-hook-request-file" "$POWER_HOOK_REQUEST_FILE"
        "--power-hook-response-file" "$POWER_HOOK_RESPONSE_FILE"
        "--power-hook-trace-file" "$POWER_HOOK_TRACE_FILE"
    )
    if [[ -n "$SIMULATOR_PLATFORM" ]]; then
        args+=("--preload" "$SIMULATOR_PLATFORM")
    fi
    args+=(run "--trace=/chip/cluster_0/redmule")

    log "Running target $SOFTHIER_TARGET with closed-loop thermal feedback"
    (
        cd "$SOFTHIER_DIR"
        source_environment
        "$SOFTHIER_WORKDIR/install/bin/gvsoc" "${args[@]}"
    )
}


write_manifest() {
    kv PROVIDER_NAME softhier
    kv SOFTHIER_DIR "$SOFTHIER_DIR"
    kv SOFTHIER_GIT_COMMIT "$(git_commit "$SOFTHIER_DIR")"
    kv SOFTHIER_GIT_BRANCH "$(git_branch "$SOFTHIER_DIR")"
    kv SOFTHIER_ENGINE_GIT_COMMIT "$(git_commit "$SOFTHIER_DIR/engine")"
    kv SOFTHIER_ENGINE_GIT_BRANCH "$(git_branch "$SOFTHIER_DIR/engine")"
    kv SOFTHIER_CORE_GIT_COMMIT "$(git_commit "$SOFTHIER_DIR/core")"
    kv SOFTHIER_PULP_GIT_COMMIT "$(git_commit "$SOFTHIER_DIR/pulp")"
    kv SOFTHIER_SDK_DIR "$SOFTHIER_SDK_DIR"
    kv SOFTHIER_SDK_GIT_COMMIT "$(git_commit "$SOFTHIER_SDK_DIR")"
    kv SOFTHIER_SDK_PIN "$SOFTHIER_SDK_COMMIT"
    kv SOFTHIER_WORKDIR "$SOFTHIER_WORKDIR"
    kv SOFTHIER_SYSTEMC_HOME "$SOFTHIER_SYSTEMC_HOME"
    kv SOFTHIER_SYSTEMC_VERSION "$SOFTHIER_SYSTEMC_VERSION"
    kv SOFTHIER_DRAMSYS_HOME "$SOFTHIER_DRAMSYS_HOME"
    kv SOFTHIER_DRAMSYS_COMMIT "$SOFTHIER_DRAMSYS_COMMIT"
    kv SOFTHIER_TARGET "$SOFTHIER_TARGET"
}


action="${1:-}"
case "$action" in
    name)
        printf 'SoftHier\n'
        ;;
    default-config)
        printf '%s\n' "$SOFTHIER_SDK_DIR/examples/SoftHier/config/arch_NoC1024.py"
        ;;
    bootstrap)
        bootstrap_provider
        ;;
    check)
        check_provider
        ;;
    export-system)
        export_system
        ;;
    build)
        build_simulator
        ;;
    run)
        run_simulator
        ;;
    manifest)
        write_manifest
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage
        die "unknown provider action: ${action:-<empty>}"
        ;;
esac
