/* Included only in the dedicated regression ELF. Exercises real SDK kernels,
 * not a host reimplementation: multiple SUMMA M/N/K tiles and all six heads. */
static uint16_t regression_input(uint32_t row, uint32_t col)
{
    return (uint16_t)(0x3400u + ((row % 32u) << 4) + (col % 16u));
}

void llm_common_kernel_regression(void)
{
    flex_global_barrier_xy();
    llm_common_init_accelerators();
    llm_zero_hbm_region(llm_wq_addr(0), BYTES_WQ);
    llm_init_eye(llm_wq_addr(0));
    for (uint32_t row = flex_get_cluster_id(); row < LLM_T; row += ARCH_NUM_CLUSTER) {
        if (flex_get_core_id() == 0) {
            uint16_t *buf = (uint16_t *)local(0);
            for (uint32_t col = 0; col < LLM_D_MODEL; ++col)
                buf[col] = regression_input(row, col);
        }
        flex_intra_cluster_sync();
        if (flex_is_dm_core()) {
            flex_dma_async_1d(LLM_H_ADDR + (uint64_t)row * LLM_D_MODEL * 2, local(0), LLM_D_MODEL * 2);
            flex_dma_async_wait_all();
        }
        flex_intra_cluster_sync();
    }
    flex_global_barrier_xy();
    /* Verify initialization independently before exercising the accelerator. */
    if (flex_get_cluster_id() == 0) {
        for (uint32_t matrix = 0; matrix < 2; ++matrix) {
            uint32_t rows = matrix ? LLM_D_MODEL : LLM_T;
            uint64_t base = matrix ? llm_wq_addr(0) : LLM_H_ADDR;
            for (uint32_t row = 0; row < rows; ++row) {
                if (row % 64 != 0 && row + 1 != rows) continue;
                if (flex_is_dm_core()) {
                    flex_dma_async_1d(local(0), base + (uint64_t)row * LLM_D_MODEL * 2, LLM_D_MODEL * 2);
                    flex_dma_async_wait_all();
                }
                flex_intra_cluster_sync();
                if (flex_get_core_id() == 0) {
                    volatile uint16_t *buf = (volatile uint16_t *)local(0);
                    for (uint32_t col = 0; col < LLM_D_MODEL; ++col) {
                        uint16_t expected = matrix ? (row == col ? 0x3c00 : 0) : regression_input(row, col);
                        if (buf[col] != expected) {
                            printf("[LLMForward] Input mismatch matrix=%u row=%u col=%u actual=%x expected=%x\n", matrix, row, col, buf[col], expected);
                            llm_fail(13, "HBM initialization mismatch");
                        }
                    }
                }
                flex_intra_cluster_sync();
            }
        }
    }
    flex_global_barrier_xy();
    if (flex_get_core_id() == 0 && flex_get_cluster_id() == 0)
        printf("[LLMForward] HBM initialization: PASS\n");
    llm_run_gemm_prefill(LLM_H_ADDR, llm_wq_addr(0), LLM_QKV_TM_ADDR,
                         LLM_T, LLM_D_MODEL, LLM_D_MODEL);
    /* Fetch each result once; inspect every element on core zero. */
    _Static_assert(LLM_T * LLM_D_MODEL * 2 <= ARCH_CLUSTER_TCDM_SIZE,
                   "regression output must fit the check buffer");
    if (flex_get_cluster_id() == 0) {
        if (flex_is_dm_core()) {
            flex_dma_async_1d(local(0), LLM_QKV_TM_ADDR, LLM_T * LLM_D_MODEL * 2);
            flex_dma_async_wait_all();
        }
        flex_intra_cluster_sync();
        if (flex_get_core_id() == 0) {
            volatile uint16_t *buf = (volatile uint16_t *)local(0);
            for (uint32_t row = 0; row < LLM_T; ++row)
                for (uint32_t col = 0; col < LLM_D_MODEL; ++col)
                    if (buf[row * LLM_D_MODEL + col] != regression_input(row, col)) {
                        printf("[LLMForward] GEMM mismatch row=%u col=%u actual=%x expected=%x\n",
                               row, col, buf[row * LLM_D_MODEL + col], regression_input(row, col));
                        llm_fail(10, "multi-tile identity GEMM mismatch");
                    }
        }
        flex_intra_cluster_sync();
    }
    flex_global_barrier_xy();
    if (flex_get_cluster_id() == 0 && flex_get_core_id() == 0)
        printf("[LLMForward] Multi-tile GEMM: PASS\n");

    llm_zero_hbm_region(LLM_Q_HM_ADDR, BYTES_HM_ALL);
    llm_zero_hbm_region(LLM_K_CACHE_BASE_ADDR, BYTES_KV_ALL_LAYERS);
    for (uint32_t head = flex_get_cluster_id(); head < LLM_N_HEAD; head += ARCH_NUM_CLUSTER) {
        if (flex_get_core_id() == 0) {
            uint16_t *buf = (uint16_t *)local(0);
            for (uint32_t d = 0; d < LLM_HEAD_DIM; ++d)
                buf[d] = float_to_fp16((float)(head + 1));
        }
        flex_intra_cluster_sync();
        if (flex_is_dm_core()) {
            for (uint32_t token = 0; token < LLM_MAX_CTX; ++token) {
                flex_dma_async_1d(llm_v_cache_head_token_addr(0, head, token), local(0), LLM_HEAD_DIM * 2);
                flex_dma_async_wait_all();
            }
        }
        flex_intra_cluster_sync();
    }
    flex_global_barrier_xy();
    for (uint32_t test = 0; test < 2; ++test) {
        uint32_t q = test ? 1 : 64, kv = test ? 65 : 64;
        LLMAttentionRuntimeArgs a;
        llm_common_attn_profile_default(&a);
        a.flatten_scale_x = a.flatten_scale_y = test ? 1 : 4;
        a.flatten_shape_x = a.flatten_shape_y = test ? 1 : 64;
        if (llm_run_attention(&a, q, kv, LLM_K_CACHE_BASE_ADDR, LLM_V_CACHE_BASE_ADDR, BYTES_KV_HEAD_CTX))
            llm_fail(11, "regression attention profile invalid");
        if (flex_get_cluster_id() == 0) {
            if (flex_is_dm_core()) {
                flex_dma_async_1d(local(0), LLM_O_HM_ADDR, LLM_N_HEAD * q * LLM_HEAD_DIM * 2);
                flex_dma_async_wait_all();
            }
            flex_intra_cluster_sync();
            if (flex_get_core_id() == 0) {
                volatile uint16_t *buf = (volatile uint16_t *)local(0);
                for (uint32_t head = 0; head < LLM_N_HEAD; ++head)
                    for (uint32_t row = 0; row < q; ++row)
                        for (uint32_t d = 0; d < LLM_HEAD_DIM; ++d) {
                            uint16_t value = buf[(head * q + row) * LLM_HEAD_DIM + d];
                            float delta = fp16_to_float(value) - (float)(head + 1);
                            if ((value & 0x7c00u) == 0x7c00u || delta > 0.02f || delta < -0.02f) {
                                printf("[LLMForward] Attention mismatch test=%u head=%u row=%u col=%u actual=%x\n", test, head, row, d, value);
                                llm_fail(12, "multi-head attention mismatch");
                            }
                        }
            }
            flex_intra_cluster_sync();
        }
        flex_global_barrier_xy();
        if (flex_get_cluster_id() == 0 && flex_get_core_id() == 0)
            printf("[LLMForward] Attention q=%u kv=%u heads=%u: PASS\n", q, kv, LLM_N_HEAD);
    }
    flex_global_barrier_xy();
    if (flex_get_cluster_id() == 0 && flex_get_core_id() == 0)
        printf("[LLMForward] Kernel regression: PASS\n");
    flex_global_barrier_xy();
}
