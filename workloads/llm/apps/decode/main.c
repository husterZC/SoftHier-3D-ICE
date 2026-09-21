#include "llm_common.h"
#include "llm_decode_cfg.h"

extern void flex_barrier_xy_init(void);
extern void flex_global_barrier_xy(void);
extern void flex_eoc(uint32_t val);

int main()
{
    uint32_t eoc_val = 0;
    uint32_t query_tokens = (uint32_t)LLM_DECODE_QUERY_TOKENS;
    LLMAttentionRuntimeArgs decode_attn;


    LLMRuntimeState state;
    llm_common_init_runtime(&state,
                            0u,
                            (uint32_t)LLM_DECODE_STEPS,
                            (uint32_t)LLM_MAX_CTX);
    llm_init_attn_decode(&decode_attn);

    flex_barrier_xy_init();
    flex_global_barrier_xy();
    llm_common_init_accelerators();


    // Decode starts from an prepared cache length.
    state.cache_len = (uint32_t)LLM_DECODE_INIT_CACHE_LEN;
    if (state.cache_len > state.max_ctx_len)
    {
        eoc_val = 3;
        goto finish;
    }


    // Initialize all layers so decode behavior is deterministic.
    for (uint32_t layer = 0; layer < (uint32_t)LLM_NUM_LAYERS; ++layer)
        llm_common_init_dummy_weights(layer);

    llm_common_init_synthetic_cache(state.cache_len);
    llm_common_init_hidden_state();
    flex_global_barrier_xy();
    llm_common_log_start();

    for (uint32_t step = 0; step < state.decode_steps; ++step)
    {
        if (!llm_common_cache_can_append(&state, query_tokens))
        {
            eoc_val = 3;
            break;
        }

        // Current step attends to all cached tokens plus new query_tokens.
        const uint32_t kv_len = state.cache_len + query_tokens;

        for (uint32_t layer = 0; layer < (uint32_t)LLM_NUM_LAYERS; ++layer)
        {
            flex_global_barrier_xy();
            // Decode executes a configurable query chunk against growing KV context.
            // The decode layer appends current-step K/V internally before attention.
            llm_common_run_decode_layer(layer, query_tokens, kv_len, &decode_attn);
            llm_common_log_layer_done(layer);
        }

        if (!llm_common_cache_append(&state, query_tokens))
        {
            eoc_val = 4;
            break;
        }
    }

    llm_common_validate_hidden();

#if LLM_DECODE_DEBUG_DUMP
    llm_common_dma_dump_u16((uint64_t)LLM_H_ADDR, 8);
    if (state.cache_len > 0u)
    {
        const uint32_t last_token = state.cache_len - 1u;
        llm_common_dma_dump_u16(llm_k_cache_head_token_addr(0, 0, last_token), 8);
        llm_common_dma_dump_u16(llm_v_cache_head_token_addr(0, 0, last_token), 8);
    }
#endif

finish:
    flex_global_barrier_xy();
    llm_common_finish(eoc_val);
    return (int)eoc_val;
}
