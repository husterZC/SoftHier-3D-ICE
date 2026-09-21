#ifndef _LLM_WEIGHTS_H_
#define _LLM_WEIGHTS_H_

#include <stdint.h>
#include "llm_layout.h"

// Keep every weight block 4KB-aligned for simple DMA transfers.
#define W_ALIGN_BYTES 4096ULL
#define LLM_ALIGN_UP(x,a) (((uint64_t)(x) + ((uint64_t)(a) - 1ULL)) & ~((uint64_t)(a) - 1ULL))

// ---- sizes (bytes) ----
// Matrix shapes:
// Q/K/V/O: [d_model, d_model], up: [d_model, d_ff], down: [d_ff, d_model].
#define BYTES_WQ      ((uint64_t)LLM_D_MODEL * (uint64_t)LLM_D_MODEL * (uint64_t)LLM_ELEM_SIZE)
#define BYTES_WK      BYTES_WQ
#define BYTES_WV      BYTES_WQ
#define BYTES_WO      BYTES_WQ
#define BYTES_W1_UP   ((uint64_t)LLM_D_MODEL * (uint64_t)LLM_D_FF    * (uint64_t)LLM_ELEM_SIZE)
#define BYTES_W2_DOWN ((uint64_t)LLM_D_FF    * (uint64_t)LLM_D_MODEL * (uint64_t)LLM_ELEM_SIZE)

// Per-layer packed layout in HBM: WQ | WK | WV | WO | W1 | W2.
#define OFF_WQ       (0ULL)
#define OFF_WK       (LLM_ALIGN_UP(OFF_WQ       + BYTES_WQ,      W_ALIGN_BYTES))
#define OFF_WV       (LLM_ALIGN_UP(OFF_WK       + BYTES_WK,      W_ALIGN_BYTES))
#define OFF_WO       (LLM_ALIGN_UP(OFF_WV       + BYTES_WV,      W_ALIGN_BYTES))
#define OFF_W1_UP    (LLM_ALIGN_UP(OFF_WO       + BYTES_WO,      W_ALIGN_BYTES))
#define OFF_W2_DOWN  (LLM_ALIGN_UP(OFF_W1_UP    + BYTES_W1_UP,   W_ALIGN_BYTES))

#define LLM_LAYER_W_STRIDE (LLM_ALIGN_UP(OFF_W2_DOWN + BYTES_W2_DOWN, W_ALIGN_BYTES))


// Resolve absolute addresses for a given transformer layer.
static inline uint64_t llm_w_layer_base(uint32_t layer_id)
{
    return (uint64_t)LLM_W_BASE + (uint64_t)layer_id * (uint64_t)LLM_LAYER_W_STRIDE;
}

static inline uint64_t llm_wq_addr(uint32_t layer_id)      { return llm_w_layer_base(layer_id) + OFF_WQ; }
static inline uint64_t llm_wk_addr(uint32_t layer_id)      { return llm_w_layer_base(layer_id) + OFF_WK; }
static inline uint64_t llm_wv_addr(uint32_t layer_id)      { return llm_w_layer_base(layer_id) + OFF_WV; }
static inline uint64_t llm_wo_addr(uint32_t layer_id)      { return llm_w_layer_base(layer_id) + OFF_WO; }
static inline uint64_t llm_w1_up_addr(uint32_t layer_id)   { return llm_w_layer_base(layer_id) + OFF_W1_UP; }
static inline uint64_t llm_w2_down_addr(uint32_t layer_id) { return llm_w_layer_base(layer_id) + OFF_W2_DOWN; }

#endif
