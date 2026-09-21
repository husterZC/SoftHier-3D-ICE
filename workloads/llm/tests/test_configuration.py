import copy
import ctypes
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

PACKAGE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("llm_prepare", PACKAGE / "prepare.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.arch = prepare.load_arch(PACKAGE / "configs/arch_4x4.py")
        self.config = prepare.configuration("prefill", "smoke")

    def test_all_presets_fit(self):
        for phase in ("prefill", "decode"):
            for preset in ("smoke", "original"):
                with self.subTest(phase=phase, preset=preset):
                    config = prepare.configuration(phase, preset)
                    layout = prepare.validate(config, self.arch)
                    self.assertEqual(layout["regions"][-1]["start"], 0x9C0000000)
                    for first, second in zip(layout["regions"], layout["regions"][1:]):
                        self.assertLessEqual(first["end"], second["start"])

    def test_unsupported_architecture(self):
        for field, value in (("num_cluster_x", 32), ("spatz_attaced_core_list", []),
                             ("cluster_tcdm_size", 4096), ("hbm_chan_placement", [0,0,0,0]),
                             ("spatz_vlsu_port_width", 4), ("cluster_tcdm_bank_width", 64),
                             ("spatz_num_vlsu_port", 4), ("spatz_num_function_unit", 8)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                prepare.validate(self.config, {**self.arch, field: value})

    def test_invalid_model_and_cache(self):
        for field, value in (("num_layers", 0), ("n_head", 5), ("n_kv_head", 2),
                             ("decode_init_cache_len", 127), ("prefill_tokens", 129),
                             ("d_model", 769), ("elem_size", 4)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                prepare.validate({**self.config, field: value}, self.arch)

    def test_invalid_tiling(self):
        for mapping, field, value in (("gemm", "group_x", 3), ("gemm", "m_tile", 17),
                                      ("attention", "block_y", 128), ("attention", "block_x", 63)):
            config = copy.deepcopy(self.config)
            config[mapping][field] = value
            with self.subTest(mapping=mapping, field=field), self.assertRaises(ValueError):
                prepare.validate(config, self.arch)

    def test_memory_overflow(self):
        self.config["num_layers"] = 1000
        with self.assertRaises(ValueError):
            prepare.validate(self.config, self.arch)

    def test_decode_last_token_and_overflow(self):
        config = prepare.configuration("decode", "original")
        prepare.validate(config, self.arch)
        config["decode_steps"] += 1
        with self.assertRaises(ValueError):
            prepare.validate(config, self.arch)


class NativeHelpersTests(unittest.TestCase):
    """Compile the real C layout/partition helpers, independent of RISC-V."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        temp = Path(cls.tmp.name)
        (temp / "flex_cluster_arch.h").write_text('#define ARCH_HBM_START_BASE 0xc0000000u\n#define ARCH_HBM_NODE_ADDR_SPACE 0xc0000000u\n#define ARCH_NUM_CLUSTER_X 4\n#define ARCH_NUM_CLUSTER_Y 4\n')
        (temp / "helpers.c").write_text('''
#include "llm_layout.h"
#include "llm_weights.h"
#include "llm_partition.h"
uint64_t cache(uint32_t l, uint32_t h, uint32_t t) { return llm_k_cache_head_token_addr(l,h,t); }
uint32_t append(uint32_t n, uint32_t k, uint32_t cap) { return llm_cache_can_append(n,k,cap); }
uint32_t start(uint32_t n, uint32_t w, uint32_t i) { return llm_row_start(n,w,i); }
uint32_t count(uint32_t n, uint32_t w, uint32_t i) { return llm_row_count(n,w,i); }
uint64_t weight(uint32_t l) { return llm_wq_addr(l); }
''')
        subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Werror", "-I", str(temp), "-I", str(PACKAGE / "common/include"),
                        str(temp / "helpers.c"), "-o", str(temp / "helpers.so")], check=True)
        cls.lib = ctypes.CDLL(str(temp / "helpers.so"))
        for name in ("cache", "weight"):
            getattr(cls.lib, name).restype = ctypes.c_uint64

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_allocated_cache_head_stride(self):
        self.assertEqual(self.lib.cache(0,1,0) - self.lib.cache(0,0,0), 2048*128*2)
        self.assertEqual(self.lib.cache(1,0,0) - self.lib.cache(0,0,0), 6*2048*128*2)
        self.assertEqual(self.lib.cache(0,0,1) - self.lib.cache(0,0,0), 128*2)
        self.assertLess(self.lib.cache(0,0,2047), self.lib.cache(0,1,0))

    def test_architecture_weight_base(self):
        self.assertEqual(self.lib.weight(0), 0x9c0000000)
        self.assertGreater(self.lib.weight(1), self.lib.weight(0))

    def test_cache_boundaries(self):
        self.assertEqual(self.lib.append(2047,1,2048), 1)
        self.assertEqual(self.lib.append(2048,1,2048), 0)
        self.assertEqual(self.lib.append(2049,0,2048), 0)
        self.assertEqual(self.lib.append(1,0xffffffff,2048), 0)

    def test_complete_nonoverlapping_row_ownership(self):
        for rows in (0,1,2,3,4,5,16,17,64):
            covered = []
            for sid in range(4):
                start = self.lib.start(rows,4,sid)
                covered.extend(range(start, start + self.lib.count(rows,4,sid)))
            self.assertEqual(covered, list(range(rows)))
        self.assertEqual([self.lib.count(1,4,i) for i in range(4)], [1,0,0,0])
        self.assertEqual(self.lib.count(1,0,0), 0)
