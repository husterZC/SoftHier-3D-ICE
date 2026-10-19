// Copyright (C) 2026 ETH Zurich and University of Bologna
// SPDX-License-Identifier: Apache-2.0
#include "flex_cluster_arch.h"
#include "flex_runtime.h"

int main(void)
{
    if (flex_get_cluster_id() != 0 || !flex_is_first_core()) return 0;
    _Float16 *src = (_Float16 *)local(0x2000);
    float *dst = (float *)local(0x1000);
    for (unsigned i = 0; i < 32; ++i) src[i] = (_Float16)(i + 1);
    printf("SPATZ_CHAINING_BEGIN\n");

    // Match the first RMSNorm conversion: vmv.v.i is issued while the load
    // is pending. Its immediate leaves input register slot zero unused.
    unsigned vl;
    __asm__ volatile(
        "vsetvli %0, %1, e16, m2, ta, ma\n"
        "vle16.v v0, (%2)\n"
        "vmv.v.i v4, 0\n"
        "vfwadd.vv v8, v0, v4\n"
        "vsetvli zero, %0, e32, m4, ta, ma\n"
        "vse32.v v8, (%3)\n"
        // A scalar load waits for outstanding vector stores before checking.
        "lw t0, 0(%3)\naddi t0, t0, 0\nfence rw, rw"
        : "=&r"(vl) : "r"(32), "r"(src), "r"(dst)
        : "v0", "v1", "v4", "v5", "v8", "v9", "v10", "v11", "t0", "memory");

    unsigned errors = vl != 32;
    for (unsigned i = 0; i < 32; ++i) errors += dst[i] != (float)(i + 1);
    printf("SPATZ_CHAINING_%s vl=%u checked=32 errors=%u\n",
           errors ? "FAIL" : "PASS", vl, errors);
    flex_eoc(errors != 0);
    return errors != 0;
}
