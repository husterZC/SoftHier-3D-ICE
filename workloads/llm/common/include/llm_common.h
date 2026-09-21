#ifndef _LLM_COMMON_H_
#define _LLM_COMMON_H_

#include <stdint.h>

#include "llm_layout.h"
#include "llm_weights.h"

typedef struct LLMRuntimeState
{
    uint32_t prompt_len;    // Prefill token count.
    uint32_t decode_steps;  // Number of decode iterations to run.
    uint32_t cache_len;     // Current KV cache length.
    uint32_t max_ctx_len;   // Cap for cache growth.
} LLMRuntimeState;

typedef struct LLMAttentionRuntimeArgs
{
    // FlatAttention runtime knobs
    // Call llm_common_attn_profile_default() first, then override 
    uint32_t speculative_length;
    uint32_t head_dimension;
    uint32_t num_head;
    uint32_t num_head_group;
    uint32_t batch_size;
    uint32_t flatten_scale_x;
    uint32_t flatten_scale_y;
    uint32_t flatten_shape_x;
    uint32_t flatten_shape_y;
    uint32_t async_enable;
    uint32_t dump_enable;
} LLMAttentionRuntimeArgs;

// Initialize runtime state with optional max-context override.
void llm_common_init_runtime(LLMRuntimeState *state,
                             uint32_t prompt_len,
                             uint32_t decode_steps,
                             uint32_t max_ctx_len);

// Query whether cache_len can grow by append_tokens.
uint32_t llm_common_cache_can_append(const LLMRuntimeState *state, uint32_t append_tokens);
// Apply cache growth when valid; returns 1 on success, 0 on overflow/invalid state.
uint32_t llm_common_cache_append(LLMRuntimeState *state, uint32_t append_tokens);

void llm_common_init_accelerators(void);
void llm_common_init_synthetic_cache(uint32_t valid_tokens);
void llm_common_validate_hidden(void);
__attribute__((noreturn)) void llm_common_finish(uint32_t code);
void llm_common_kernel_regression(void);

// Data/weight initialization helpers for micro-bench style runs.
void llm_common_init_hidden_state(void);
void llm_common_init_dummy_weights(uint32_t layer_id);
// Fill attention args with fallback defaults from attn.h.
void llm_common_attn_profile_default(LLMAttentionRuntimeArgs *args);

// Layer primitives used by prefill/decode applications.
// If attn_args is NULL, fallback defaults from attn.h are used.
void llm_common_run_prefill_layer(uint32_t layer_id,
                                  uint32_t q_len,
                                  uint32_t kv_len,
                                  const LLMAttentionRuntimeArgs *attn_args);
// If attn_args is NULL, fallback defaults from attn.h are used.
void llm_common_run_decode_layer(uint32_t layer_id,
                                 uint32_t q_len,
                                 uint32_t kv_len,
                                 const LLMAttentionRuntimeArgs *attn_args);
// Persist the current layer K/V tensors into shared KV cache layout.
void llm_common_store_prefill_kv_cache(uint32_t layer_id, uint32_t kv_len);
// Append decode-step K/V slices into cache at [cache_pos, cache_pos + append_len).
// Note: llm_common_run_decode_layer() now does this internally before attention.
// Keep this API for explicit/manual cache writes in specialized flows.
void llm_common_store_decode_kv_cache(uint32_t layer_id, uint32_t cache_pos, uint32_t append_len);

// Debug utility: dump fp16 values from HBM.
void llm_common_dma_dump_u16(uint64_t hbm_addr, uint32_t n_halfwords);

void llm_common_log_start(void);
void llm_common_log_layer_done(uint32_t layer_id);

#endif
