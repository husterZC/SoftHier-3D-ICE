# Staged SDK corrections

These patches target SDK commit `1244fdbc34977aff5a6a10ead079053fb5d31d00`.
`prepare.py` checks that revision and applies patches with zero fuzz to a copy of
the kernel sources. The SDK checkout is never patched.

1. `0001-summa-reset-accumulator.patch` clears `store_recorded` when a reused
   output buffer is zeroed. Without this, later output tiles can reuse stale
   accumulator bookkeeping.
2. `0002-attention-whole-rows.patch` assigns complete rows to Spatz units and
   skips vector work for units with no rows. The synchronous kernel now handles
   one query row with four vector units. Asynchronous attention is unsupported
   by this workload and rejected before execution.

3. `0003-attention-row-tails.patch` adds the remainder loops omitted by the
   SDK's sixteen-row unrolling. Row max, exponentiation, row sum, scaling, and
   normalization now execute for four rows per Spatz in prefill and one row
   in decode.

The live-length versus allocated-capacity KV stride correction is in the LLM
attention wrapper, which adjusts both the head stride and each group's initial
K/V base after SDK analysis. Existing SDK entry points are unchanged.
