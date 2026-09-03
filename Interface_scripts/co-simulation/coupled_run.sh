#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

SIMULATOR_PROVIDER="${SIMULATOR_PROVIDER:-}"
SIMULATOR_NAME="${SIMULATOR_NAME:-simulator}"
DICE_DIR="${DICE_DIR:-$ROOT_DIR/3D-ICE}"
DICE_BIN_DIR="${DICE_BIN_DIR:-$DICE_DIR/bin}"
GEOMETRY_SCRIPT_DIR="${GEOMETRY_SCRIPT_DIR:-$ROOT_DIR/Interface_scripts/geometry_generator}"
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
SIMULATOR_CONFIG="${SIMULATOR_CONFIG:-}"
SIMULATOR_APP="${SIMULATOR_APP:-}"
SIMULATOR_PLATFORM="${SIMULATOR_PLATFORM:-}"
PORT="${PORT:-54322}"
SERVER_HOST="${SERVER_HOST:-127.0.0.1}"
DICE_RUN_MODE="${DICE_RUN_MODE:-local-server}"
POWER_INTERVAL_PS="${POWER_INTERVAL_PS:-100000000}"
ICE_SLOT_SECONDS="${ICE_SLOT_SECONDS:-}"
ICE_STEP_SECONDS="${ICE_STEP_SECONDS:-}"
ICE_TARGET_TOP_DIE_CELLS="${ICE_TARGET_TOP_DIE_CELLS:-65536}"
DEFAULT_POWER_W="${DEFAULT_POWER_W:-}"
BUILD_SIMULATOR="${BUILD_SIMULATOR:-1}"
BUILD_3DICE="${BUILD_3DICE:-1}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"
EXIT_TIMEOUT="${EXIT_TIMEOUT:-120}"
SIMULATOR_LOG_TAIL_LINES="${SIMULATOR_LOG_TAIL_LINES:-5}"
PYTHON="${PYTHON:-python3}"
MAKE_CMD="${MAKE:-make}"
AUTO_BOOTSTRAP="${AUTO_BOOTSTRAP:-0}"
ICE_GENERATE_GIF="${ICE_GENERATE_GIF:-0}"
ICE_GIF_FILE="${ICE_GIF_FILE:-$RUN_3DICE_DIR/temperature_map.gif}"
ICE_GIF_STRIDE="${ICE_GIF_STRIDE:-1}"
ICE_GIF_WIDTH="${ICE_GIF_WIDTH:-1600}"
ICE_GIF_FPS="${ICE_GIF_FPS:-8}"
ICE_GIF_WRITER="${ICE_GIF_WRITER:-auto}"
if [[ -z "${ICE_GIF_PYTHON:-}" && -x "$ROOT_DIR/.venv_plot/bin/python" ]]; then
    ICE_GIF_PYTHON="$ROOT_DIR/.venv_plot/bin/python"
else
    ICE_GIF_PYTHON="${ICE_GIF_PYTHON:-$PYTHON}"
fi

SYSTEM_CONFIG_FILE="${SYSTEM_CONFIG_FILE:-$GENERATED_DIR/system_config.json}"
GEO_FILE="${GEO_FILE:-$GENERATED_DIR/geo.json}"
ICE_FLOORPLAN_FILE="${ICE_FLOORPLAN_FILE:-$RUN_3DICE_GEN_DIR/floorplan_nopower.flp}"
ICE_STK_FILE="${ICE_STK_FILE:-$RUN_3DICE_GEN_DIR/ice.stk}"
ICE_RUNTIME_FLOORPLAN_FILE="${ICE_RUNTIME_FLOORPLAN_FILE:-$RUN_3DICE_DIR/floorplan_nopower.flp}"
ICE_RUNTIME_STK_FILE="${ICE_RUNTIME_STK_FILE:-$RUN_3DICE_DIR/ice.stk}"
RAW_POWER_TRACE="${RAW_POWER_TRACE:-$TRACE_DIR/power_hook_trace.jsonl}"
DICE_POWER_TRACE="${DICE_POWER_TRACE:-$TRACE_DIR/3dice_power_traces.txt}"
DONE_FILE="${DONE_FILE:-$STATE_DIR/simulator.done}"
POWER_HOOK_EXECUTABLE="${POWER_HOOK_EXECUTABLE:-$SCRIPT_DIR/3dice_power_hook.py}"
POWER_HOOK_CONFIG_FILE="${POWER_HOOK_CONFIG_FILE:-$GENERATED_DIR/power_hook_config.json}"
POWER_HOOK_REQUEST_FILE="${POWER_HOOK_REQUEST_FILE:-$STATE_DIR/power_hook_request.json}"
POWER_HOOK_RESPONSE_FILE="${POWER_HOOK_RESPONSE_FILE:-$STATE_DIR/power_hook_response.json}"
POWER_HOOK_TRACE_FILE="${POWER_HOOK_TRACE_FILE:-$RAW_POWER_TRACE}"
COMPONENT_TEMPERATURE_TRACE="${COMPONENT_TEMPERATURE_TRACE:-$TRACE_DIR/component_temperatures.csv}"
THERMAL_FEEDBACK_FILE="${THERMAL_FEEDBACK_FILE:-$RUN_3DICE_DIR/output_top_die_flp_avg.txt}"
POWER_HOOK_POLL_SECONDS="${POWER_HOOK_POLL_SECONDS:-0.02}"
POWER_HOOK_TIMEOUT_SECONDS="${POWER_HOOK_TIMEOUT_SECONDS:-$WAIT_TIMEOUT}"
MANIFEST_FILE="${MANIFEST_FILE:-$RUN_DIR/run.env}"
SUMMARY_FILE="${SUMMARY_FILE:-$RUN_DIR/summary.txt}"
SIMULATOR_LOG_FILE="${SIMULATOR_LOG_FILE:-$LOG_DIR/simulator.log}"

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
Usage: Interface_scripts/co-simulation/coupled_run.sh [run|status|stop]

Environment overrides:
  RUN_NAME=$RUN_NAME
  RUN_ID=$RUN_ID
  RUN_DIR=$RUN_DIR
  SIMULATOR_PROVIDER=$SIMULATOR_PROVIDER
  SIMULATOR_CONFIG=$SIMULATOR_CONFIG
  SIMULATOR_APP=$SIMULATOR_APP
  SIMULATOR_PLATFORM=$SIMULATOR_PLATFORM
  PORT=$PORT
  DICE_RUN_MODE=$DICE_RUN_MODE
  POWER_INTERVAL_PS=$POWER_INTERVAL_PS
  ICE_SLOT_SECONDS=$ICE_SLOT_SECONDS
  ICE_STEP_SECONDS=$ICE_STEP_SECONDS
  ICE_TARGET_TOP_DIE_CELLS=$ICE_TARGET_TOP_DIE_CELLS
  DEFAULT_POWER_W=$DEFAULT_POWER_W
  BUILD_SIMULATOR=$BUILD_SIMULATOR
  BUILD_3DICE=$BUILD_3DICE
  SIMULATOR_LOG_TAIL_LINES=$SIMULATOR_LOG_TAIL_LINES
  ICE_GENERATE_GIF=$ICE_GENERATE_GIF
  ICE_GIF_FILE=$ICE_GIF_FILE
  ICE_GIF_STRIDE=$ICE_GIF_STRIDE
  ICE_GIF_WIDTH=$ICE_GIF_WIDTH
  ICE_GIF_FPS=$ICE_GIF_FPS
  ICE_GIF_WRITER=$ICE_GIF_WRITER
  ICE_GIF_PYTHON=$ICE_GIF_PYTHON
  POWER_HOOK_EXECUTABLE=$POWER_HOOK_EXECUTABLE
  POWER_HOOK_POLL_SECONDS=$POWER_HOOK_POLL_SECONDS
  POWER_HOOK_TIMEOUT_SECONDS=$POWER_HOOK_TIMEOUT_SECONDS
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

provider_command() {
    ROOT_DIR="$ROOT_DIR" \
    SOFTHIER_DIR="${SOFTHIER_DIR:-$ROOT_DIR/SoftHier}" \
    RUN_DIR="$RUN_DIR" \
    SIMULATOR_CONFIG="$SIMULATOR_CONFIG" \
    SIMULATOR_APP="$SIMULATOR_APP" \
    SIMULATOR_PLATFORM="$SIMULATOR_PLATFORM" \
    POWER_INTERVAL_PS="$POWER_INTERVAL_PS" \
    RAW_POWER_TRACE="$RAW_POWER_TRACE" \
    SYSTEM_CONFIG_FILE="$SYSTEM_CONFIG_FILE" \
    GEO_FILE="$GEO_FILE" \
    DEFAULT_POWER_W="$DEFAULT_POWER_W" \
    POWER_HOOK_EXECUTABLE="$POWER_HOOK_EXECUTABLE" \
    POWER_HOOK_CONFIG_FILE="$POWER_HOOK_CONFIG_FILE" \
    POWER_HOOK_REQUEST_FILE="$POWER_HOOK_REQUEST_FILE" \
    POWER_HOOK_RESPONSE_FILE="$POWER_HOOK_RESPONSE_FILE" \
    POWER_HOOK_TRACE_FILE="$POWER_HOOK_TRACE_FILE" \
    PYTHON="$PYTHON" \
    MAKE="$MAKE_CMD" \
        "$SIMULATOR_PROVIDER" "$@"
}

resolve_provider() {
    [[ -n "$SIMULATOR_PROVIDER" ]] || die "SIMULATOR_PROVIDER is required"
    require_executable "$SIMULATOR_PROVIDER"
    if [[ -z "$SIMULATOR_CONFIG" ]]; then
        SIMULATOR_CONFIG="$(provider_command default-config)"
    fi
    SIMULATOR_NAME="$(provider_command name)"
    [[ -n "$SIMULATOR_NAME" ]] || die "simulator provider returned an empty name"
}

kv() {
    local key="$1"
    local value="$2"
    printf '%s=%q\n' "$key" "$value"
}

format_seconds() {
    local value="$1"
    awk -v value="$value" 'BEGIN { printf "%.15g", value }'
}

effective_slot_seconds() {
    if [[ -n "$ICE_SLOT_SECONDS" ]]; then
        format_seconds "$ICE_SLOT_SECONDS"
    else
        awk -v ps="$POWER_INTERVAL_PS" 'BEGIN { printf "%.15g", ps * 1e-12 }'
    fi
}

effective_step_seconds() {
    if [[ -n "$ICE_STEP_SECONDS" ]]; then
        format_seconds "$ICE_STEP_SECONDS"
    else
        awk -v slot="$(effective_slot_seconds)" 'BEGIN { printf "%.15g", slot / 10.0 }'
    fi
}

nonnegative_integer() {
    [[ "$1" =~ ^[0-9]+$ ]]
}

validate_simulator_log_tail_lines() {
    nonnegative_integer "$SIMULATOR_LOG_TAIL_LINES" ||
        die "SIMULATOR_LOG_TAIL_LINES must be a non-negative integer"
}

validate_target_top_die_cells() {
    nonnegative_integer "$ICE_TARGET_TOP_DIE_CELLS" ||
        die "ICE_TARGET_TOP_DIE_CELLS must be a positive integer"
    [[ "$ICE_TARGET_TOP_DIE_CELLS" != "0" ]] ||
        die "ICE_TARGET_TOP_DIE_CELLS must be a positive integer"
}

validate_dice_run_mode() {
    case "$DICE_RUN_MODE" in
        local-server|client-server)
            ;;
        *)
            die "DICE_RUN_MODE must be local-server or client-server"
            ;;
    esac
}

positive_integer() {
    [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

positive_number() {
    [[ "$1" =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]] && awk -v value="$1" 'BEGIN { exit !(value > 0) }'
}

validate_gif_settings() {
    case "$ICE_GENERATE_GIF" in
        0|1)
            ;;
        *)
            die "ICE_GENERATE_GIF must be 0 or 1"
            ;;
    esac

    positive_integer "$ICE_GIF_STRIDE" ||
        die "ICE_GIF_STRIDE must be a positive integer"
    positive_integer "$ICE_GIF_WIDTH" ||
        die "ICE_GIF_WIDTH must be a positive integer"
    positive_number "$ICE_GIF_FPS" ||
        die "ICE_GIF_FPS must be a positive number"

    case "$ICE_GIF_WRITER" in
        auto|pillow|imagemagick)
            ;;
        *)
            die "ICE_GIF_WRITER must be auto, pillow, or imagemagick"
            ;;
    esac
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
        "$POWER_HOOK_CONFIG_FILE" \
        "$POWER_HOOK_REQUEST_FILE" \
        "$POWER_HOOK_RESPONSE_FILE" \
        "$POWER_HOOK_TRACE_FILE" \
        "$COMPONENT_TEMPERATURE_TRACE" \
        "$LOG_DIR/3dice_server.log" \
        "$LOG_DIR/3dice_client.log" \
        "$SIMULATOR_LOG_FILE" \
        "$LOG_DIR/adapter.log" \
        "$LOG_DIR/tmap_gif.log" \
        "$PID_DIR/3dice_server.pid" \
        "$PID_DIR/3dice_client.pid" \
        "$PID_DIR/adapter.pid" \
        "$PID_DIR/simulator.pid" \
        "$PID_DIR/simulator_log_tail.pid" \
        "$RUN_3DICE_DIR/output_top_die_flp_avg.txt" \
        "$RUN_3DICE_DIR/output_top_die_flp_max.txt" \
        "$RUN_3DICE_DIR/output_top_die_flp_min.txt" \
        "$RUN_3DICE_DIR/output_top_die.txt" \
        "$RUN_3DICE_DIR/xyaxis_TOP_DIE.txt" \
        "$RUN_3DICE_DIR/output_top_die_map.txt" \
        "$RUN_3DICE_DIR/output_top_die_map.coords.txt" \
        "$RUN_3DICE_DIR/thermal_map.txt" \
        "$ICE_GIF_FILE" \
        "$SUMMARY_FILE"
}

build_or_verify_3dice() {
    local need_build=0
    local need_client=0

    if [[ "$DICE_RUN_MODE" == "client-server" ]]; then
        need_client=1
    fi

    if [[ ! -x "$SERVER_BIN" ]]; then
        need_build=1
    fi

    if [[ "$need_client" == "1" && ! -x "$CLIENT_BIN" ]]; then
        need_build=1
    fi

    if [[ "$BUILD_3DICE" == "1" && "$need_build" == "1" ]]; then
        log "Building 3D-ICE binaries"
        SRC_DIR="$DICE_DIR" "$SCRIPT_DIR/3dice_client_server.sh" install
    fi

    require_executable "$SERVER_BIN"

    if [[ "$need_client" == "1" ]]; then
        require_executable "$CLIENT_BIN"
    fi
}

prepare_system_config() {
    log "Exporting system contract through $SIMULATOR_NAME provider"
    provider_command export-system
    require_file "$SYSTEM_CONFIG_FILE"
    "$PYTHON" "$ROOT_DIR/Interface_scripts/system_contract.py" "$SYSTEM_CONFIG_FILE"
}

generate_ice_inputs() {
    require_file "$SYSTEM_CONFIG_FILE"
    log "Generating 3D-ICE inputs under $RUN_3DICE_GEN_DIR"

    local args=(
        "$GEOMETRY_SCRIPT_DIR/generate_3dice_inputs.py"
        "--system-config" "$SYSTEM_CONFIG_FILE"
        "--geo" "$GEO_FILE"
        "--floorplan" "$ICE_FLOORPLAN_FILE"
        "--stk" "$ICE_STK_FILE"
        "--power-interval-ps" "$POWER_INTERVAL_PS"
        "--target-top-die-cells" "$ICE_TARGET_TOP_DIE_CELLS"
    )

    if [[ -n "$ICE_SLOT_SECONDS" ]]; then
        args+=("--slot-seconds" "$ICE_SLOT_SECONDS")
    fi

    if [[ -n "$ICE_STEP_SECONDS" ]]; then
        args+=("--step-seconds" "$ICE_STEP_SECONDS")
    fi

    "$PYTHON" "${args[@]}"
}

prepare_runtime_3dice_inputs() {
    require_file "$ICE_FLOORPLAN_FILE"
    require_file "$ICE_STK_FILE"

    cp -f "$ICE_FLOORPLAN_FILE" "$ICE_RUNTIME_FLOORPLAN_FILE"
    cp -f "$ICE_STK_FILE" "$ICE_RUNTIME_STK_FILE"

    if [[ -f "$RUN_3DICE_GEN_DIR/conductance_layer.txt" ]]; then
        cp -f "$RUN_3DICE_GEN_DIR/conductance_layer.txt" "$RUN_3DICE_DIR/conductance_layer.txt"
    fi

    # Both 3D-ICE follow modes need the file to exist before the first GVSoC
    # exchange appends a slot.
    : > "$DICE_POWER_TRACE"
}

prepare_power_hook_config() {
    require_executable "$POWER_HOOK_EXECUTABLE"
    require_file "$SYSTEM_CONFIG_FILE"
    require_file "$ICE_RUNTIME_FLOORPLAN_FILE"

    local args=(
        "$SCRIPT_DIR/generate_power_hook_config.py"
        "--output" "$POWER_HOOK_CONFIG_FILE"
        "--system-config" "$SYSTEM_CONFIG_FILE"
        "--floorplan" "$ICE_RUNTIME_FLOORPLAN_FILE"
        "--power-trace" "$DICE_POWER_TRACE"
        "--temperature-output" "$THERMAL_FEEDBACK_FILE"
        "--temperature-history" "$COMPONENT_TEMPERATURE_TRACE"
        "--poll-seconds" "$POWER_HOOK_POLL_SECONDS"
        "--timeout-seconds" "$POWER_HOOK_TIMEOUT_SECONDS"
    )
    if [[ -n "$DEFAULT_POWER_W" ]]; then
        args+=("--default-power-w" "$DEFAULT_POWER_W")
    fi
    "$PYTHON" "${args[@]}"
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
        kv SIMULATOR_PROVIDER "$SIMULATOR_PROVIDER"
        kv SIMULATOR_NAME "$SIMULATOR_NAME"
        kv SIMULATOR_CONFIG "$SIMULATOR_CONFIG"
        kv SIMULATOR_APP "$SIMULATOR_APP"
        kv SIMULATOR_PLATFORM "$SIMULATOR_PLATFORM"
        kv PORT "$PORT"
        kv SERVER_HOST "$SERVER_HOST"
        kv DICE_RUN_MODE "$DICE_RUN_MODE"
        kv ICE_TARGET_TOP_DIE_CELLS "$ICE_TARGET_TOP_DIE_CELLS"
        kv POWER_INTERVAL_PS "$POWER_INTERVAL_PS"
        kv ICE_SLOT_SECONDS "$ICE_SLOT_SECONDS"
        kv ICE_STEP_SECONDS "$ICE_STEP_SECONDS"
        kv EFFECTIVE_ICE_SLOT_SECONDS "$(effective_slot_seconds)"
        kv EFFECTIVE_ICE_STEP_SECONDS "$(effective_step_seconds)"
        kv DEFAULT_POWER_W "$DEFAULT_POWER_W"
        kv BUILD_SIMULATOR "$BUILD_SIMULATOR"
        kv BUILD_3DICE "$BUILD_3DICE"
        kv SIMULATOR_LOG_TAIL_LINES "$SIMULATOR_LOG_TAIL_LINES"
        kv ICE_GENERATE_GIF "$ICE_GENERATE_GIF"
        kv ICE_GIF_FILE "$ICE_GIF_FILE"
        kv ICE_GIF_STRIDE "$ICE_GIF_STRIDE"
        kv ICE_GIF_WIDTH "$ICE_GIF_WIDTH"
        kv ICE_GIF_FPS "$ICE_GIF_FPS"
        kv ICE_GIF_WRITER "$ICE_GIF_WRITER"
        kv ICE_GIF_PYTHON "$ICE_GIF_PYTHON"
        kv SYSTEM_CONFIG_FILE "$SYSTEM_CONFIG_FILE"
        kv GEO_FILE "$GEO_FILE"
        kv ICE_FLOORPLAN_FILE "$ICE_FLOORPLAN_FILE"
        kv ICE_STK_FILE "$ICE_STK_FILE"
        kv ICE_RUNTIME_FLOORPLAN_FILE "$ICE_RUNTIME_FLOORPLAN_FILE"
        kv ICE_RUNTIME_STK_FILE "$ICE_RUNTIME_STK_FILE"
        kv RAW_POWER_TRACE "$RAW_POWER_TRACE"
        kv DICE_POWER_TRACE "$DICE_POWER_TRACE"
        kv POWER_HOOK_EXECUTABLE "$POWER_HOOK_EXECUTABLE"
        kv POWER_HOOK_CONFIG_FILE "$POWER_HOOK_CONFIG_FILE"
        kv POWER_HOOK_REQUEST_FILE "$POWER_HOOK_REQUEST_FILE"
        kv POWER_HOOK_RESPONSE_FILE "$POWER_HOOK_RESPONSE_FILE"
        kv POWER_HOOK_TRACE_FILE "$POWER_HOOK_TRACE_FILE"
        kv COMPONENT_TEMPERATURE_TRACE "$COMPONENT_TEMPERATURE_TRACE"
        kv THERMAL_FEEDBACK_FILE "$THERMAL_FEEDBACK_FILE"
        kv POWER_HOOK_POLL_SECONDS "$POWER_HOOK_POLL_SECONDS"
        kv POWER_HOOK_TIMEOUT_SECONDS "$POWER_HOOK_TIMEOUT_SECONDS"
        kv GEOMETRY_SCRIPT_DIR "$GEOMETRY_SCRIPT_DIR"
        kv ROOT_GIT_COMMIT "$(git_commit "$ROOT_DIR")"
        kv DICE_GIT_COMMIT "$(git_commit "$DICE_DIR")"
        provider_command manifest
    } > "$MANIFEST_FILE"
}

build_simulator() {
    if [[ "$BUILD_SIMULATOR" != "1" ]]; then
        log "Skipping $SIMULATOR_NAME build because BUILD_SIMULATOR=$BUILD_SIMULATOR"
        log "The existing build must include the GVSoC power-hook interface"
        return
    fi

    log "Building $SIMULATOR_NAME through provider"
    provider_command build
}

start_simulator_log_tail() {
    local simulator_pid="$1"
    local logfile="$SIMULATOR_LOG_FILE"

    validate_simulator_log_tail_lines

    if ((SIMULATOR_LOG_TAIL_LINES == 0)); then
        return 0
    fi

    if [[ ! -t 1 ]]; then
        log "$SIMULATOR_NAME live log window disabled because stdout is not a terminal; full log: $logfile"
        return 0
    fi

    log "Showing latest $SIMULATOR_LOG_TAIL_LINES $SIMULATOR_NAME log line(s) in a fixed window; full log: $logfile"

    (
        lines="$SIMULATOR_LOG_TAIL_LINES"
        window_lines=$((lines + 2))
        printed=0
        green=$'\033[32m'
        reset=$'\033[0m'

        trap 'exit 0' INT TERM

        repeat_char() {
            local char="$1"
            local count="$2"
            local result=""
            local i

            for ((i = 0; i < count; i++)); do
                result+="$char"
            done
            printf '%s' "$result"
        }

        print_box_border() {
            local width="$1"
            local title="${2:-}"
            local inner_width=$((width - 2))
            local fill

            if [[ -n "$title" ]]; then
                title=" $title "
                title="${title:0:inner_width}"
                fill=$((inner_width - ${#title}))
                printf '\r\033[2K%s+%s%s+%s\n' \
                    "$green" \
                    "$title" \
                    "$(repeat_char '-' "$fill")" \
                    "$reset"
            else
                printf '\r\033[2K%s+%s+%s\n' \
                    "$green" \
                    "$(repeat_char '-' "$inner_width")" \
                    "$reset"
            fi
        }

        print_box_row() {
            local width="$1"
            local content_width=$((width - 4))
            local content="$2"

            content="${content//$'\r'/}"
            content="${content//$'\t'/    }"
            printf '\r\033[2K%s|%s %-*.*s %s|%s\n' \
                "$green" \
                "$reset" \
                "$content_width" \
                "$content_width" \
                "$content" \
                "$green" \
                "$reset"
        }

        render_window() {
            local columns i pad line width
            local recent=()

            columns="${COLUMNS:-}"
            if ! [[ "$columns" =~ ^[0-9]+$ ]]; then
                columns="$(tput cols 2>/dev/null || printf '120')"
            fi
            if ((columns < 20)); then
                columns=20
            fi
            width=$((columns - 1))
            if ((width > 120)); then
                width=120
            fi

            if ((printed)); then
                printf '\033[%dA' "$window_lines"
            fi

            print_box_border "$width" "$SIMULATOR_NAME live log: last $lines line(s)"

            if [[ -f "$logfile" ]]; then
                mapfile -t recent < <(tail -n "$lines" "$logfile" 2>/dev/null || true)
            fi

            pad=$((lines - ${#recent[@]}))
            for ((i = 0; i < pad; i++)); do
                print_box_row "$width" ""
            done

            for line in "${recent[@]}"; do
                print_box_row "$width" "$line"
            done

            print_box_border "$width"

            printed=1
        }

        while pid_is_running "$simulator_pid"; do
            render_window
            sleep 1
        done
        render_window
    ) &

    write_pid simulator_log_tail "$!"
}

stop_simulator_log_tail() {
    local pid

    pid="$(read_pid simulator_log_tail || true)"
    if [[ -n "$pid" && "$pid" != "$$" ]] && pid_is_running "$pid"; then
        kill "$pid" >/dev/null 2>&1 || true
        wait "$pid" 2>/dev/null || true
    fi
}

start_server() {
    local wait_pattern

    if [[ "$DICE_RUN_MODE" == "local-server" ]]; then
        log "Starting 3D-ICE server in local power-trace mode"
        (
            cd "$RUN_3DICE_DIR"
            "$SERVER_BIN" "$(basename "$ICE_RUNTIME_STK_FILE")" \
                --power-trace "$DICE_POWER_TRACE" \
                --follow \
                --until-minus-one
        ) > "$LOG_DIR/3dice_server.log" 2>&1 &
        wait_pattern="Running local power trace mode."
    else
        log "Starting 3D-ICE server on port $PORT"
        (
            cd "$RUN_3DICE_DIR"
            "$SERVER_BIN" "$(basename "$ICE_RUNTIME_STK_FILE")" "$PORT"
        ) > "$LOG_DIR/3dice_server.log" 2>&1 &
        wait_pattern="Waiting for client"
    fi

    local pid=$!
    write_pid 3dice_server "$pid"

    "$PYTHON" "$SCRIPT_DIR/wait_for_log.py" \
        --file "$LOG_DIR/3dice_server.log" \
        --pattern "$wait_pattern" \
        --pid "$pid" \
        --timeout "$WAIT_TIMEOUT"
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

run_simulator() {
    log "Running $SIMULATOR_NAME through provider"
    provider_command run > "$SIMULATOR_LOG_FILE" 2>&1 &

    local pid=$!
    write_pid simulator "$pid"
    start_simulator_log_tail "$pid"

    set +e
    wait "$pid"
    local status=$?
    set -e
    stop_simulator_log_tail

    : > "$DONE_FILE"
    log "$SIMULATOR_NAME exited with status $status"

    return "$status"
}

ensure_3dice_termination() {
    local width
    width="$(awk '/^[[:space:]]*[A-Za-z0-9_.-]+[[:space:]]*:[[:space:]]*$/ { count++ } END { print count + 0 }' "$ICE_RUNTIME_FLOORPLAN_FILE")"
    ((width > 0)) || die "cannot determine 3D-ICE floorplan width"

    if awk -v width="$width" '
        NF {
            fields = NF
            all_minus_one = 1
            for (field_index = 1; field_index <= NF; field_index++) {
                if (($field_index + 0) != -1) {
                    all_minus_one = 0
                }
            }
        }
        END { exit !(fields == width && all_minus_one) }
    ' "$DICE_POWER_TRACE"; then
        return
    fi

    log "Appending fallback 3D-ICE termination slot"
    awk -v width="$width" 'BEGIN {
        for (field_index = 1; field_index <= width; field_index++) {
            printf "%s-1", field_index == 1 ? "" : " "
        }
        printf "\n"
    }' >> "$DICE_POWER_TRACE"
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
        awk 'NF && $1 !~ /^%/ { rows++ } END { print rows + 0 }' "$path"
    else
        printf '0'
    fi
}

max_temperature() {
    local path="$1"
    if [[ -f "$path" ]]; then
        awk 'NF && $1 !~ /^%/ { for (i = 2; i <= NF; i++) { value = $i + 0; if (!seen || value > max) { max = value; seen = 1 } } } END { if (!seen) print "n/a"; else printf "%.3f", max }' "$path"
    else
        printf 'n/a'
    fi
}

generate_temperature_gif() {
    if [[ "$ICE_GENERATE_GIF" != "1" ]]; then
        return 0
    fi

    log "Generating temperature dashboard GIF at $ICE_GIF_FILE"
    mkdir -p "$(dirname "$ICE_GIF_FILE")"

    "$ICE_GIF_PYTHON" "$ROOT_DIR/Interface_scripts/plot_runtime_temperature_map/plot_runtime_tmap.py" \
        --coords "$RUN_3DICE_DIR/xyaxis_TOP_DIE.txt" \
        --map "$RUN_3DICE_DIR/output_top_die.txt" \
        --gif "$ICE_GIF_FILE" \
        --once \
        --gif-stride "$ICE_GIF_STRIDE" \
        --gif-width "$ICE_GIF_WIDTH" \
        --gif-fps "$ICE_GIF_FPS" \
        --gif-writer "$ICE_GIF_WRITER" \
        > "$LOG_DIR/tmap_gif.log" 2>&1
}

write_summary() {
    local simulator_status="$1"
    local client_status="$2"
    local server_status="$3"
    local gif_status="$4"
    local thermal_file="$THERMAL_FEEDBACK_FILE"

    {
        printf '%s\n' 'Run summary'
        printf '%s\n' '==========='
        printf 'run_name: %s\n' "$RUN_NAME"
        printf 'run_id: %s\n' "$RUN_ID"
        printf 'run_dir: %s\n' "$RUN_DIR"
        printf 'simulator_provider: %s\n' "$SIMULATOR_PROVIDER"
        printf 'simulator_name: %s\n' "$SIMULATOR_NAME"
        printf 'simulator_config: %s\n' "$SIMULATOR_CONFIG"
        printf 'simulator_app: %s\n' "${SIMULATOR_APP:-<provider default>}"
        printf 'power_interval_ps: %s\n' "$POWER_INTERVAL_PS"
        printf '3dice_mode: %s\n' "$DICE_RUN_MODE"
        printf '3dice_slot_seconds: %s\n' "$(effective_slot_seconds)"
        printf '3dice_step_seconds: %s\n' "$(effective_step_seconds)"
        printf '\n'
        printf '%s\n' 'Process statuses'
        printf '%s\n' '----------------'
        printf 'simulator: %s\n' "$simulator_status"
        printf 'power_hook: integrated with simulator\n'
        printf '3dice_client: %s\n' "$client_status"
        printf '3dice_server: %s\n' "$server_status"
        printf 'temperature_gif: %s\n' "$gif_status"
        printf '\n'
        printf '%s\n' 'Output counts'
        printf '%s\n' '-------------'
        printf 'gvsoc_power_hook_exchanges: %s\n' "$(line_count "$POWER_HOOK_TRACE_FILE")"
        printf 'component_temperature_csv_rows: %s\n' "$(line_count "$COMPONENT_TEMPERATURE_TRACE")"
        printf '3dice_power_trace_rows: %s\n' "$(line_count "$DICE_POWER_TRACE")"
        printf '3dice_tflp_rows: %s\n' "$(thermal_row_count "$thermal_file")"
        printf 'max_temperature_k: %s\n' "$(max_temperature "$thermal_file")"
        printf '\n'
        printf '%s\n' 'Key files'
        printf '%s\n' '---------'
        printf 'manifest: %s\n' "$MANIFEST_FILE"
        printf 'system contract: %s\n' "$SYSTEM_CONFIG_FILE"
        printf 'power-hook configuration: %s\n' "$POWER_HOOK_CONFIG_FILE"
        printf 'GVSoC power-hook trace: %s\n' "$POWER_HOOK_TRACE_FILE"
        printf 'component temperature history: %s\n' "$COMPONENT_TEMPERATURE_TRACE"
        printf '3D-ICE power trace: %s\n' "$DICE_POWER_TRACE"
        printf '3D-ICE floorplan temperatures: %s\n' "$thermal_file"
        printf '3D-ICE temperature map: %s\n' "$RUN_3DICE_DIR/output_top_die.txt"
        if [[ "$ICE_GENERATE_GIF" == "1" ]]; then
            printf 'temperature GIF: %s\n' "$ICE_GIF_FILE"
            printf 'temperature GIF log: %s\n' "$LOG_DIR/tmap_gif.log"
        fi
        printf 'logs: %s\n' "$LOG_DIR"
    } > "$SUMMARY_FILE"
}

run_all() {
    resolve_provider
    validate_simulator_log_tail_lines
    validate_target_top_die_cells
    validate_dice_run_mode
    validate_gif_settings
    make_dirs
    write_latest_link
    reset_runtime_files

    if [[ "$AUTO_BOOTSTRAP" == "1" ]]; then
        log "Running bootstrap because AUTO_BOOTSTRAP=1"
        ROOT_DIR="$ROOT_DIR" \
        SIMULATOR_PROVIDER="$SIMULATOR_PROVIDER" \
        SIMULATOR_CONFIG="$SIMULATOR_CONFIG" \
        DICE_DIR="$DICE_DIR" \
        DICE_BIN_DIR="$DICE_BIN_DIR" \
        BUILD_3DICE="$BUILD_3DICE" \
        PYTHON="$PYTHON" \
        "$SCRIPT_DIR/bootstrap.sh"
    fi

    build_or_verify_3dice
    prepare_system_config
    generate_ice_inputs
    prepare_runtime_3dice_inputs
    prepare_power_hook_config
    write_manifest
    build_simulator

    require_file "$ICE_RUNTIME_FLOORPLAN_FILE"
    require_file "$ICE_RUNTIME_STK_FILE"

    start_server

    if [[ "$DICE_RUN_MODE" == "client-server" ]]; then
        start_client
    fi

    local simulator_status=0
    run_simulator || simulator_status=$?
    ensure_3dice_termination

    local client_pid server_pid
    server_pid="$(read_pid 3dice_server)"

    local client_status=0 server_status=0

    if [[ "$DICE_RUN_MODE" == "client-server" ]]; then
        client_pid="$(read_pid 3dice_client)"
        wait_for_exit 3dice_client "$client_pid" "$EXIT_TIMEOUT" || client_status=$?
    else
        client_status="skipped"
    fi

    wait_for_exit 3dice_server "$server_pid" "$EXIT_TIMEOUT" || server_status=$?

    local gif_status="disabled"
    if [[ "$ICE_GENERATE_GIF" == "1" ]]; then
        gif_status=0
        generate_temperature_gif || gif_status=$?
    fi

    write_summary "$simulator_status" "$client_status" "$server_status" "$gif_status"

    log "Run directory: $RUN_DIR"
    log "Logs: $LOG_DIR"
    log "Summary: $SUMMARY_FILE"

    if [[ "$simulator_status" != "0" ]]; then
        return "$simulator_status"
    fi
    if [[ "$client_status" != "0" && "$client_status" != "skipped" ]]; then
        return "$client_status"
    fi
    if [[ "$server_status" != "0" ]]; then
        return "$server_status"
    fi
    if [[ "$gif_status" != "0" && "$gif_status" != "disabled" ]]; then
        return "$gif_status"
    fi

    return 0
}

cmd="${1:-run}"

case "$cmd" in
    run)
        run_all
        ;;
    status)
        status_pid_name 3dice_server
        status_pid_name 3dice_client
        status_pid_name simulator
        status_pid_name simulator_log_tail
        ;;
    stop)
        stop_pid_name simulator
        stop_pid_name simulator_log_tail
        stop_pid_name 3dice_client
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
