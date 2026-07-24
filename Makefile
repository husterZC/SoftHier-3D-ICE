SHELL := /usr/bin/env bash

ROOT_DIR := $(abspath .)
SOFTHIER_DIR ?= $(ROOT_DIR)/SoftHier
DICE_DIR ?= $(ROOT_DIR)/3D-ICE
DICE_BIN_DIR ?= $(DICE_DIR)/bin
SIMULATOR_PROVIDER ?= $(ROOT_DIR)/Interface_scripts/providers/softhier/provider.sh
CO_SIMULATION_SCRIPT_DIR ?= $(ROOT_DIR)/Interface_scripts/co-simulation
GEOMETRY_SCRIPT_DIR ?= $(ROOT_DIR)/Interface_scripts/geometry_generator

RUN_NAME ?= default
RUN_ROOT ?= $(ROOT_DIR)/runs
ifeq ($(origin RUN_ID),undefined)
ifneq ($(origin RUN_DIR),undefined)
RUN_ID := $(notdir $(abspath $(RUN_DIR)))
else
RUN_ID := $(shell date +%Y%m%d-%H%M%S)
endif
endif

LATEST_RUN_DIR ?= $(RUN_ROOT)/$(RUN_NAME)/latest
LATEST_GOALS := coupled-status coupled-stop clean-latest latest-run
ifeq ($(filter $(LATEST_GOALS),$(MAKECMDGOALS)),)
RUN_DIR ?= $(RUN_ROOT)/$(RUN_NAME)/$(RUN_ID)
else
RUN_DIR ?= $(LATEST_RUN_DIR)
endif

GENERATED_DIR ?= $(RUN_DIR)/generated
TRACE_DIR ?= $(RUN_DIR)/traces
RESULT_DIR ?= $(RUN_DIR)/results
STATE_DIR ?= $(RUN_DIR)/state
RUN_3DICE_GEN_DIR ?= $(GENERATED_DIR)/3dice
RUN_3DICE_DIR ?= $(RESULT_DIR)/3dice
LOG_DIR ?= $(RUN_DIR)/logs
PID_DIR ?= $(RUN_DIR)/pids

CFG ?=
APP ?=
PLD ?=
SIMULATOR_CONFIG ?= $(CFG)
SIMULATOR_APP ?= $(APP)
SIMULATOR_PLATFORM ?= $(PLD)
PORT ?= 54322
SERVER_HOST ?= 127.0.0.1
DICE_RUN_MODE ?= local-server
PWR_INTERVAL_PS ?= 100000000
POWER_INTERVAL_PS ?= $(PWR_INTERVAL_PS)
ICE_SLOT_SECONDS ?=
ICE_STEP_SECONDS ?=
ICE_TARGET_TOP_DIE_CELLS ?= 65536
OTHERS_POWER ?=
DEFAULT_POWER_W ?= $(OTHERS_POWER)
BUILD_SOFTHIER ?= 1
BUILD_SIMULATOR ?= $(BUILD_SOFTHIER)
BUILD_3DICE ?= 1
WAIT_TIMEOUT ?= 180
EXIT_TIMEOUT ?= 120
SOFTHIER_LOG_TAIL_LINES ?= 5
SIMULATOR_LOG_TAIL_LINES ?= $(SOFTHIER_LOG_TAIL_LINES)
PYTHON ?= python3
AUTO_BOOTSTRAP ?= 0
ICE_GENERATE_GIF ?= 0
ICE_GIF_FILE ?= $(RUN_3DICE_DIR)/temperature_map.gif
ICE_GIF_STRIDE ?= 1
ICE_GIF_WIDTH ?= 1600
ICE_GIF_FPS ?= 8
ICE_GIF_WRITER ?= auto
ICE_GIF_PYTHON ?=

SYSTEM_CONFIG_FILE ?= $(GENERATED_DIR)/system_config.json
GEO_FILE ?= $(GENERATED_DIR)/geo.json
ICE_FLOORPLAN_FILE ?= $(RUN_3DICE_GEN_DIR)/floorplan_nopower.flp
ICE_STK_FILE ?= $(RUN_3DICE_GEN_DIR)/ice.stk
ICE_RUNTIME_FLOORPLAN_FILE ?= $(RUN_3DICE_DIR)/floorplan_nopower.flp
ICE_RUNTIME_STK_FILE ?= $(RUN_3DICE_DIR)/ice.stk
RAW_POWER_TRACE ?= $(TRACE_DIR)/power_hook_trace.jsonl
DICE_POWER_TRACE ?= $(TRACE_DIR)/3dice_power_traces.txt
DONE_FILE ?= $(STATE_DIR)/simulator.done
POWER_HOOK_EXECUTABLE ?= $(CO_SIMULATION_SCRIPT_DIR)/3dice_power_hook.py
POWER_HOOK_CONFIG_FILE ?= $(GENERATED_DIR)/power_hook_config.json
POWER_HOOK_REQUEST_FILE ?= $(STATE_DIR)/power_hook_request.json
POWER_HOOK_RESPONSE_FILE ?= $(STATE_DIR)/power_hook_response.json
POWER_HOOK_TRACE_FILE ?= $(RAW_POWER_TRACE)
COMPONENT_TEMPERATURE_TRACE ?= $(TRACE_DIR)/component_temperatures.csv
THERMAL_FEEDBACK_FILE ?= $(RUN_3DICE_DIR)/output_top_die_flp_avg.txt
POWER_HOOK_POLL_SECONDS ?= 0.02
POWER_HOOK_TIMEOUT_SECONDS ?= $(WAIT_TIMEOUT)

.PHONY: help bootstrap co-simulation dirs 3dice-build simulator-init simulator-check simulator-build softhier-init softhier-power-check softhier-build ice-inputs coupled-run coupled-status coupled-stop clean-run clean-latest clean-runs list-runs latest-run adapter-smoke interface-tests

help:
	@printf '%s\n' \
		'Simulator/3D-ICE root orchestration targets:' \
		'  make bootstrap       First-time provider and 3D-ICE setup' \
		'  make co-simulation   Run bootstrap, then a timestamped coupled simulation' \
		'  make 3dice-build      Build or verify 3D-ICE client/server binaries' \
		'  make simulator-init   Initialize the selected simulator provider' \
		'  make simulator-check  Verify the selected provider' \
		'  make ice-inputs       Generate run-local 3D-ICE geo/floorplan/stk files' \
		'  make simulator-build  Build through the selected provider' \
		'  make coupled-run      Run the localhost coupled simulation' \
		'  make interface-tests  Validate the neutral interface contract' \
		'  make coupled-status   Show recorded process status' \
		'  make coupled-stop     Stop recorded coupled-run processes' \
		'  make list-runs        List timestamped run directories' \
		'  make latest-run       Print the latest run path for RUN_NAME' \
		'  make clean-run        Remove the selected RUN_DIR' \
		'  make clean-latest     Remove runs/RUN_NAME/latest target' \
		'  make clean-runs       Remove all root run directories' \
		'' \
		'Common overrides:' \
		'  RUN_NAME=$(RUN_NAME)' \
		'  RUN_ID=$(RUN_ID)' \
		'  SIMULATOR_PROVIDER=$(SIMULATOR_PROVIDER)' \
		'  SIMULATOR_CONFIG=$(SIMULATOR_CONFIG)' \
		'  SIMULATOR_APP=$(SIMULATOR_APP)' \
		'  PORT=$(PORT)' \
		'  DICE_RUN_MODE=$(DICE_RUN_MODE)' \
		'  POWER_INTERVAL_PS=$(POWER_INTERVAL_PS)' \
		'  ICE_SLOT_SECONDS=$(ICE_SLOT_SECONDS)' \
		'  ICE_STEP_SECONDS=$(ICE_STEP_SECONDS)' \
		'  ICE_TARGET_TOP_DIE_CELLS=$(ICE_TARGET_TOP_DIE_CELLS)' \
		'  DEFAULT_POWER_W=$(DEFAULT_POWER_W)' \
		'  ICE_GENERATE_GIF=$(ICE_GENERATE_GIF)' \
		'  ICE_GIF_FILE=$(ICE_GIF_FILE)' \
		'  ICE_GIF_STRIDE=$(ICE_GIF_STRIDE)' \
		'  ICE_GIF_PYTHON=$(ICE_GIF_PYTHON)' \
		'  BUILD_SIMULATOR=$(BUILD_SIMULATOR)' \
		'  SIMULATOR_LOG_TAIL_LINES=$(SIMULATOR_LOG_TAIL_LINES)' \
		'  Legacy SoftHier variables remain compatibility aliases.' \
		'' \
		'More detail: co-simulation.md'

bootstrap:
	@ROOT_DIR="$(ROOT_DIR)" \
	SIMULATOR_PROVIDER="$(SIMULATOR_PROVIDER)" \
	SIMULATOR_CONFIG="$(SIMULATOR_CONFIG)" \
	SIMULATOR_APP="$(SIMULATOR_APP)" \
	SIMULATOR_PLATFORM="$(SIMULATOR_PLATFORM)" \
	POWER_INTERVAL_PS="$(POWER_INTERVAL_PS)" \
	SOFTHIER_DIR="$(SOFTHIER_DIR)" \
	DICE_DIR="$(DICE_DIR)" \
	DICE_BIN_DIR="$(DICE_BIN_DIR)" \
	BUILD_3DICE="$(BUILD_3DICE)" \
	PYTHON="$(PYTHON)" \
	"$(CO_SIMULATION_SCRIPT_DIR)/bootstrap.sh"

co-simulation: bootstrap coupled-run

dirs:
	mkdir -p "$(GENERATED_DIR)" "$(TRACE_DIR)" "$(RESULT_DIR)" "$(STATE_DIR)" "$(RUN_3DICE_GEN_DIR)" "$(RUN_3DICE_DIR)" "$(LOG_DIR)" "$(PID_DIR)"

3dice-build:
	@if [[ "$(BUILD_3DICE)" == "1" && ( ! -x "$(DICE_BIN_DIR)/3D-ICE-Server" || ! -x "$(DICE_BIN_DIR)/3D-ICE-Client" ) ]]; then \
		SRC_DIR="$(DICE_DIR)" "$(CO_SIMULATION_SCRIPT_DIR)/3dice_client_server.sh" install; \
	fi
	@test -x "$(DICE_BIN_DIR)/3D-ICE-Server"
	@test -x "$(DICE_BIN_DIR)/3D-ICE-Client"
	@printf '3D-ICE server/client are available in %s\n' "$(DICE_BIN_DIR)"

simulator-init:
	@ROOT_DIR="$(ROOT_DIR)" SOFTHIER_DIR="$(SOFTHIER_DIR)" \
	SIMULATOR_CONFIG="$(SIMULATOR_CONFIG)" PYTHON="$(PYTHON)" \
	"$(SIMULATOR_PROVIDER)" bootstrap

simulator-check:
	@ROOT_DIR="$(ROOT_DIR)" SOFTHIER_DIR="$(SOFTHIER_DIR)" \
	SIMULATOR_CONFIG="$(SIMULATOR_CONFIG)" PYTHON="$(PYTHON)" \
	"$(SIMULATOR_PROVIDER)" check

softhier-init: simulator-init

softhier-power-check: simulator-check

ice-inputs: dirs
	@ROOT_DIR="$(ROOT_DIR)" SOFTHIER_DIR="$(SOFTHIER_DIR)" \
	SIMULATOR_CONFIG="$(SIMULATOR_CONFIG)" \
	SYSTEM_CONFIG_FILE="$(SYSTEM_CONFIG_FILE)" \
	DEFAULT_POWER_W="$(DEFAULT_POWER_W)" PYTHON="$(PYTHON)" \
	"$(SIMULATOR_PROVIDER)" export-system
	@args=( \
		"$(GEOMETRY_SCRIPT_DIR)/generate_3dice_inputs.py" \
		--system-config "$(SYSTEM_CONFIG_FILE)" \
		--geo "$(GEO_FILE)" \
		--floorplan "$(ICE_FLOORPLAN_FILE)" \
		--stk "$(ICE_STK_FILE)" \
		--power-interval-ps "$(POWER_INTERVAL_PS)" \
		--target-top-die-cells "$(ICE_TARGET_TOP_DIE_CELLS)" \
	); \
	if [[ -n "$(ICE_SLOT_SECONDS)" ]]; then args+=(--slot-seconds "$(ICE_SLOT_SECONDS)"); fi; \
	if [[ -n "$(ICE_STEP_SECONDS)" ]]; then args+=(--step-seconds "$(ICE_STEP_SECONDS)"); fi; \
	"$(PYTHON)" "$${args[@]}"

simulator-build:
	@ROOT_DIR="$(ROOT_DIR)" SOFTHIER_DIR="$(SOFTHIER_DIR)" \
	SIMULATOR_CONFIG="$(SIMULATOR_CONFIG)" \
	SIMULATOR_APP="$(SIMULATOR_APP)" \
	SIMULATOR_PLATFORM="$(SIMULATOR_PLATFORM)" \
	POWER_INTERVAL_PS="$(POWER_INTERVAL_PS)" \
	RAW_POWER_TRACE="$(RAW_POWER_TRACE)" \
	POWER_HOOK_EXECUTABLE="$(POWER_HOOK_EXECUTABLE)" \
	POWER_HOOK_CONFIG_FILE="$(POWER_HOOK_CONFIG_FILE)" \
	POWER_HOOK_REQUEST_FILE="$(POWER_HOOK_REQUEST_FILE)" \
	POWER_HOOK_RESPONSE_FILE="$(POWER_HOOK_RESPONSE_FILE)" \
	POWER_HOOK_TRACE_FILE="$(POWER_HOOK_TRACE_FILE)" \
	SYSTEM_CONFIG_FILE="$(SYSTEM_CONFIG_FILE)" \
	GEO_FILE="$(GEO_FILE)" \
	PYTHON="$(PYTHON)" \
	"$(SIMULATOR_PROVIDER)" build

softhier-build: simulator-build

coupled-run:
	@RUN_NAME="$(RUN_NAME)" \
	RUN_ID="$(RUN_ID)" \
	RUN_ROOT="$(RUN_ROOT)" \
	RUN_DIR="$(RUN_DIR)" \
	GENERATED_DIR="$(GENERATED_DIR)" \
	TRACE_DIR="$(TRACE_DIR)" \
	RESULT_DIR="$(RESULT_DIR)" \
	STATE_DIR="$(STATE_DIR)" \
	RUN_3DICE_GEN_DIR="$(RUN_3DICE_GEN_DIR)" \
	RUN_3DICE_DIR="$(RUN_3DICE_DIR)" \
	LOG_DIR="$(LOG_DIR)" \
	PID_DIR="$(PID_DIR)" \
	SIMULATOR_PROVIDER="$(SIMULATOR_PROVIDER)" \
	SIMULATOR_CONFIG="$(SIMULATOR_CONFIG)" \
	SIMULATOR_APP="$(SIMULATOR_APP)" \
	SIMULATOR_PLATFORM="$(SIMULATOR_PLATFORM)" \
	SOFTHIER_DIR="$(SOFTHIER_DIR)" \
	GEOMETRY_SCRIPT_DIR="$(GEOMETRY_SCRIPT_DIR)" \
	DICE_DIR="$(DICE_DIR)" \
	DICE_BIN_DIR="$(DICE_BIN_DIR)" \
	PORT="$(PORT)" \
	SERVER_HOST="$(SERVER_HOST)" \
	DICE_RUN_MODE="$(DICE_RUN_MODE)" \
	POWER_INTERVAL_PS="$(POWER_INTERVAL_PS)" \
	ICE_SLOT_SECONDS="$(ICE_SLOT_SECONDS)" \
	ICE_STEP_SECONDS="$(ICE_STEP_SECONDS)" \
	ICE_TARGET_TOP_DIE_CELLS="$(ICE_TARGET_TOP_DIE_CELLS)" \
	DEFAULT_POWER_W="$(DEFAULT_POWER_W)" \
	BUILD_SIMULATOR="$(BUILD_SIMULATOR)" \
	BUILD_3DICE="$(BUILD_3DICE)" \
	WAIT_TIMEOUT="$(WAIT_TIMEOUT)" \
	EXIT_TIMEOUT="$(EXIT_TIMEOUT)" \
	SIMULATOR_LOG_TAIL_LINES="$(SIMULATOR_LOG_TAIL_LINES)" \
	PYTHON="$(PYTHON)" \
	AUTO_BOOTSTRAP="$(AUTO_BOOTSTRAP)" \
	ICE_GENERATE_GIF="$(ICE_GENERATE_GIF)" \
	ICE_GIF_FILE="$(ICE_GIF_FILE)" \
	ICE_GIF_STRIDE="$(ICE_GIF_STRIDE)" \
	ICE_GIF_WIDTH="$(ICE_GIF_WIDTH)" \
	ICE_GIF_FPS="$(ICE_GIF_FPS)" \
	ICE_GIF_WRITER="$(ICE_GIF_WRITER)" \
	ICE_GIF_PYTHON="$(ICE_GIF_PYTHON)" \
	SYSTEM_CONFIG_FILE="$(SYSTEM_CONFIG_FILE)" \
	GEO_FILE="$(GEO_FILE)" \
	ICE_FLOORPLAN_FILE="$(ICE_FLOORPLAN_FILE)" \
	ICE_STK_FILE="$(ICE_STK_FILE)" \
	ICE_RUNTIME_FLOORPLAN_FILE="$(ICE_RUNTIME_FLOORPLAN_FILE)" \
	ICE_RUNTIME_STK_FILE="$(ICE_RUNTIME_STK_FILE)" \
	RAW_POWER_TRACE="$(RAW_POWER_TRACE)" \
	DICE_POWER_TRACE="$(DICE_POWER_TRACE)" \
	DONE_FILE="$(DONE_FILE)" \
	POWER_HOOK_EXECUTABLE="$(POWER_HOOK_EXECUTABLE)" \
	POWER_HOOK_CONFIG_FILE="$(POWER_HOOK_CONFIG_FILE)" \
	POWER_HOOK_REQUEST_FILE="$(POWER_HOOK_REQUEST_FILE)" \
	POWER_HOOK_RESPONSE_FILE="$(POWER_HOOK_RESPONSE_FILE)" \
	POWER_HOOK_TRACE_FILE="$(POWER_HOOK_TRACE_FILE)" \
	COMPONENT_TEMPERATURE_TRACE="$(COMPONENT_TEMPERATURE_TRACE)" \
	THERMAL_FEEDBACK_FILE="$(THERMAL_FEEDBACK_FILE)" \
	POWER_HOOK_POLL_SECONDS="$(POWER_HOOK_POLL_SECONDS)" \
	POWER_HOOK_TIMEOUT_SECONDS="$(POWER_HOOK_TIMEOUT_SECONDS)" \
	"$(CO_SIMULATION_SCRIPT_DIR)/coupled_run.sh" run

coupled-status:
	@RUN_DIR="$(RUN_DIR)" PID_DIR="$(PID_DIR)" "$(CO_SIMULATION_SCRIPT_DIR)/coupled_run.sh" status

coupled-stop:
	@RUN_DIR="$(RUN_DIR)" PID_DIR="$(PID_DIR)" "$(CO_SIMULATION_SCRIPT_DIR)/coupled_run.sh" stop

clean-run:
	@printf 'Removing %s\n' "$(RUN_DIR)"
	@rm -rf "$(RUN_DIR)"

clean-latest:
	@latest="$(RUN_ROOT)/$(RUN_NAME)/latest"; \
	if [[ -L "$$latest" ]]; then \
		target="$$(readlink -f "$$latest")"; \
		printf 'Removing latest target %s and link %s\n' "$$target" "$$latest"; \
		rm -rf "$$target" "$$latest"; \
	elif [[ -e "$$latest" ]]; then \
		printf 'Removing %s\n' "$$latest"; \
		rm -rf "$$latest"; \
	else \
		printf 'No latest run for RUN_NAME=%s\n' "$(RUN_NAME)"; \
	fi

clean-runs:
	@printf 'Removing all root run directories under %s\n' "$(RUN_ROOT)"
	@rm -rf "$(RUN_ROOT)"

list-runs:
	@if [[ -d "$(RUN_ROOT)" ]]; then \
		found=0; \
		while IFS= read -r manifest; do \
			found=1; \
			dir="$$(dirname "$$manifest")"; \
			printf '%s\n' "$${dir#$(RUN_ROOT)/}"; \
		done < <(find "$(RUN_ROOT)" -name run.env -type f | sort); \
		if [[ "$$found" == "0" ]]; then printf 'No timestamped runs with run.env under %s\n' "$(RUN_ROOT)"; fi; \
	else \
		printf 'No runs under %s\n' "$(RUN_ROOT)"; \
	fi

latest-run:
	@latest="$(RUN_ROOT)/$(RUN_NAME)/latest"; \
	if [[ -L "$$latest" || -e "$$latest" ]]; then readlink -f "$$latest"; else printf 'No latest run for RUN_NAME=%s\n' "$(RUN_NAME)"; fi

adapter-smoke:
	$(PYTHON) -m py_compile \
		"$(ROOT_DIR)/Interface_scripts/system_contract.py" \
		"$(ROOT_DIR)/Interface_scripts/providers/softhier/export_system_config.py" \
		"$(CO_SIMULATION_SCRIPT_DIR)/3dice_power_hook.py" \
		"$(CO_SIMULATION_SCRIPT_DIR)/generate_power_hook_config.py" \
		"$(CO_SIMULATION_SCRIPT_DIR)/ice_trace_adapter.py" \
		"$(CO_SIMULATION_SCRIPT_DIR)/wait_for_log.py" \
		"$(ROOT_DIR)/Interface_scripts/plot_runtime_temperature_map/plot_runtime_tmap.py" \
		"$(GEOMETRY_SCRIPT_DIR)/generate_3dice_inputs.py" \
		"$(GEOMETRY_SCRIPT_DIR)/roi2ice_floorplan_no_power.py" \
		"$(GEOMETRY_SCRIPT_DIR)/roi2ice_stk.py"

interface-tests: adapter-smoke
	$(PYTHON) -m unittest discover \
		-s "$(ROOT_DIR)/Interface_scripts/tests" \
		-p "test_*.py" \
		-v
