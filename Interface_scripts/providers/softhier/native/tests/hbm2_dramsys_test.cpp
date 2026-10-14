// Native ABI integration test. Link against the patched DRAMSys and SystemC.
#include <systemc>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>

struct GvsocMemspec {
    unsigned access_size, nb_channels, nb_pseudo_channels, nb_ranks,
             nb_bank_groups, nb_banks, nb_rows, nb_columns,
             channel_stride, rank_stride, bankgroup_stride, bank_stride,
             row_stride, column_stride;
};
extern "C" {
int add_dram(char *, char *, GvsocMemspec *);
int softhier_dram_energy_abi_version();
int dram_get_core_energy_j(int, uint64_t, double *, double *);
void dram_preload_byte(int, uint64_t, int);
void dram_write_buffer(int, int, int);
void dram_write_strobe(int, int, int);
void dram_send_req(int, uint64_t, uint64_t, uint64_t, uint64_t);
}
struct Energy { double commands, background; double total() const { return commands + background; } };
static Energy sample(int id) {
    Energy result{};
    uint64_t ps = sc_core::sc_time_stamp() / sc_core::sc_time(1, sc_core::SC_PS);
    assert(dram_get_core_energy_j(id, ps, &result.commands, &result.background) == 0);
    assert(std::isfinite(result.total()) && result.commands >= 0 && result.background >= 0);
    return result;
}
static void close(double actual, double expected) {
    assert(std::abs(actual - expected) <= 1e-10 * std::max(1e-12, std::abs(expected)));
}
int sc_main(int argc, char **argv) {
    if (argc != 6) { std::cerr << "usage: test resources config power_profile early_refresh self_refresh\n"; return 2; }
    setenv("SOFTHIER_DRAM_POWER_CONFIG", argv[3], 1);
    sc_core::sc_set_time_resolution(1, sc_core::SC_PS);
    assert(softhier_dram_energy_abi_version() == 1);
    GvsocMemspec spec{}, other_spec{};
    const int busy = add_dram(argv[1], argv[2], &spec);
    const int idle = add_dram(argv[1], argv[2], &other_spec);
    assert(spec.nb_channels == 1 && spec.nb_pseudo_channels == 2 && spec.access_size > 0);
    close(sample(busy).total(), 0);
    dram_preload_byte(busy, 0, 42);
    close(sample(busy).total(), 0); // Untimed preload has no dynamic cost.
    sc_core::sc_start(100, sc_core::SC_NS);
    const auto first = sample(busy);
    close(first.commands, 0);
    close(first.total(), sample(idle).total());
    assert(first.background > 0);
    for (unsigned i = 0; i < spec.access_size; ++i) dram_write_buffer(busy, i, i);
    dram_send_req(busy, 0, spec.access_size, 1, 0);
    sc_core::sc_start(300, sc_core::SC_NS);
    dram_send_req(busy, 0, spec.access_size, 0, 0);
    sc_core::sc_start(300, sc_core::SC_NS);
    const auto second = sample(busy);
    assert(second.commands > 0);
    if (std::atoi(argv[4])) {
        assert(sample(idle).commands > 0); // Per-bank refresh starts sooner.
        assert(second.commands > sample(idle).commands);
    } else {
        close(sample(idle).commands, 0); // Traffic cannot heat the other instance.
    }
    close(sample(busy).total(), second.total()); // Same-time snapshots are idempotent.
    // A masked write exercises the second pseudo-channel and the MWR/MWRA
    // conversion. Its core energy remains a full write burst.
    for (unsigned i = 0; i < spec.access_size; ++i) {
        dram_write_buffer(busy, i, i);
        dram_write_strobe(busy, i % 2, i);
    }
    dram_send_req(busy, spec.rank_stride, spec.access_size, 1, 1);
    sc_core::sc_start(300, sc_core::SC_NS);
    assert(sample(busy).commands > second.commands);
    sc_core::sc_start(4700, sc_core::SC_NS);
    const auto third = sample(busy);
    if (std::atoi(argv[5])) {
        close(sample(idle).commands, 0); // Self refresh is included in background.
        assert(sample(idle).background > 0);
    } else {
        assert(sample(idle).commands > 0); // Autonomous explicit refresh is included.
    }
    assert(third.commands >= second.commands && third.background > second.background);
    close(first.total() + (second.total() - first.total()) + (third.total() - second.total()), third.total());
    sc_core::sc_start(0.5, sc_core::SC_NS);
    const auto final = sample(busy);
    assert(final.background > third.background); // Final fractional DRAM cycle.
    double commands, background;
    assert(dram_get_core_energy_j(-1, 0, &commands, &background) == -1);
    std::cout << "HBM2 DRAMSys energy ABI: PASS (busy command " << final.commands
              << " J, background " << final.background << " J)\n";
    return 0;
}
