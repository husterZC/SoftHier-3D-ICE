/* Included exactly once, by the staged engine's power_trace.cpp. */
#include "power_accounting_timing.h"
#include "closed_loop_timing.h"
#include <cstring>
#include <sys/syscall.h>

CloPowerTimingState clo_power_timing_state;
thread_local bool clo_power_thread_checked = false;
static bool clo_dram_registered = false;

static void clo_power_fail(const char *message)
{
    fprintf(stderr, "power-accounting timing: %s\n", message);
    abort();
}
static bool clo_power_flag(const char *name)
{
    const char *value = getenv(name);
    if (!value || !value[0] || !strcmp(value, "0")) return false;
    if (strcmp(value, "1")) clo_power_fail("timing environment flags must be 0 or 1");
    return true;
}
extern "C" int clo_power_timing_abi_version() { return 1; }
extern "C" int clo_power_timing_requested()
{
    static const bool enabled = clo_power_flag("CLO_POWER_ACCOUNTING_TIMING");
    return enabled;
}
static bool clo_power_window_selected(int diagnostic)
{
    static const bool external = clo_power_flag("CLO_POWER_TIMING_EXTERNAL_WINDOW");
    return clo_power_timing_requested() && external == bool(diagnostic);
}
void clo_power_check_thread()
{
    if (syscall(SYS_gettid) != clo_power_timing_state.owner_tid)
        clo_power_fail("concurrent accounting threads cannot be attributed as exclusive wall time");
    clo_power_thread_checked = true;
}
void clo_power_segment_begin() { clo_power_timing_state.segment_ns = clo_now(); }
void clo_power_segment_end()
{
    auto &s = clo_power_timing_state;
    s.elapsed_ns += clo_now() - s.segment_ns;
}
extern "C" int clo_power_timing_enter() { return clo_power_enter_inline(); }
extern "C" void clo_power_timing_leave(int entered) { clo_power_leave_inline(entered); }
extern "C" void clo_power_timing_dram_registered() { clo_dram_registered = true; }

CloPowerPause::CloPowerPause() : entered(clo_power_timing_state.active)
{
    auto &s = clo_power_timing_state;
    if (entered) {
        if (!clo_power_thread_checked) clo_power_check_thread();
        if (s.suspended++ == 0 && s.depth) clo_power_segment_end();
    }
}
CloPowerPause::~CloPowerPause()
{
    auto &s = clo_power_timing_state;
    if (entered && --s.suspended == 0 && s.depth) clo_power_segment_begin();
}

extern "C" void clo_power_window_begin(int64_t host_ns, int64_t sim_ps, int diagnostic)
{
    if (!clo_power_window_selected(diagnostic)) return;
    auto &s = clo_power_timing_state;
    if (clo_fd() < 0) clo_power_fail("CLO_TIMING_DIR is required when timing is enabled");
    if (s.active || s.depth || s.suspended) clo_power_fail("overlapping measurement windows");
    s = CloPowerTimingState();
    s.start_ns = host_ns;
    s.start_ps = sim_ps;
    s.owner_tid = syscall(SYS_gettid);
    clo_power_thread_checked = true;
    s.active = true;
}
extern "C" void clo_power_window_end(int64_t host_ns, int64_t sim_ps, int diagnostic)
{
    if (!clo_power_window_selected(diagnostic)) return;
    auto &s = clo_power_timing_state;
    if (!s.active || s.depth || s.suspended) clo_power_fail("unbalanced measurement window or scope");
    clo_power_check_thread();
    s.active = false;
    if (host_ns <= s.start_ns || sim_ps < s.start_ps || s.elapsed_ns < 0 ||
        s.elapsed_ns > host_ns - s.start_ns) clo_power_fail("invalid accumulated duration");
    // A scalar counter, NOT an interval occupying the entire workload.
    char line[1024];
    int n = snprintf(line, sizeof(line),
        "{\"actor\":\"gvsoc\",\"phase\":\"inline_power_accounting\",\"kind\":\"counter\","
        "\"method_version\":1,\"pid\":%ld,\"start_ns\":%lld,\"end_ns\":%lld,"
        "\"sim_ps\":%lld,\"value\":%lld,\"calls\":%llu,\"window_start_ns\":%lld,"
        "\"window_end_ns\":%lld,\"window_start_ps\":%lld,\"window_end_ps\":%lld,"
        "\"scope\":\"%s\",\"dram_callbacks_registered\":%s}\n",
        (long)getpid(), (long long)host_ns, (long long)host_ns, (long long)sim_ps,
        (long long)s.elapsed_ns, (unsigned long long)s.calls, (long long)s.start_ns,
        (long long)host_ns, (long long)s.start_ps, (long long)sim_ps,
        diagnostic ? "diagnostic_window" : "roi", clo_dram_registered ? "true" : "false");
    if (n <= 0 || n >= (int)sizeof(line) || write(clo_fd(), line, n) != n)
        clo_power_fail("cannot write counter summary");
}
