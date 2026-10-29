/* Provider-owned, optional direct timing of inline power work.
 * One accumulator lives in libpulpvp (not in every model/shared library).
 * The current SystemC/GVSoC execution path is single-threaded. A second
 * accounting thread is rejected rather than summing overlapping wall time.
 */
#ifndef SOFTHIER_POWER_ACCOUNTING_TIMING_H
#define SOFTHIER_POWER_ACCOUNTING_TIMING_H
#include <stdint.h>

struct CloPowerTimingState {
    bool active = false;
    unsigned depth = 0, suspended = 0;
    int64_t start_ns = 0, start_ps = 0, segment_ns = 0, elapsed_ns = 0;
    uint64_t calls = 0;
    long owner_tid = 0;
};
extern CloPowerTimingState clo_power_timing_state;
extern thread_local bool clo_power_thread_checked;
void clo_power_check_thread();
void clo_power_segment_begin();
void clo_power_segment_end();

extern "C" {
int clo_power_timing_abi_version();
int clo_power_timing_requested();
int clo_power_timing_enter();
void clo_power_timing_leave(int entered);
void clo_power_timing_dram_registered();
void clo_power_window_begin(int64_t host_ns, int64_t sim_ps, int diagnostic);
void clo_power_window_end(int64_t host_ns, int64_t sim_ps, int diagnostic);
}

inline bool clo_power_enter_inline()
{
    auto &s = clo_power_timing_state;
    if (!s.active || s.suspended) return false;
    if (!clo_power_thread_checked) clo_power_check_thread();
    if (s.depth++ == 0) {
        ++s.calls;
        clo_power_segment_begin();
    }
    return true;
}
inline void clo_power_leave_inline(bool entered)
{
    if (entered && --clo_power_timing_state.depth == 0) clo_power_segment_end();
}
class CloPowerScope {
    bool entered;
public:
    CloPowerScope() : entered(clo_power_enter_inline()) {}
    ~CloPowerScope() { clo_power_leave_inline(entered); }
    CloPowerScope(const CloPowerScope &) = delete;
    CloPowerScope &operator=(const CloPowerScope &) = delete;
};
class CloPowerPause {
    bool entered;
public:
    CloPowerPause();
    ~CloPowerPause();
    CloPowerPause(const CloPowerPause &) = delete;
    CloPowerPause &operator=(const CloPowerPause &) = delete;
};
#endif
