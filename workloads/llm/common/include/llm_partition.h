#ifndef LLM_PARTITION_H
#define LLM_PARTITION_H
#include <stdint.h>
/* Whole-row ownership, including fewer rows than vector units. */
static inline uint32_t llm_row_count(uint32_t rows, uint32_t workers, uint32_t sid)
{
    return workers && sid < workers ? rows / workers + (sid < rows % workers) : 0;
}
static inline uint32_t llm_row_start(uint32_t rows, uint32_t workers, uint32_t sid)
{
    if (!workers || sid >= workers) return rows;
    uint32_t remainder = rows % workers;
    return sid * (rows / workers) + (sid < remainder ? sid : remainder);
}
#endif
