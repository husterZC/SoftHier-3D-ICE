/* DRAMSys stays independently linkable. GVSoC registers its shared timer
 * through this optional C ABI when direct accounting timing is requested.
 */
#ifndef SOFTHIER_DRAM_POWER_TIMING_HPP
#define SOFTHIER_DRAM_POWER_TIMING_HPP
#include <cstdlib>
using CloDramEnter = int (*)();
using CloDramLeave = void (*)(int);
extern CloDramEnter clo_dram_enter;
extern CloDramLeave clo_dram_leave;
extern "C" int softhier_dram_set_power_timing_callbacks(CloDramEnter, CloDramLeave);
class CloDramPowerScope {
    int entered = 0;
public:
    CloDramPowerScope() { if (clo_dram_enter) entered = clo_dram_enter(); }
    ~CloDramPowerScope() { if (clo_dram_leave) clo_dram_leave(entered); }
    CloDramPowerScope(const CloDramPowerScope &) = delete;
    CloDramPowerScope &operator=(const CloDramPowerScope &) = delete;
};
#ifdef CLO_DRAM_TIMING_IMPLEMENTATION
CloDramEnter clo_dram_enter = nullptr;
CloDramLeave clo_dram_leave = nullptr;
extern "C" int softhier_dram_set_power_timing_callbacks(CloDramEnter enter, CloDramLeave leave)
{
    if (!enter || !leave || (clo_dram_enter && (clo_dram_enter != enter || clo_dram_leave != leave)))
        return -1;
    clo_dram_enter = enter;
    clo_dram_leave = leave;
    return 1; // ABI version
}
#endif
#endif
