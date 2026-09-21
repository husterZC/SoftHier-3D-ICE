#pragma once
#include "llm_common.h"
#define LLM_PREFILL_PROMPT_LEN LLM_T
#define LLM_PREFILL_DEBUG_DUMP 1

static inline void llm_init_attn_prefill(LLMAttentionRuntimeArgs *args)
{
    llm_common_attn_profile_default(args);
}
