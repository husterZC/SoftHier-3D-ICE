#ifndef _LLM_LAYOUT_H_
#define _LLM_LAYOUT_H_

#include <stdint.h>
#include "flex_cluster_arch.h"

// =======================================================
// Model hyperparameters
// =======================================================

// Working token length. Decode can override this to 1 while prefill keeps full length.
#ifndef LLM_T
#define LLM_T          2048
#endif
// Upper bound reserved for decode-phase cache growth.
#ifndef LLM_MAX_CTX
#define LLM_MAX_CTX    2048
#endif
#ifndef LLM_D_MODEL
#define LLM_D_MODEL    768
#endif
#ifndef LLM_D_FF
#define LLM_D_FF       3072
#endif
#ifndef LLM_N_HEAD
#define LLM_N_HEAD     6
#endif
#ifndef LLM_N_KV_HEAD
#define LLM_N_KV_HEAD  6
#endif
#define LLM_HEAD_DIM   (LLM_D_MODEL / LLM_N_HEAD)
#ifndef LLM_NUM_LAYERS
#define LLM_NUM_LAYERS 12
#endif
#ifndef LLM_ELEM_SIZE
#define LLM_ELEM_SIZE  2        // fp16 bytes
#endif

#if (LLM_D_MODEL % LLM_N_HEAD) != 0
#error "LLM_D_MODEL must be divisible by LLM_N_HEAD"
#endif


// Sizes in bytes
#define BYTES_H        ((uint64_t)LLM_T * (uint64_t)LLM_D_MODEL * (uint64_t)LLM_ELEM_SIZE)
#define BYTES_H_NORM   BYTES_H
#define BYTES_MLP_MID  ((uint64_t)LLM_T * (uint64_t)LLM_D_FF    * (uint64_t)LLM_ELEM_SIZE)
#define BYTES_MLP_OUT  BYTES_H

#define BYTES_HM_HEAD  ((uint64_t)LLM_T * (uint64_t)LLM_HEAD_DIM * (uint64_t)LLM_ELEM_SIZE)
#define BYTES_HM_ALL   ((uint64_t)LLM_N_HEAD * (uint64_t)BYTES_HM_HEAD)

// Persistent KV-cache footprint (head-major: [kv_head][ctx][head_dim]).
#define BYTES_KV_HEAD_CTX       ((uint64_t)LLM_MAX_CTX * (uint64_t)LLM_HEAD_DIM * (uint64_t)LLM_ELEM_SIZE)
#define BYTES_KV_LAYER          ((uint64_t)LLM_N_KV_HEAD * (uint64_t)BYTES_KV_HEAD_CTX)
#define BYTES_KV_ALL_LAYERS     ((uint64_t)LLM_NUM_LAYERS * (uint64_t)BYTES_KV_LAYER)
// Bytes written when appending one token for one layer across all KV heads.
#define BYTES_KV_TOKEN_ALL_HEAD ((uint64_t)LLM_N_KV_HEAD * (uint64_t)LLM_HEAD_DIM * (uint64_t)LLM_ELEM_SIZE)
#define BYTES_KV_TOKEN_PER_HEAD ((uint64_t)LLM_HEAD_DIM * (uint64_t)LLM_ELEM_SIZE)

// =======================================================
// Activation layout in HBM
// =======================================================
#define LLM_H_ADDR        ((uint64_t)ARCH_HBM_START_BASE)
#define LLM_H_NORM_ADDR   (LLM_H_ADDR       + BYTES_H)
#define LLM_MLP_MID_ADDR  (LLM_H_NORM_ADDR  + BYTES_H_NORM)
#define LLM_MLP_OUT_ADDR  (LLM_MLP_MID_ADDR + BYTES_MLP_MID)
#define LLM_ATTN_O_ADDR   (LLM_MLP_OUT_ADDR + BYTES_MLP_OUT)

// Attention scratch/work buffers
#define LLM_QKV_TM_ADDR      (LLM_ATTN_O_ADDR + BYTES_H)
#define LLM_Q_HM_ADDR        (LLM_QKV_TM_ADDR + BYTES_H)
#define LLM_K_HM_ADDR        (LLM_Q_HM_ADDR   + BYTES_HM_ALL)
#define LLM_V_HM_ADDR        (LLM_K_HM_ADDR   + BYTES_HM_ALL)
#define LLM_O_HM_ADDR        (LLM_V_HM_ADDR   + BYTES_HM_ALL)

// Reuse MLP_OUT as attention projection output
#define LLM_ATTN_PROJ_TM_ADDR  (LLM_MLP_OUT_ADDR)

// End of temporary activation/scratch region.
#define LLM_ACT_SCRATCH_END_ADDR ((uint64_t)LLM_O_HM_ADDR + (uint64_t)BYTES_HM_ALL)

// Persistent cache regions within this phase's independently initialized run.
#define LLM_K_CACHE_BASE_ADDR   (LLM_ACT_SCRATCH_END_ADDR)
#define LLM_V_CACHE_BASE_ADDR   (LLM_K_CACHE_BASE_ADDR + BYTES_KV_ALL_LAYERS)
#define LLM_KV_CACHE_END_ADDR   (LLM_V_CACHE_BASE_ADDR + BYTES_KV_ALL_LAYERS)


// Weight base (HBM): hbm_start_addr + hbm_node_addr_space * (2 * num_cluster_y + num_cluster_x)
#define LLM_W_BASE        ((uint64_t)ARCH_HBM_START_BASE + (uint64_t)ARCH_HBM_NODE_ADDR_SPACE * \
                           (2ULL * ARCH_NUM_CLUSTER_Y + ARCH_NUM_CLUSTER_X))


// Layer-major base pointers for persistent K/V cache.
static inline uint64_t llm_k_cache_layer_base(uint32_t layer_id)
{
    return (uint64_t)LLM_K_CACHE_BASE_ADDR + (uint64_t)layer_id * (uint64_t)BYTES_KV_LAYER;
}

static inline uint64_t llm_v_cache_layer_base(uint32_t layer_id)
{
    return (uint64_t)LLM_V_CACHE_BASE_ADDR + (uint64_t)layer_id * (uint64_t)BYTES_KV_LAYER;
}

// Address helpers for one [head_dim] token slice in K/V cache.
static inline uint64_t llm_k_cache_head_token_addr(uint32_t layer_id, uint32_t kv_head, uint32_t token_idx)
{
    return llm_k_cache_layer_base(layer_id)
           + (uint64_t)kv_head * (uint64_t)BYTES_KV_HEAD_CTX
           + (uint64_t)token_idx * (uint64_t)BYTES_KV_TOKEN_PER_HEAD;
}

static inline uint64_t llm_v_cache_head_token_addr(uint32_t layer_id, uint32_t kv_head, uint32_t token_idx)
{
    return llm_v_cache_layer_base(layer_id)
           + (uint64_t)kv_head * (uint64_t)BYTES_KV_HEAD_CTX
           + (uint64_t)token_idx * (uint64_t)BYTES_KV_TOKEN_PER_HEAD;
}


static inline uint32_t llm_cache_valid_head(uint32_t kv_head)
{
    return (kv_head < (uint32_t)LLM_N_KV_HEAD) ? 1u : 0u;
}

static inline uint32_t llm_cache_valid_token(uint32_t token_idx, uint32_t max_ctx_len)
{
    return (token_idx < max_ctx_len) ? 1u : 0u;
}

// True when appending append_len tokens will not exceed max_ctx_len.
static inline uint32_t llm_cache_can_append(uint32_t cache_len, uint32_t append_len, uint32_t max_ctx_len)
{
    if (cache_len > max_ctx_len)
        return 0u;
    return (append_len <= (max_ctx_len - cache_len)) ? 1u : 0u;
}

#endif // _LLM_LAYOUT_H_
