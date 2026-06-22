#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SRC_DIR="${SRC_DIR:-$ROOT_DIR/3D-ICE}"
PORT="${PORT:-54322}"
SLOTS="${SLOTS:-3}"
POWER_TRACE="${POWER_TRACE:-client_server_power_traces.txt}"
SERVER_STK="${SERVER_STK:-example_transient_test_server.stk}"
WITH_APT=0
WITH_PYTHON_REQS=0

usage() {
    cat <<USAGE
Usage: $0 [--with-apt] [--with-python-reqs] <command>

Commands:
  install   Initialize the 3D-ICE submodule if needed, build SuperLU_MT, build 3D-ICE, write demo trace
  build     Configure and build an initialized 3D-ICE tree
  demo      Run the localhost 3D-ICE client-server example
  status    Print detected paths and expected binaries
  clean     Run upstream make clean
  distclean Run upstream clean and remove local extracted SuperLU_MT build tree

Environment overrides:
  SRC_DIR=/path/to/3D-ICE
  CBLAS_INCLUDE_DIR=/path/containing/cblas.h
  OPENBLAS_LIB=/path/to/libopenblas.so.0
  PORT=54322
  SLOTS=3
USAGE
}

log() {
    printf '[3d-ice] %s\n' "$*"
}

die() {
    printf '[3d-ice] error: %s\n' "$*" >&2
    exit 1
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1
}

parse_args() {
    while (($#)); do
        case "$1" in
            --with-apt)
                WITH_APT=1
                shift
                ;;
            --with-python-reqs)
                WITH_PYTHON_REQS=1
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                CMD="$1"
                shift
                if (($#)); then
                    die "unexpected extra argument: $1"
                fi
                return
                ;;
        esac
    done
    CMD="install"
}

install_apt_deps() {
    if ((WITH_APT == 0)); then
        return
    fi

    if ! need_cmd apt-get; then
        die "--with-apt was requested, but apt-get is not available"
    fi

    log "Installing Debian/Ubuntu packages from the 3D-ICE guide"
    sudo apt-get update
    sudo apt-get install -y build-essential bison flex libopenblas-dev csh unzip git python3 python3-pip
}

check_deps() {
    local missing=()
    for cmd in git make gcc bison flex unzip; do
        if ! need_cmd "$cmd"; then
            missing+=("$cmd")
        fi
    done

    if ! need_cmd csh && ! need_cmd tcsh; then
        missing+=("csh")
    fi

    if ((${#missing[@]})); then
        printf '[3d-ice] missing commands: %s\n' "${missing[*]}" >&2
        printf '[3d-ice] On Debian/Ubuntu run:\n' >&2
        printf '  sudo apt-get install build-essential bison flex libopenblas-dev csh unzip git python3 python3-pip\n' >&2
        die "install missing dependencies, or rerun this script with --with-apt on Debian/Ubuntu"
    fi
}

source_tree_ready() {
    [[ -f "$SRC_DIR/Makefile" && -f "$SRC_DIR/makefile.def" ]]
}

init_source_tree() {
    if source_tree_ready; then
        log "Using 3D-ICE source tree: $SRC_DIR"
        return
    fi

    if [[ "$SRC_DIR" == "$ROOT_DIR/3D-ICE" ]]; then
        log "Initializing 3D-ICE submodule"
        git -C "$ROOT_DIR" submodule update --init 3D-ICE
    fi

    source_tree_ready || die "$SRC_DIR is not an initialized 3D-ICE source tree; expected Makefile and makefile.def"
}

find_cblas_include() {
    if [[ -n "${CBLAS_INCLUDE_DIR:-}" ]]; then
        [[ -f "$CBLAS_INCLUDE_DIR/cblas.h" ]] || die "CBLAS_INCLUDE_DIR does not contain cblas.h: $CBLAS_INCLUDE_DIR"
        printf '%s\n' "$CBLAS_INCLUDE_DIR"
        return
    fi

    local dir
    for dir in \
        /usr/include \
        /usr/include/cblas \
        /usr/include/openblas \
        /usr/include/x86_64-linux-gnu \
        /usr/local/include \
        /usr/local/include/openblas \
        /opt/OpenBLAS/include
    do
        if [[ -f "$dir/cblas.h" ]]; then
            printf '%s\n' "$dir"
            return
        fi
    done

    local found
    found="$(find /usr/include /usr/local/include /opt -maxdepth 5 -name cblas.h -print -quit 2>/dev/null || true)"
    if [[ -n "$found" ]]; then
        dirname "$found"
        return
    fi

    die "cannot find cblas.h; install libopenblas-dev or set CBLAS_INCLUDE_DIR"
}

find_openblas_lib() {
    if [[ -n "${OPENBLAS_LIB:-}" ]]; then
        [[ -e "$OPENBLAS_LIB" || "$OPENBLAS_LIB" == -* ]] || die "OPENBLAS_LIB does not exist: $OPENBLAS_LIB"
        printf '%s\n' "$OPENBLAS_LIB"
        return
    fi

    if need_cmd pkg-config && pkg-config --exists openblas; then
        pkg-config --libs openblas
        return
    fi

    local lib
    for lib in \
        /usr/lib64/libopenblas.so \
        /usr/lib64/libopenblas.so.* \
        /lib64/libopenblas.so \
        /lib64/libopenblas.so.* \
        /usr/lib/x86_64-linux-gnu/libopenblas.so \
        /usr/lib/x86_64-linux-gnu/libopenblas.so.* \
        /lib/x86_64-linux-gnu/libopenblas.so \
        /lib/x86_64-linux-gnu/libopenblas.so.* \
        /usr/local/lib/libopenblas.so \
        /usr/local/lib/libopenblas.so.*
    do
        if [[ -e "$lib" ]]; then
            printf '%s\n' "$lib"
            return
        fi
    done

    local found
    found="$(ldconfig -p 2>/dev/null | awk '/libopenblas\.so/ { print $NF; exit }' || true)"
    if [[ -n "$found" ]]; then
        printf '%s\n' "$found"
        return
    fi

    die "cannot find OpenBLAS; install libopenblas-dev or set OPENBLAS_LIB"
}

sed_escape() {
    printf '%s' "$1" | sed 's/[\/&]/\\&/g'
}

configure_3dice() {
    local cblas_include openblas_lib cblas_escaped openblas_escaped
    cblas_include="$(find_cblas_include)"
    openblas_lib="$(find_openblas_lib)"
    cblas_escaped="$(sed_escape "$cblas_include")"
    openblas_escaped="$(sed_escape "$openblas_lib")"

    log "Using CBLAS include directory: $cblas_include"
    log "Using OpenBLAS link flag: $openblas_lib"

    sed -i -E "s|^SLU_MAIN[[:space:]]*=.*|SLU_MAIN    = \$(3DICE_MAIN)/superlu_mt-\$(SLU_VERSION)|" "$SRC_DIR/makefile.def"
    sed -i -E "s|^SLU_LIBS[[:space:]]*=.*|SLU_LIBS    = -L\$(SLU_LIB) -lsuperlu_mt_OPENMP\$(PLAT) $openblas_escaped|" "$SRC_DIR/makefile.def"
    sed -i -E "s|^CINCLUDES[[:space:]]*=.*|CINCLUDES = -I\$(3DICE_INCLUDE) -I$cblas_escaped|" "$SRC_DIR/makefile.def"

    configure_superlu "$openblas_lib"
}

configure_superlu() {
    local openblas_lib="$1"
    local slu_dir="$SRC_DIR/superlu_mt-4.0.0"

    if [[ ! -d "$slu_dir" ]]; then
        log "Extracting SuperLU_MT 4.0.0"
        (cd "$SRC_DIR" && unzip -q superlu_mt-4.0.0.zip)
    fi

    cp "$slu_dir/MAKE_INC/make.linux.openmp" "$slu_dir/make.inc"
    sed -i 's/^BLASDEF.*/BLASDEF   = -DUSE_VENDOR_BLAS/' "$slu_dir/make.inc"
    sed -i 's/^CDEFS.*/CDEFS        = -DAdd_/' "$slu_dir/make.inc"
    sed -i -E "s|^BLASLIB[[:space:]]*=.*|BLASLIB = $(sed_escape "$openblas_lib")|" "$slu_dir/make.inc"
}

build_superlu() {
    log "Building SuperLU_MT"
    make -C "$SRC_DIR/superlu_mt-4.0.0"
}

build_3dice() {
    log "Building 3D-ICE"
    make -C "$SRC_DIR"
}

write_demo_power_trace() {
    local trace="$SRC_DIR/bin/$POWER_TRACE"
    log "Writing demo power trace: $trace"
    cat > "$trace" <<'TRACE'
0.10 0.35 0.45 0.12 0.75 0.40 0.20 0.08
0.12 0.42 0.50 0.15 0.80 0.45 0.25 0.10
0.09 0.30 0.38 0.11 0.70 0.36 0.22 0.07
TRACE
}

install_python_requirements() {
    if ((WITH_PYTHON_REQS == 0)); then
        return
    fi

    need_cmd python3 || die "python3 is required for --with-python-reqs"
    log "Installing optional Python requirements from requirements.txt"
    python3 -m pip install -r "$SRC_DIR/requirements.txt"
}

install_all() {
    install_apt_deps
    check_deps
    init_source_tree
    configure_3dice
    build_superlu
    build_3dice
    write_demo_power_trace
    install_python_requirements
    log "Install complete. Binaries are in $SRC_DIR/bin"
}

ensure_built() {
    [[ -x "$SRC_DIR/bin/3D-ICE-Server" ]] || install_all
    [[ -x "$SRC_DIR/bin/3D-ICE-Client" ]] || install_all
}

run_demo() {
    ensure_built
    write_demo_power_trace

    local bin_dir="$SRC_DIR/bin"
    local server_log="$ROOT_DIR/3dice_server.log"
    local client_log="$ROOT_DIR/3dice_client.log"
    local server_pid

    log "Starting server on 127.0.0.1:$PORT"
    (
        cd "$bin_dir"
        ./3D-ICE-Server "$SERVER_STK" "$PORT"
    ) >"$server_log" 2>&1 &
    server_pid=$!

    cleanup_server() {
        if kill -0 "$server_pid" >/dev/null 2>&1; then
            kill "$server_pid" >/dev/null 2>&1 || true
        fi
    }
    trap cleanup_server EXIT

    local ready=0
    for _ in $(seq 1 120); do
        if grep -q 'Waiting for client' "$server_log"; then
            ready=1
            break
        fi
        if ! kill -0 "$server_pid" >/dev/null 2>&1; then
            printf '%s\n' "---- server log ----"
            cat "$server_log"
            die "server exited before accepting a client"
        fi
        sleep 1
    done

    ((ready == 1)) || die "server did not become ready; see $server_log"

    log "Running client for $SLOTS slot(s)"
    (
        cd "$bin_dir"
        ./3D-ICE-Client "$SLOTS" 127.0.0.1 "$PORT" "$POWER_TRACE"
    ) >"$client_log" 2>&1

    wait "$server_pid"
    trap - EXIT

    log "Client-server demo complete"
    log "Server log: $server_log"
    log "Client log: $client_log"
    log "Generated outputs are in $bin_dir"
}

status() {
    printf 'ROOT_DIR=%s\n' "$ROOT_DIR"
    printf 'SRC_DIR=%s\n' "$SRC_DIR"
    printf 'PORT=%s\n' "$PORT"
    printf 'SLOTS=%s\n' "$SLOTS"
    if source_tree_ready; then
        printf '3D-ICE source: initialized\n'
        printf 'Server binary: %s\n' "$SRC_DIR/bin/3D-ICE-Server"
        printf 'Client binary: %s\n' "$SRC_DIR/bin/3D-ICE-Client"
    else
        printf '3D-ICE source: missing or not initialized\n'
    fi
}

clean() {
    if source_tree_ready; then
        make -C "$SRC_DIR" clean
    fi
}

distclean() {
    clean
    rm -rf "$SRC_DIR/superlu_mt-4.0.0"
}

parse_args "$@"

case "$CMD" in
    install)
        install_all
        ;;
    build)
        check_deps
        init_source_tree
        configure_3dice
        build_superlu
        build_3dice
        write_demo_power_trace
        ;;
    demo)
        run_demo
        ;;
    status)
        status
        ;;
    clean)
        clean
        ;;
    distclean)
        distclean
        ;;
    *)
        usage
        die "unknown command: $CMD"
        ;;
esac
