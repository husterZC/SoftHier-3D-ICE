"""Tests for scientific comparison guards and the candidate proposal score."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from experiments.layout_opt.compare import validate_comparison
from experiments.layout_opt.native_export import native_contract
from experiments.layout_opt.search import optimize
from experiments.layout_opt.test_layout_opt import ARCH, build_contract, import_architecture
from experiments.layout_opt.transforms import ROOT, plan_for, write_json


class SearchTests(unittest.TestCase):
    def test_search_finds_separated_optima_and_records_monotone_moves(self):
        # Two deliberately independent clusters have known orientation minima.
        # Target selection matters: only the chosen orientation's target counts.
        matrix = np.zeros((16,1,16,2))
        costs = [[10,9,8,7,6,2,5,4],[10,8,2,7,6,5,4,3]]
        for cid in range(2):
            for orientation,cost in enumerate(costs[cid]):
                index=cid*8+orientation
                matrix[index,0,index,:]=cost
        best,history,normalization = optimize(matrix,restarts=8)
        self.assertEqual(best["transforms"],["MY","R180"])
        self.assertAlmostEqual(best["score_ratio"],.2)
        self.assertEqual(normalization.tolist(),[10,10])
        for trial in history:
            scores=[move["score_ratio"] for move in trial["accepted_moves"]]
            self.assertEqual(scores,sorted(scores,reverse=True))


@unittest.skipUnless(ARCH.is_file(),"requires the pinned SDK")
class ComparisonTests(unittest.TestCase):
    def setUp(self):
        folder=ROOT/"tmp/layout_opt_tests"
        folder.mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.folders=[]
        self.items=[]
        config=build_contract(import_architecture(ARCH),ARCH,0.,floorplan_rule="square_bands")
        keys=("cases layer seed repeats all_experts opt_step interval_ps thermal_step_ps target_cells "
              "estimate_scale floorplan_rule thermal_replay thermal_replay_till allow_model_clamp "
              "dram_power_temperature_dependence sdk_commit arch_sha256 source_sha256 "
              "native_source_sha256 thermal_config_sha256 dram_power_config_sha256 "
              "resolved_thermal_config resolved_dram_power_config").split()
        for number,mapping in enumerate((["R0"]*16,["MY","R90"]+["R0"]*14)):
            trial=self.root/str(number)
            trial.mkdir()
            self.folders.append(trial)
            write_json(trial/"study.json",dict.fromkeys(keys))
            plan=plan_for(mapping)
            profiles={}
            for profile in ("constant","temperature_aware"):
                run=trial/profile
                write_json(run/"generated/system_config.json",native_contract(config,plan))
                profiles[profile]=dict(run=str(run.relative_to(ROOT)),binary_sha256="same",
                    preload_sha256="same",stage_cycles={"moe":1},layer_time_us=1.,slots=2,
                    dies={"LOGIC_DIE":{"cell_count":16384},"DRAM_DIE":{"cell_count":16384}})
                np.savez_compressed(trial/f"{profile}_activity.npz",paths=["logic","dram"],
                    start_ps=[0,10],end_ps=[10,20],dynamic_w=[[1.,2.],[3.,4.]],
                    dram_background_w=[[.1],[.2]])
            self.items.append(dict(name=str(number),trial=str(trial.relative_to(ROOT)),
                                   layout=plan,profiles=profiles))

    def test_legal_geometry_and_identical_activity_pass(self):
        evidence=validate_comparison(self.items,self.folders)
        self.assertEqual(evidence[1]["max_dynamic_delta_w"],dict(constant=0.,temperature_aware=0.))

    def test_changed_timing_is_rejected(self):
        self.items[1]["profiles"]["temperature_aware"]["stage_cycles"]["moe"]=2
        with self.assertRaisesRegex(ValueError,"stage_cycles"):
            validate_comparison(self.items,self.folders)

    def test_changed_dynamic_work_is_rejected(self):
        path=self.folders[1]/"constant_activity.npz"
        with np.load(path) as data:
            values={key:data[key].copy() for key in data.files}
        values["dynamic_w"][1,0]+=.01
        np.savez_compressed(path,**values)
        with self.assertRaisesRegex(ValueError,"activity differs"):
            validate_comparison(self.items,self.folders)

    def test_nonrigid_component_move_is_rejected(self):
        path=self.folders[1]/"constant/generated/system_config.json"
        data=json.loads(path.read_text())
        data["geometry"]["chip"]["subs"]["cluster_0"]["subs"]["redmule"]["offset"][0]+=.01
        write_json(path,data)
        with self.assertRaisesRegex(ValueError,"rigid cluster transform"):
            validate_comparison(self.items,self.folders)


if __name__ == "__main__":
    unittest.main()
