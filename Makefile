SHELL := /usr/bin/env bash

ROOT_DIR := $(abspath .)
SOFTHIER_DIR ?= $(ROOT_DIR)/SoftHier
DICE_DIR ?= $(ROOT_DIR)/3D-ICE
DICE_BIN_DIR ?= $(DICE_DIR)/bin
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

CFG ?= $(SOFTHIER_DIR)/soft_hier/flex_cluster/flex_cluster_arch.py
APP ?=
PLD ?=
PORT ?= 54322
SERVER_HOST ?= 127.0.0.1
PWR_INTERVAL_PS ?= 100000000
ICE_SLOT_SECONDS ?=
ICE_STEP_SECONDS ?=
OTHERS_POWER ?= 0.0
BUILD_SOFTHIER ?= 1
BUILD_3DICE ?= 1
WAIT_TIMEOUT ?= 180
EXIT_TIMEOUT ?= 120
SOFTHIER_LOG_TAIL_LINES ?= 5
PYTHON ?= python3
AUTO_BOOTSTRAP ?= 0

GEO_FILE ?= $(GENERATED_DIR)/geo.json
ICE_FLOORPLAN_FILE ?= $(RUN_3DICE_GEN_DIR)/floorplan_nopower.flp
ICE_STK_FILE ?= $(RUN_3DICE_GEN_DIR)/ice.stk
ICE_RUNTIME_FLOORPLAN_FILE ?= $(RUN_3DICE_DIR)/floorplan_nopower.flp
ICE_RUNTIME_STK_FILE ?= $(RUN_3DICE_DIR)/ice.stk
RAW_POWER_TRACE ?= $(TRACE_DIR)/softhier_power_raw.txt
DICE_POWER_TRACE ?= $(TRACE_DIR)/3dice_power_traces.txt
DONE_FILE ?= $(STATE_DIR)/softhier.done

APP_ARG := $(if $(strip $(APP)),app=$(APP),)
PLD_ARG := $(if $(strip $(PLD)),pld=$(PLD),)

.PHONY: help bootstrap co-simulation dirs 3dice-build softhier-init softhier-power-check ice-inputs softhier-build coupled-run coupled-status coupled-stop clean-run clean-latest clean-runs list-runs latest-run adapter-smoke

help:
	@printf '%s\n' \
		'SoftHier/3D-ICE root orchestration targets:' \
		'  make bootstrap       First-time setup: submodules, SoftHier hooks, 3D-ICE build' \
		'  make co-simulation   Run bootstrap, then a timestamped coupled simulation' \
		'  make 3dice-build      Build or verify 3D-ICE client/server binaries' \
		'  make softhier-init    Initialize SoftHier and nested submodules' \
		'  make softhier-power-check Verify SoftHier runtime power hook is present' \
		'  make ice-inputs       Generate run-local 3D-ICE geo/floorplan/stk files' \
		'  make softhier-build   Build SoftHier hw/sw with run-local power output path' \
		'  make coupled-run      Run the localhost coupled simulation' \
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
		'  CFG=$(CFG)' \
		'  APP=$(APP)' \
		'  PORT=$(PORT)' \
		'  PWR_INTERVAL_PS=$(PWR_INTERVAL_PS)' \
		'  ICE_SLOT_SECONDS=$(ICE_SLOT_SECONDS)' \
		'  ICE_STEP_SECONDS=$(ICE_STEP_SECONDS)' \
		'  BUILD_SOFTHIER=$(BUILD_SOFTHIER)' \
		'  SOFTHIER_LOG_TAIL_LINES=$(SOFTHIER_LOG_TAIL_LINES)' \
		'' \
		'More detail: co-simulation.md'

bootstrap:
	@ROOT_DIR="$(ROOT_DIR)" \
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

softhier-init:
	git submodule update --init SoftHier
	git -C "$(SOFTHIER_DIR)" submodule update --init --recursive
	cd "$(SOFTHIER_DIR)" && source ./sourceme.sh

softhier-power-check:
	@"$(CO_SIMULATION_SCRIPT_DIR)/bootstrap.sh" --check-only

ice-inputs: dirs
	@args=( \
		"$(GEOMETRY_SCRIPT_DIR)/generate_3dice_inputs.py" \
		--arch "$(CFG)" \
		--geo "$(GEO_FILE)" \
		--floorplan "$(ICE_FLOORPLAN_FILE)" \
		--stk "$(ICE_STK_FILE)" \
		--pwr-interval-ps "$(PWR_INTERVAL_PS)" \
	); \
	if [[ -n "$(ICE_SLOT_SECONDS)" ]]; then args+=(--slot-seconds "$(ICE_SLOT_SECONDS)"); fi; \
	if [[ -n "$(ICE_STEP_SECONDS)" ]]; then args+=(--step-seconds "$(ICE_STEP_SECONDS)"); fi; \
	"$(PYTHON)" "$${args[@]}"

softhier-build:
	cd "$(SOFTHIER_DIR)" && source ./sourceme.sh && \
		$(MAKE) hw sw \
			cfg="$(CFG)" \
			$(APP_ARG) \
			pwr_interval_ps="$(PWR_INTERVAL_PS)" \
			ice_geo_file="$(GEO_FILE)" \
			ice_power_trace_file="$(RAW_POWER_TRACE)"

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
	SOFTHIER_DIR="$(SOFTHIER_DIR)" \
	GEOMETRY_SCRIPT_DIR="$(GEOMETRY_SCRIPT_DIR)" \
	DICE_DIR="$(DICE_DIR)" \
	DICE_BIN_DIR="$(DICE_BIN_DIR)" \
	CFG="$(CFG)" \
	APP="$(APP)" \
	PLD="$(PLD)" \
	PORT="$(PORT)" \
	SERVER_HOST="$(SERVER_HOST)" \
	PWR_INTERVAL_PS="$(PWR_INTERVAL_PS)" \
	ICE_SLOT_SECONDS="$(ICE_SLOT_SECONDS)" \
	ICE_STEP_SECONDS="$(ICE_STEP_SECONDS)" \
	OTHERS_POWER="$(OTHERS_POWER)" \
	BUILD_SOFTHIER="$(BUILD_SOFTHIER)" \
	BUILD_3DICE="$(BUILD_3DICE)" \
	WAIT_TIMEOUT="$(WAIT_TIMEOUT)" \
	EXIT_TIMEOUT="$(EXIT_TIMEOUT)" \
	SOFTHIER_LOG_TAIL_LINES="$(SOFTHIER_LOG_TAIL_LINES)" \
	PYTHON="$(PYTHON)" \
	AUTO_BOOTSTRAP="$(AUTO_BOOTSTRAP)" \
	GEO_FILE="$(GEO_FILE)" \
	ICE_FLOORPLAN_FILE="$(ICE_FLOORPLAN_FILE)" \
	ICE_STK_FILE="$(ICE_STK_FILE)" \
	ICE_RUNTIME_FLOORPLAN_FILE="$(ICE_RUNTIME_FLOORPLAN_FILE)" \
	ICE_RUNTIME_STK_FILE="$(ICE_RUNTIME_STK_FILE)" \
	RAW_POWER_TRACE="$(RAW_POWER_TRACE)" \
	DICE_POWER_TRACE="$(DICE_POWER_TRACE)" \
	DONE_FILE="$(DONE_FILE)" \
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
		"$(CO_SIMULATION_SCRIPT_DIR)/ice_trace_adapter.py" \
		"$(CO_SIMULATION_SCRIPT_DIR)/wait_for_log.py" \
		"$(GEOMETRY_SCRIPT_DIR)/generate_3dice_inputs.py" \
		"$(GEOMETRY_SCRIPT_DIR)/roi2ice_stk.py"
