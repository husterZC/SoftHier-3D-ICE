#include "llm_common.h"
#include "llm_prefill_cfg.h"

extern void flex_barrier_xy_init(void);
extern void flex_global_barrier_xy(void);
extern void flex_eoc(uint32_t val);

int main()
{
    uint32_t eoc_val = 0;
    const uint32_t prompt_len = (uint32_t)LLM_PREFILL_PROMPT_LEN;
    LLMAttentionRuntimeArgs prefill_attn;

    LLMRuntimeState state;
    llm_common_init_runtime(&state, prompt_len, 0, (uint32_t)LLM_MAX_CTX);
    llm_init_attn_prefill(&prefill_attn);

    flex_barrier_xy_init();
    flex_global_barrier_xy();
    llm_common_init_accelerators();

    // Initialize all layers so prefill is deterministic.
    for (uint32_t layer = 0; layer < (uint32_t)LLM_NUM_LAYERS; ++layer)
        llm_common_init_dummy_weights(layer);

    llm_common_init_hidden_state();
    flex_global_barrier_xy();
    llm_common_log_start();

    for (uint32_t layer = 0; layer < (uint32_t)LLM_NUM_LAYERS; ++layer)
    {
        flex_global_barrier_xy();
        // Prefill runs full-sequence attention: q_len == kv_len == prompt_len.
        llm_common_run_prefill_layer(layer, state.prompt_len, state.prompt_len, &prefill_attn);
        llm_common_store_prefill_kv_cache(layer, state.prompt_len);
        llm_common_log_layer_done(layer);
    }

    // After prefill, cache contains the entire prompt.
    if (!llm_common_cache_append(&state, state.prompt_len))
    {
        eoc_val = 2;
        goto finish;
    }

    llm_common_validate_hidden();

#if LLM_PREFILL_DEBUG_DUMP
    llm_common_dma_dump_u16((uint64_t)LLM_H_ADDR, 8);
    llm_common_dma_dump_u16(llm_k_cache_head_token_addr(0, 0, 0), 8);
    llm_common_dma_dump_u16(llm_v_cache_head_token_addr(0, 0, 0), 8);
#endif

finish:
    flex_global_barrier_xy();
    llm_common_finish(eoc_val);
    return (int)eoc_val;
}
