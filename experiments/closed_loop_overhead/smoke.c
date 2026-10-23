/* Small integration check, not the B8 benchmark workload. */
#include "flex_cluster_arch.h"
#include "flex_runtime.h"

int main(void)
{
    if (flex_get_cluster_id() != 0 || !flex_is_first_core()) return 0;
    volatile unsigned sum = 0;
    flex_timer_start();
    for (unsigned i = 0; i < 10000; ++i) sum += i;
    flex_timer_end();
    unsigned error = sum != 49995000;
    printf("CLO_SMOKE_%s sum=%u\n", error ? "FAIL" : "PASS", sum);
    flex_eoc(error);
    return error;
}
