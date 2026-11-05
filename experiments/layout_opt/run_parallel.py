#!/usr/bin/env python3
"""Capture, validate and replay all four layouts, with durable progress files."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from experiments.layout_opt.config import CASE, OPT_STEP, HEAT_TRANSFER_COEFFICIENT, CAMPAIGN, LAYOUTS
from experiments.layout_opt.progress import status as capture_status
from experiments.layout_opt.transforms import local, write_json
from experiments.llm_dram3d.opt_steps import opt_metadata

HERE = ROOT / "experiments/layout_opt"
PROFILES = ("constant", "temperature_aware")


def read_json(path, default=None):
    try:
        return json.loads(local(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def git_revision(ref="HEAD"):
    return subprocess.check_output(["git", "rev-parse", ref], cwd=ROOT, text=True).strip()


def available_ports(count):
    """Hold all allocations together so no two workers receive the same port."""
    sockets = []
    try:
        for _ in range(count):
            stream = socket.socket()
            sockets.append(stream)
            stream.bind(("127.0.0.1", 0))
        return [stream.getsockname()[1] for stream in sockets]
    finally:
        for stream in sockets:
            stream.close()


def replay_status(campaign):
    entries = []
    for name, _ in LAYOUTS:
        for profile in PROFILES:
            path = HERE / "artifacts" / campaign / "runs" / name / profile / "summary.json"
            summary = read_json(path, {})
            entries.append(dict(layout=name, profile=profile, state=summary.get("status", "queued"),
                simulated_seconds=summary.get("simulated_seconds", 0.), target_seconds=1.,
                slots=summary.get("slots", 0), target_slots=50000,
                peak_component_c=summary.get("peak_component_c"),
                first_model_limit_seconds=summary.get("first_model_limit_seconds"),
                summary=str(path.relative_to(ROOT))))
    return entries


class Pipeline:
    def __init__(self, args):
        self.args = args
        self.folder = local(HERE / "artifacts" / args.campaign)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.logs = self.folder / "pipeline_logs"
        self.logs.mkdir(exist_ok=True)
        self.lock = (self.folder / "controller.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise
        self.started = time.monotonic()
        self.active = {}
        self.state = dict(status="running", phase="preparation", pid=os.getpid(),
            git_commit=git_revision(), started_utc=self.now(), workers=args.workers,
            configuration=dict(case=CASE, **opt_metadata(OPT_STEP),
                heat_transfer_coefficient_w_m2_k=HEAT_TRANSFER_COEFFICIENT,
                thermal_replay_till=1., allow_model_clamp=True, profiles=list(PROFILES),
                interval_ps=20_000_000, target_cells_per_die=16384),
            source_campaign=args.campaign, replay_campaign=args.replay_campaign,
            jobs=[], error=None,
            comparison=str((HERE / "results" / args.replay_campaign / "final_comparison").relative_to(ROOT)))

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def update(self):
        self.state.update(updated_utc=self.now(), elapsed_seconds=round(time.monotonic()-self.started, 1),
            single_execution_runs=capture_status(self.args.campaign),
            replay_runs=replay_status(self.args.replay_campaign),
            replay_analysis=read_json(HERE / "artifacts" / self.args.replay_campaign / "analysis_progress.json", {}))
        write_json(self.folder / "progress.json", self.state)
        lines = ["# Layout campaign progress", "", f"Updated: {self.state['updated_utc']}", "",
            f"Status: **{self.state['status']}** · phase: **{self.state['phase']}**", "",
            f"{CASE} · OPT {OPT_STEP} (SW) · h = {HEAT_TRANSFER_COEFFICIENT:,.0f} W/(m² K)",
            "", "Replay: 1 second per layout/profile, model clamp enabled.", "",
            f"Controller PID: {os.getpid()} · commit: `{self.state['git_commit']}`", "",
            "| Job | State | Log |", "| --- | --- | --- |"]
        for job in self.state["jobs"]:
            lines.append(f"| {job['name']} | {job['state']} | `{job['log']}` |")
        lines += ["", "## Fresh application captures", "",
            "| Layout | Profile | State | Simulated µs |", "| --- | --- | --- | ---: |"]
        for run in self.state["single_execution_runs"]:
            lines.append(f"| {run['trial']} | {run['profile']} | {run['state']} | {run['simulated_us']:.2f} |")
        lines += ["", "## One-second replays", "",
            "| Layout | Profile | State | Seconds / 1 | Steps / 50000 |",
            "| --- | --- | --- | ---: | ---: |"]
        for run in self.state["replay_runs"]:
            lines.append(f"| {run['layout']} | {run['profile']} | {run['state']} | {run['simulated_seconds']:.5f} | {run['slots']} |")
        if self.state["error"]:
            lines += ["", "Error: " + self.state["error"]]
        lines += ["", "Final comparison: `" + self.state["comparison"] + "`", ""]
        temporary = self.folder / "progress.md.tmp"
        temporary.write_text("\n".join(lines))
        temporary.replace(self.folder / "progress.md")

    def group(self, phase, jobs, workers=1):
        self.state["phase"] = phase
        queued = []
        for name, arguments in jobs:
            job = dict(name=name, state="queued", command=[sys.executable, *map(str, arguments)],
                       log=str((self.logs / f"{name}.log").relative_to(ROOT)))
            self.state["jobs"].append(job)
            queued.append(job)
        failed = []
        while queued or self.active:
            while queued and len(self.active) < workers and not failed:
                job = queued.pop(0)
                log = local(ROOT / job["log"]).open("a")
                try:
                    process = subprocess.Popen(job["command"], cwd=ROOT, stdout=log,
                        stderr=subprocess.STDOUT, start_new_session=True)
                finally:
                    log.close()
                job.update(state="running", pid=process.pid, started_utc=self.now())
                self.active[process] = job
                print(f"Started {job['name']}; log: {job['log']}", flush=True)
            for process, job in list(self.active.items()):
                code = process.poll()
                if code is None:
                    continue
                job.update(state="complete" if code == 0 else "failed", exit_code=code, ended_utc=self.now())
                self.active.pop(process)
                if code:
                    failed.append(job["name"])
                print(f"{job['name']}: {job['state']}", flush=True)
            if failed:
                for job in queued:
                    job["state"] = "skipped"
                queued.clear()
            self.update()
            if queued or self.active:
                time.sleep(self.args.poll_seconds)
        if failed:
            raise RuntimeError("failed jobs: " + ", ".join(failed) + "; see pipeline_logs")

    def stop_children(self):
        for process in self.active:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process, job in self.active.items():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            job.update(state="interrupted", exit_code=process.returncode)
        self.active.clear()

    def run(self):
        self.update()
        try:
            if self.args.require_pushed and git_revision("@{upstream}") != self.state["git_commit"]:
                raise ValueError("push HEAD to the tracked upstream before starting this campaign")
            if self.args.require_pushed:
                subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "experiments/layout_opt",
                    "experiments/llm_dram3d"], cwd=ROOT, check=True)
            prepare = [HERE / "run.py", "prepare", "--campaign", self.args.campaign]
            if self.args.build_hardware:
                prepare.append("--build-hardware")
            self.group("prepare_workload", [("prepare_workload", prepare)])
            ports = available_ports(len(LAYOUTS))
            self.group("capture_layouts", [(f"capture_{name}", [HERE / "run.py", "run",
                "--campaign", self.args.campaign, "--name", name,
                "--layout", HERE / "layouts" / layout, "--port", port])
                for (name, layout), port in zip(LAYOUTS, ports)], self.args.workers)
            source_results = HERE / "results" / self.args.campaign
            self.group("measure_captures", [(f"measure_{name}", [HERE / "measure.py",
                "--trial", self.folder / "trials" / name, "--output", source_results / name])
                for name, _ in LAYOUTS], min(2, self.args.workers))
            self.group("compare_captures", [("compare_captures", [HERE / "compare.py", "--results",
                *[source_results / name for name, _ in LAYOUTS],
                "--output", source_results / "final_comparison", "--animations"])])
            replay = HERE / "artifacts" / self.args.replay_campaign
            if not (replay / "study.json").is_file():
                self.group("prepare_replay", [("prepare_replay", [HERE / "replay_study.py", "prepare",
                    "--campaign", self.args.replay_campaign, "--source-results", source_results,
                    "--heat-transfer-coefficient", HEAT_TRANSFER_COEFFICIENT])])
            else:
                study = read_json(replay / "study.json")
                if any(study.get(key) != value for key, value in {
                    "case":CASE, "opt_step":OPT_STEP,
                    "heat_transfer_coefficient_w_m2_k":HEAT_TRANSFER_COEFFICIENT,
                    "source_results":str(source_results.relative_to(ROOT))}.items()):
                    raise ValueError("existing replay belongs to different source settings")
            self.group("replay_1s", [("replay_1s", [HERE / "replay_study.py", "run",
                "--campaign", self.args.replay_campaign, "--workers", self.args.workers])])
            self.group("analyze_and_render", [("analyze_and_render", [HERE / "finish_replay.py",
                "--campaign", self.args.replay_campaign])])
            self.state.update(status="complete", phase="complete", finished_utc=self.now())
        except BaseException as exc:
            self.stop_children()
            self.state.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                              error=str(exc) or type(exc).__name__)
            raise
        finally:
            self.update()
            self.lock.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", default=CAMPAIGN)
    parser.add_argument("--replay-campaign")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--poll-seconds", type=float, default=15.)
    parser.add_argument("--build-hardware", action="store_true")
    parser.add_argument("--require-pushed", action="store_true")
    parser.add_argument("--detach", action="store_true", help="launch a persistent background controller")
    args = parser.parse_args(argv)
    args.replay_campaign = args.replay_campaign or args.campaign + "_replay1s"
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value)
           for value in (args.campaign, args.replay_campaign)):
        parser.error("campaign names must be simple directory names")
    if args.campaign == args.replay_campaign or not 1 <= args.workers <= 8 or not 1 <= args.poll_seconds <= 60:
        parser.error("use distinct campaigns, 1..8 workers and a 1..60 second polling interval")
    return args


def main():
    args = parse_args()
    if args.detach:
        folder = local(HERE / "artifacts" / args.campaign)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "controller.log").open("a") as log:
            command = [sys.executable, str(Path(__file__).resolve()),
                       *[arg for arg in sys.argv[1:] if arg != "--detach"]]
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(20):
            state = read_json(folder / "progress.json", {})
            if state.get("pid") == process.pid and process.poll() is None:
                print(json.dumps(dict(pid=process.pid, progress=str(folder / "progress.json"),
                    readable_progress=str(folder / "progress.md"), log=str(folder / "controller.log")), indent=2))
                return
            if process.poll() is not None:
                raise RuntimeError(f"controller exited with {process.returncode}; see {folder / 'controller.log'}")
            time.sleep(.5)
        raise RuntimeError("controller did not publish progress within 10 seconds; inspect controller.log")
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"received signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    Pipeline(args).run()


if __name__ == "__main__":
    main()
