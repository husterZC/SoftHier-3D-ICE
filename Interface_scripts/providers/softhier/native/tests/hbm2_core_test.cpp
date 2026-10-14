#include "../softhier_hbm2_core.hpp"
#include <cassert>
#include <iostream>
#include <limits>
using namespace softhier_hbm2;

static void close(double actual, double expected) {
    if (std::abs(actual - expected) > 1e-12 * std::max(1e-9, std::abs(expected))) {
        std::cerr << actual << " != " << expected << std::endl;
        std::abort();
    }
}
static double total(const std::vector<Energy> &values) {
    double result = 0; for (const auto &v : values) result += v.commands() + v.background();
    return result;
}
static Profile profile() { return {{1, 3, .5, .25, .1}, {2e-9, 1e-9, 4e-9, 5e-9, 9e-9, 2e-9}}; }
static Timing timing() { return {1e-9, 10, 4, 2, 2, 3}; }
template<class F> void rejects(F function) {
    bool threw = false; try { function(); } catch (const std::runtime_error &) { threw = true; }
    assert(threw);
}
int main() {
    // Idle includes every pseudo-channel and fractional final time, idempotently.
    Core idle(profile(), timing(), 2, 4);
    close(total(idle.snapshot(10.5)), 21e-9);
    close(total(idle.snapshot(10.5)), 21e-9);
    rejects([&]{ idle.snapshot(10); });
    rejects([&]{ idle.command(5, Command::ACT); });

    // A future command cannot leak into an earlier thermal interval.
    Core future(profile(), timing(), 1, 4);
    future.command(10, Command::ACT);
    close(total(future.snapshot(5)), 5e-9);
    close(total(future.snapshot(11)), (10 + 2 + 3)*1e-9);

    // Read/write, explicit precharge, all-/per-bank refresh and conservation.
    Core split(profile(), timing(), 1, 4), whole(profile(), timing(), 1, 4);
    for (Core *model : {&split, &whole}) {
        model->command(2, Command::ACT);
        model->command(3, Command::RD);
        model->command(4, Command::WR);
        model->command(12, Command::PRE);
        model->command(15, Command::REFAB);
        model->command(20, Command::REFPB, 0, 3);
    }
    double previous = 0, sum = 0;
    for (double time : {4., 9., 16., 23.5}) {
        double current = total(split.snapshot(time)); sum += current - previous; previous = current;
    }
    close(sum, total(whole.snapshot(23.5)));
    close(sum, (2 + 30 + 11.5 + 2 + 4 + 5 + 1 + 9 + 2)*1e-9);

    // Automatic precharge respects tRAS (read) and write recovery (write).
    Core auto_read(profile(), timing(), 1, 4);
    auto_read.command(2, Command::ACT);
    auto_read.command(3, Command::RDA);
    close(total(auto_read.snapshot(11)), (2 + 27 + 2 + 4)*1e-9);
    close(total(auto_read.snapshot(13)), (2 + 30 + 1 + 2 + 4 + 1)*1e-9);
    Core auto_write(profile(), timing(), 1, 4);
    auto_write.command(0, Command::ACT);
    auto_write.command(10, Command::WRA);
    close(total(auto_write.snapshot(18)), (17*3 + 1 + 2 + 5 + 1)*1e-9);

    // Power down/self refresh count background; self-refresh current already
    // includes its internal refresh, so do not synthesize extra REF commands.
    Core low(profile(), timing(), 1, 4);
    low.command(2, Command::PDEP); low.command(6, Command::PDXP);
    low.command(8, Command::SREFEN); low.command(12, Command::SREFEX);
    close(total(low.snapshot(14)), (2 + 4*.25 + 2 + 4*.1 + 2)*1e-9);
    Core active_down(profile(), timing(), 1, 4);
    active_down.command(0, Command::ACT); active_down.command(2, Command::PDEA);
    active_down.command(6, Command::PDXA); active_down.command(10, Command::PRE);
    close(total(active_down.snapshot(12)), (2 + 2*3 + 4*.5 + 4*3 + 1 + 2)*1e-9);

    // Independent instances do not share counters; preload has no command API
    // side effect and thus cannot create dynamic energy at time zero.
    Core untouched(profile(), timing(), 1, 4);
    close(total(untouched.snapshot(0)), 0);
    close(total(untouched.snapshot(14)), 14e-9);
    Core illegal(profile(), timing(), 1, 4);
    illegal.command(0, Command::RD);
    rejects([&]{ illegal.snapshot(1); });
    rejects([&]{ untouched.snapshot(std::numeric_limits<double>::infinity()); });
    std::cout << "HBM2 core accounting: PASS\n";
}
