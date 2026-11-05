"""Current layout campaign settings; historical results retain their manifests."""

CASE = "decode_b1_kv4096"
OPT_STEP = 16
HEAT_TRANSFER_COEFFICIENT = 25000.
CAMPAIGN = "b1_kv4096_opt16_h25000"
REPLAY_CAMPAIGN = CAMPAIGN + "_replay1s"
LAYOUTS = (
    ("00_baseline", "00_baseline.json"),
    ("01_edge_inward_v2", "01_edge_inward.json"),
    ("02_balanced_search", "02_balanced_search.json"),
    ("03_top_refinement", "03_top_refinement.json"),
)
