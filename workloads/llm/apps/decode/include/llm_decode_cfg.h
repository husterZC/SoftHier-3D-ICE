#pragma once
#include "llm_common.h"
#define LLM_DECODE_QUERY_TOKENS 1u
#define LLM_DECODE_DEBUG_DUMP 1

static inline void llm_init_attn_decode(LLMAttentionRuntimeArgs *args)
{
    llm_common_attn_profile_default(args);
}
