#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

SOFTHIER_DIR="${SOFTHIER_DIR:-$ROOT_DIR/SoftHier}"
DICE_DIR="${DICE_DIR:-$ROOT_DIR/3D-ICE}"
DICE_BIN_DIR="${DICE_BIN_DIR:-$DICE_DIR/bin}"
RUN_NAME="${RUN_NAME:-default}"
RUN_ROOT="${RUN_ROOT:-$ROOT_DIR/runs}"
if [[ -z "${RUN_ID:-}" ]]; then
    if [[ -n "${RUN_DIR:-}" ]]; then
        RUN_ID="$(basename "$RUN_DIR")"
    else
        RUN_ID="$(date +%Y%m%d-%H%M%S)"
    fi
fi
RUN_DIR="${RUN_DIR:-$RUN_ROOT/$RUN_NAME/$RUN_ID}"
GENERATED_DIR="${GENERATED_DIR:-$RUN_DIR/generated}"
TRACE_DIR="${TRACE_DIR:-$RUN_DIR/traces}"
RESULT_DIR="${RESULT_DIR:-$RUN_DIR/results}"
STATE_DIR="${STATE_DIR:-$RUN_DIR/state}"
RUN_3DICE_GEN_DIR="${RUN_3DICE_GEN_DIR:-$GENERATED_DIR/3dice}"
RUN_3DICE_DIR="${RUN_3DICE_DIR:-$RESULT_DIR/3dice}"
LOG_DIR="${LOG_DIR:-$RUN_DIR/logs}"
PID_DIR="${PID_DIR:-$RUN_DIR/pids}"
CFG="${CFG:-$SOFTHIER_DIR/soft_hier/flex_cluster/flex_cluster_arch.py}"
APP="${APP:-}"
PLD="${PLD:-}"
PORT="${PORT:-54322}"
SERVER_HOST="${SERVER_HOST:-127.0.0.1}"
PWR_INTERVAL_PS="${PWR_INTERVAL_PS:-100000000}"
OTHERS_POWER="${OTHERS_POWER:-0.0}"
BUILD_SOFTHIER="${BUILD_SOFTHIER:-1}"
BUILD_3DICE="${BUILD_3DICE:-1}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"
EXIT_TIMEOUT="${EXIT_TIMEOUT:-120}"
PYTHON="${PYTHON:-python3}"
MAKE_CMD="${MAKE:-make}"
AUTO_BOOTSTRAP="${AUTO_BOOTSTRAP:-0}"

GEO_FILE="${GEO_FILE:-$GENERATED_DIR/geo.json}"
ICE_FLOORPLAN_FILE="${ICE_FLOORPLAN_FILE:-$RUN_3DICE_GEN_DIR/floorplan_nopower.flp}"
ICE_STK_FILE="${ICE_STK_FILE:-$RUN_3DICE_GEN_DIR/ice.stk}"
ICE_RUNTIME_FLOORPLAN_FILE="${ICE_RUNTIME_FLOORPLAN_FILE:-$RUN_3DICE_DIR/floorplan_nopower.flp}"
ICE_RUNTIME_STK_FILE="${ICE_RUNTIME_STK_FILE:-$RUN_3DICE_DIR/ice.stk}"
RAW_POWER_TRACE="${RAW_POWER_TRACE:-$TRACE_DIR/softhier_power_raw.txt}"
DICE_POWER_TRACE="${DICE_POWER_TRACE:-$TRACE_DIR/3dice_power_traces.txt}"
DONE_FILE="${DONE_FILE:-$STATE_DIR/softhier.done}"
MANIFEST_FILE="${MANIFEST_FILE:-$RUN_DIR/run.env}"
SUMMARY_FILE="${SUMMARY_FILE:-$RUN_DIR/summary.txt}"

SERVER_BIN="$DICE_BIN_DIR/3D-ICE-Server"
CLIENT_BIN="$DICE_BIN_DIR/3D-ICE-Client"

log() {
    printf '[coupled] %s\n' "$*"
}

die() {
    printf '[coupled] error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<USAGE
Usage: Interface_scripts/cosim/coupled_run.sh [run|status|stop]

Environment overrides:
  RUN_NAME=$RUN_NAME
  RUN_ID=$RUN_ID
  RUN_DIR=$RUN_DIR
  CFG=$CFG
  APP=$APP
  PLD=$PLD
  PORT=$PORT
  PWR_INTERVAL_PS=$PWR_INTERVAL_PS
  OTHERS_POWER=$OTHERS_POWER
  BUILD_SOFTHIER=$BUILD_SOFTHIER
  BUILD_3DICE=$BUILD_3DICE
USAGE
}

require_file() {
    local path="$1"
    [[ -f "$path" ]] || die "missing file: $path"
}

require_executable() {
    local path="$1"
    [[ -x "$path" ]] || die "missing executable: $path"
}

source_softhier_env() {
    set +u
    # shellcheck source=/dev/null
    source ./sourceme.sh
    set -u
}

pid_is_running() {
    local pid="$1"
    local state

    [[ -n "$pid" ]] || return 1
    state="$(ps -p "$pid" -o stat= 2>/dev/null || true)"
    [[ -n "$state" && "$state" != *Z* ]]
}

git_commit() {
    local dir="$1"
    git -C "$dir" rev-parse --short HEAD 2>/dev/null || printf 'unknown'
}

kv() {
    local key="$1"
    local value="$2"
    printf '%s=%q\n' "$key" "$value"
}

write_pid() {
    local name="$1"
    local pid="$2"
    printf '%s\n' "$pid" > "$PID_DIR/$name.pid"
}

read_pid() {
    local name="$1"
    local file="$PID_DIR/$name.pid"
    [[ -f "$file" ]] || return 1
    sed -n '1p' "$file"
}

make_dirs() {
    mkdir -p \
        "$RUN_DIR" \
        "$GENERATED_DIR" \
        "$TRACE_DIR" \
        "$RESULT_DIR" \
        "$STATE_DIR" \
        "$RUN_3DICE_GEN_DIR" \
        "$RUN_3DICE_DIR" \
        "$LOG_DIR" \
        "$PID_DIR"
}

write_latest_link() {
    local latest_dir="$RUN_ROOT/$RUN_NAME/latest"
    mkdir -p "$(dirname "$latest_dir")"
    ln -sfn "$RUN_DIR" "$latest_dir"
}

reset_runtime_files() {
    rm -f \
        "$RAW_POWER_TRACE" \
        "$DICE_POWER_TRACE" \
        "$DONE_FILE" \
        "$LOG_DIR/3dice_server.log" \
        "$LOG_DIR/3dice_client.log" \
        "$LOG_DIR/softhier.log" \
        "$LOG_DIR/adapter.log" \
        "$PID_DIR/3dice_server.pid" \
        "$PID_DIR/3dice_client.pid" \
        "$PID_DIR/adapter.pid" \
        "$PID_DIR/softhier.pid" \
        "$RUN_3DICE_DIR/output_top_die_flp_avg.txt" \
        "$RUN_3DICE_DIR/output_top_die_flp_max.txt" \
        "$RUN_3DICE_DIR/output_top_die_flp_min.txt" \
        "$RUN_3DICE_DIR/output_top_die_map.txt" \
        "$RUN_3DICE_DIR/output_top_die_map.coords.txt" \
        "$RUN_3DICE_DIR/thermal_map.txt" \
        "$SUMMARY_FILE"
}

build_or_verify_3dice() {
    if [[ "$BUILD_3DICE" == "1" && (! -x "$SERVER_BIN" || ! -x "$CLIENT_BIN") ]]; then
        log "Building 3D-ICE client/server binaries"
        SRC_DIR="$DICE_DIR" "$SCRIPT_DIR/3dice_client_server.sh" install
    fi

    require_executable "$SERVER_BIN"
    require_executable "$CLIENT_BIN"
}

generate_ice_inputs() {
    require_file "$CFG"
    log "Generating 3D-ICE inputs under $RUN_3DICE_GEN_DIR"
    "$MAKE_CMD" -C "$SOFTHIER_DIR" ice_prepare \
        cfg="$CFG" \
        ice_geo_file="$GEO_FILE" \
        ice_floorplan_file="$ICE_FLOORPLAN_FILE" \
        ice_stk_file="$ICE_STK_FILE"
}

prepare_runtime_3dice_inputs() {
    require_file "$ICE_FLOORPLAN_FILE"
    require_file "$ICE_STK_FILE"

    cp -f "$ICE_FLOORPLAN_FILE" "$ICE_RUNTIME_FLOORPLAN_FILE"
    cp -f "$ICE_STK_FILE" "$ICE_RUNTIME_STK_FILE"

    if [[ -f "$RUN_3DICE_GEN_DIR/conductance_layer.txt" ]]; then
        cp -f "$RUN_3DICE_GEN_DIR/conductance_layer.txt" "$RUN_3DICE_DIR/conductance_layer.txt"
    fi
}

write_manifest() {
    {
        kv RUN_CREATED_AT_LOCAL "$(date '+%Y-%m-%dT%H:%M:%S%z')"
        kv RUN_CREATED_AT_UTC "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        kv ROOT_DIR "$ROOT_DIR"
        kv RUN_NAME "$RUN_NAME"
        kv RUN_ID "$RUN_ID"
        kv RUN_DIR "$RUN_DIR"
        kv GENERATED_DIR "$GENERATED_DIR"
        kv TRACE_DIR "$TRACE_DIR"
        kv RESULT_DIR "$RESULT_DIR"
        kv LOG_DIR "$LOG_DIR"
        kv PID_DIR "$PID_DIR"
        kv CFG "$CFG"
        kv APP "$APP"
        kv PLD "$PLD"
        kv PORT "$PORT"
        kv SERVER_HOST "$SERVER_HOST"
        kv PWR_INTERVAL_PS "$PWR_INTERVAL_PS"
        kv OTHERS_POWER "$OTHERS_POWER"
        kv BUILD_SOFTHIER "$BUILD_SOFTHIER"
        kv BUILD_3DICE "$BUILD_3DICE"
        kv GEO_FILE "$GEO_FILE"
        kv ICE_FLOORPLAN_FILE "$ICE_FLOORPLAN_FILE"
        kv ICE_STK_FILE "$ICE_STK_FILE"
        kv ICE_RUNTIME_FLOORPLAN_FILE "$ICE_RUNTIME_FLOORPLAN_FILE"
        kv ICE_RUNTIME_STK_FILE "$ICE_RUNTIME_STK_FILE"
        kv RAW_POWER_TRACE "$RAW_POWER_TRACE"
        kv DICE_POWER_TRACE "$DICE_POWER_TRACE"
        kv ROOT_GIT_COMMIT "$(git_commit "$ROOT_DIR")"
        kv SOFTHIER_GIT_COMMIT "$(git_commit "$SOFTHIER_DIR")"
        kv SOFTHIER_CORE_GIT_COMMIT "$(git_commit "$SOFTHIER_DIR/core")"
        kv SOFTHIER_PULP_GIT_COMMIT "$(git_commit "$SOFTHIER_DIR/pulp")"
        kv DICE_GIT_COMMIT "$(git_commit "$DICE_DIR")"
    } > "$MANIFEST_FILE"
}

build_softhier() {
    if [[ "$BUILD_SOFTHIER" != "1" ]]; then
        log "Skipping SoftHier build because BUILD_SOFTHIER=$BUILD_SOFTHIER"
        log "Reusing a SoftHier build is only safe when it was built with RAW_POWER_TRACE=$RAW_POWER_TRACE"
        return
    fi

    local args=(
        "cfg=$CFG"
        "pwr_interval_ps=$PWR_INTERVAL_PS"
        "ice_geo_file=$GEO_FILE"
        "ice_power_trace_file=$RAW_POWER_TRACE"
    )

    if [[ -n "$APP" ]]; then
        args+=("app=$APP")
    fi

    log "Building SoftHier hardware/software"
    (
        cd "$SOFTHIER_DIR"
        source_softhier_env
        "$MAKE_CMD" hw sw "${args[@]}"
    )
}

start_server() {
    log "Starting 3D-ICE server on port $PORT"
    (
        cd "$RUN_3DICE_DIR"
        "$SERVER_BIN" "$(basename "$ICE_RUNTIME_STK_FILE")" "$PORT"
    ) > "$LOG_DIR/3dice_server.log" 2>&1 &

    local pid=$!
    write_pid 3dice_server "$pid"

    "$PYTHON" "$SCRIPT_DIR/wait_for_log.py" \
        --file "$LOG_DIR/3dice_server.log" \
        --pattern "Waiting for client" \
        --pid "$pid" \
        --timeout "$WAIT_TIMEOUT"
}

start_adapter() {
    log "Starting power trace adapter"
    "$PYTHON" "$SCRIPT_DIR/ice_trace_adapter.py" \
        --arch "$CFG" \
        --floorplan "$ICE_FLOORPLAN_FILE" \
        --input "$RAW_POWER_TRACE" \
        --output "$DICE_POWER_TRACE" \
        --done-file "$DONE_FILE" \
        --others-power "$OTHERS_POWER" \
        > "$LOG_DIR/adapter.log" 2>&1 &

    local pid=$!
    write_pid adapter "$pid"

    for _ in $(seq 1 100); do
        [[ -f "$DICE_POWER_TRACE" ]] && return 0
        pid_is_running "$pid" || die "adapter exited before creating $DICE_POWER_TRACE"
        sleep 0.1
    done

    die "adapter did not create $DICE_POWER_TRACE"
}

start_client() {
    log "Starting 3D-ICE client"
    (
        cd "$RUN_3DICE_DIR"
        "$CLIENT_BIN" "$SERVER_HOST" "$PORT" "$DICE_POWER_TRACE" \
            --follow --until-minus-one
    ) > "$LOG_DIR/3dice_client.log" 2>&1 &

    write_pid 3dice_client "$!"
}

run_softhier() {
    local args=()
    if [[ -n "$PLD" ]]; then
        args+=("pld=$PLD")
    fi

    log "Running SoftHier"
    (
        cd "$SOFTHIER_DIR"
        source_softhier_env
        "$MAKE_CMD" run "${args[@]}"
    ) > "$LOG_DIR/softhier.log" 2>&1 &

    local pid=$!
    write_pid softhier "$pid"

    set +e
    wait "$pid"
    local status=$?
    set -e

    : > "$DONE_FILE"
    log "SoftHier exited with status $status; signaled adapter with $DONE_FILE"

    return "$status"
}

wait_for_exit() {
    local name="$1"
    local pid="$2"
    local timeout="$3"

    for _ in $(seq 1 "$timeout"); do
        if ! pid_is_running "$pid"; then
            set +e
            wait "$pid"
            local status=$?
            set -e
            log "$name exited with status $status"
            return "$status"
        fi
        sleep 1
    done

    log "$name did not exit within ${timeout}s"
    return 124
}

stop_pid_name() {
    local name="$1"
    local pid

    pid="$(read_pid "$name" || true)"
    if [[ -z "$pid" ]]; then
        printf '%-16s no pid file\n' "$name"
        return 0
    fi

    if pid_is_running "$pid"; then
        printf '%-16s stopping pid %s\n' "$name" "$pid"
        kill "$pid" >/dev/null 2>&1 || true
    else
        printf '%-16s stale pid %s\n' "$name" "$pid"
    fi
}

status_pid_name() {
    local name="$1"
    local pid

    pid="$(read_pid "$name" || true)"
    if [[ -z "$pid" ]]; then
        printf '%-16s not started\n' "$name"
    elif pid_is_running "$pid"; then
        printf '%-16s running pid %s\n' "$name" "$pid"
    else
        printf '%-16s exited/stale pid %s\n' "$name" "$pid"
    fi
}

line_count() {
    local path="$1"
    if [[ -f "$path" ]]; then
        wc -l < "$path" | tr -d ' '
    else
        printf '0'
    fi
}

thermal_row_count() {
    local path="$1"
    if [[ -f "$path" ]]; then
        awk 'END { rows = NR - 2; if (rows < 0) rows = 0; print rows }' "$path"
    else
        printf '0'
    fi
}

max_temperature() {
    local path="$1"
    if [[ -f "$path" ]]; then
        awk 'FNR > 2 { for (i = 2; i <= NF; i++) if ($i + 0 > max) max = $i + 0 } END { if (max == "") print "n/a"; else printf "%.3f", max }' "$path"
    else
        printf 'n/a'
    fi
}

write_summary() {
    local softhier_status="$1"
    local adapter_status="$2"
    local client_status="$3"
    local server_status="$4"
    local thermal_file="$RUN_3DICE_DIR/output_top_die_flp_avg.txt"

    {
        printf '%s\n' 'Run summary'
        printf '%s\n' '==========='
        printf 'run_name: %s\n' "$RUN_NAME"
        printf 'run_id: %s\n' "$RUN_ID"
        printf 'run_dir: %s\n' "$RUN_DIR"
        printf 'cfg: %s\n' "$CFG"
        printf 'app: %s\n' "${APP:-<SoftHier default>}"
        printf 'pwr_interval_ps: %s\n' "$PWR_INTERVAL_PS"
        printf '\n'
        printf '%s\n' 'Process statuses'
        printf '%s\n' '----------------'
        printf 'softhier: %s\n' "$softhier_status"
        printf 'adapter: %s\n' "$adapter_status"
        printf '3dice_client: %s\n' "$client_status"
        printf '3dice_server: %s\n' "$server_status"
        printf '\n'
        printf '%s\n' 'Output counts'
        printf '%s\n' '-------------'
        printf 'softhier_raw_power_rows: %s\n' "$(line_count "$RAW_POWER_TRACE")"
        printf '3dice_power_trace_rows: %s\n' "$(line_count "$DICE_POWER_TRACE")"
        printf '3dice_thermal_rows: %s\n' "$(thermal_row_count "$thermal_file")"
        printf 'max_temperature_k: %s\n' "$(max_temperature "$thermal_file")"
        printf '\n'
        printf '%s\n' 'Key files'
        printf '%s\n' '---------'
        printf 'manifest: %s\n' "$MANIFEST_FILE"
        printf 'softHier raw power: %s\n' "$RAW_POWER_TRACE"
        printf '3D-ICE power trace: %s\n' "$DICE_POWER_TRACE"
        printf '3D-ICE average temperature: %s\n' "$thermal_file"
        printf 'logs: %s\n' "$LOG_DIR"
    } > "$SUMMARY_FILE"
}

run_all() {
    make_dirs
    write_latest_link
    reset_runtime_files
    write_manifest

    if [[ "$AUTO_BOOTSTRAP" == "1" ]]; then
        log "Running bootstrap because AUTO_BOOTSTRAP=1"
        ROOT_DIR="$ROOT_DIR" \
        SOFTHIER_DIR="$SOFTHIER_DIR" \
        DICE_DIR="$DICE_DIR" \
        DICE_BIN_DIR="$DICE_BIN_DIR" \
        BUILD_3DICE="$BUILD_3DICE" \
        PYTHON="$PYTHON" \
        "$SCRIPT_DIR/bootstrap.sh"
    fi

    build_or_verify_3dice
    generate_ice_inputs
    prepare_runtime_3dice_inputs
    build_softhier

    require_file "$ICE_RUNTIME_FLOORPLAN_FILE"
    require_file "$ICE_RUNTIME_STK_FILE"

    start_server
    start_adapter
    start_client

    local softhier_status=0
    run_softhier || softhier_status=$?

    local adapter_pid client_pid server_pid
    adapter_pid="$(read_pid adapter)"
    client_pid="$(read_pid 3dice_client)"
    server_pid="$(read_pid 3dice_server)"

    local adapter_status=0 client_status=0 server_status=0
    wait_for_exit adapter "$adapter_pid" "$EXIT_TIMEOUT" || adapter_status=$?
    wait_for_exit 3dice_client "$client_pid" "$EXIT_TIMEOUT" || client_status=$?
    wait_for_exit 3dice_server "$server_pid" "$EXIT_TIMEOUT" || server_status=$?

    write_summary "$softhier_status" "$adapter_status" "$client_status" "$server_status"

    log "Run directory: $RUN_DIR"
    log "Logs: $LOG_DIR"
    log "Summary: $SUMMARY_FILE"

    return "$softhier_status"
}

cmd="${1:-run}"

case "$cmd" in
    run)
        run_all
        ;;
    status)
        status_pid_name 3dice_server
        status_pid_name adapter
        status_pid_name 3dice_client
        status_pid_name softhier
        ;;
    stop)
        stop_pid_name softhier
        stop_pid_name 3dice_client
        stop_pid_name adapter
        stop_pid_name 3dice_server
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage
        die "unknown command: $cmd"
        ;;
esac
