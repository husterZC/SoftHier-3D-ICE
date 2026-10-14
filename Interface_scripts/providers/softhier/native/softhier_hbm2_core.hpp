// SPDX-License-Identifier: Apache-2.0
// HBM2 core accounting. Command energies are incremental above state background.
#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <queue>
#include <stdexcept>
#include <vector>

namespace softhier_hbm2 {
enum class Command { ACT, PRE, RD, WR, REFAB, REFPB, PREA, RDA, WRA,
                     PDEA, PDEP, PDXA, PDXP, SREFEN, SREFEX, NOP, AUTO_PRE };
enum class State { PRECHARGED, ACTIVE, PD_ACTIVE, PD_PRECHARGED, SELF_REFRESH };

struct Profile {
    // Per complete pseudo-channel, summed V*I [W] and V*Q [J] over core rails.
    std::array<double, 5> background_w{};
    std::array<double, 6> command_j{};
};
struct Timing {
    double cycle_s, ras, rtp, wl, burst, wr;
};
struct Energy {
    std::array<double, 6> command_j{};
    std::array<double, 5> background_j{};
    double commands() const {
        double result = 0; for (double e : command_j) result += e; return result;
    }
    double background() const {
        double result = 0; for (double e : background_j) result += e; return result;
    }
};

class Core {
    struct Bank { bool active = false; double activated = 0; uint64_t generation = 0; };
    struct Rank { std::vector<Bank> banks; State mode = State::PRECHARGED; };
    struct Event {
        double time; uint64_t sequence; Command command; size_t rank, bank;
        uint64_t generation = 0;
        bool operator<(const Event &other) const {
            return time == other.time ? sequence > other.sequence : time > other.time;
        }
    };
    Profile profile;
    Timing timing;
    std::vector<Rank> ranks;
    std::priority_queue<Event> events;
    std::vector<Energy> energy;
    double now = 0;
    uint64_t sequence = 0;

    bool active(const Rank &rank) const {
        for (const auto &bank : rank.banks) if (bank.active) return true;
        return false;
    }
    void require(bool condition, const char *message) const {
        if (!condition) throw std::runtime_error(message);
    }
    void integrate(double until) {
        const double seconds = (until - now) * timing.cycle_s;
        for (size_t i = 0; i < ranks.size(); ++i) {
            State state = ranks[i].mode;
            if (state == State::PRECHARGED)
                state = active(ranks[i]) ? State::ACTIVE : State::PRECHARGED;
            const auto index = static_cast<size_t>(state);
            energy[i].background_j[index] += seconds * profile.background_w[index];
        }
        now = until;
    }
    void charge(size_t rank, Command command) {
        const auto index = static_cast<size_t>(command);
        energy[rank].command_j.at(index) += profile.command_j.at(index);
    }
    void precharge(size_t rank, size_t bank) {
        auto &b = ranks.at(rank).banks.at(bank);
        if (b.active) { charge(rank, Command::PRE); b.active = false; ++b.generation; }
    }
    void process(const Event &event) {
        auto &rank = ranks.at(event.rank);
        auto &bank = rank.banks.at(event.bank);
        const auto command = event.command;
        if (command == Command::NOP) return;
        if (command == Command::AUTO_PRE) {
            if (bank.generation == event.generation) precharge(event.rank, event.bank);
            return;
        }
        if (command == Command::PDXA || command == Command::PDXP) {
            require(rank.mode == (command == Command::PDXA ? State::PD_ACTIVE : State::PD_PRECHARGED),
                    "HBM2 power-down exit without matching entry");
            rank.mode = State::PRECHARGED; return;
        }
        if (command == Command::SREFEX) {
            require(rank.mode == State::SELF_REFRESH, "HBM2 self-refresh exit without entry");
            rank.mode = State::PRECHARGED; return;
        }
        require(rank.mode == State::PRECHARGED, "HBM2 command while in a low-power state");
        switch (command) {
        case Command::ACT:
            require(!bank.active, "HBM2 ACT on an active bank");
            bank.active = true; bank.activated = event.time; ++bank.generation;
            charge(event.rank, command); break;
        case Command::PRE: precharge(event.rank, event.bank); break;
        case Command::PREA:
            for (size_t i = 0; i < rank.banks.size(); ++i) precharge(event.rank, i);
            break;
        case Command::RD: case Command::WR: case Command::RDA: case Command::WRA: {
            require(bank.active, "HBM2 read/write on a precharged bank");
            bool read = command == Command::RD || command == Command::RDA;
            charge(event.rank, read ? Command::RD : Command::WR);
            if (command == Command::RDA || command == Command::WRA) {
                double recovery = read ? timing.rtp : timing.wl + timing.burst + timing.wr;
                double pre_time = std::max(bank.activated + timing.ras, event.time + recovery);
                events.push({pre_time, sequence++, Command::AUTO_PRE, event.rank, event.bank, bank.generation});
            }
            break;
        }
        case Command::REFAB:
            require(!active(rank), "HBM2 all-bank refresh requires precharged banks");
            charge(event.rank, command); break;
        case Command::REFPB:
            require(!bank.active, "HBM2 per-bank refresh requires a precharged bank");
            charge(event.rank, command); break;
        case Command::PDEA:
            require(active(rank), "HBM2 active power-down requires an active bank");
            rank.mode = State::PD_ACTIVE; break;
        case Command::PDEP:
            require(!active(rank), "HBM2 precharged power-down requires precharged banks");
            rank.mode = State::PD_PRECHARGED; break;
        case Command::SREFEN:
            require(!active(rank), "HBM2 self-refresh requires precharged banks");
            rank.mode = State::SELF_REFRESH; break;
        default: throw std::runtime_error("unsupported HBM2 core command");
        }
    }
public:
    Core(Profile profile, Timing timing, size_t pseudo_channels, size_t banks_per_pseudo_channel)
        : profile(profile), timing(timing), ranks(pseudo_channels), energy(pseudo_channels) {
        require(pseudo_channels > 0 && banks_per_pseudo_channel > 0, "HBM2 topology cannot be empty");
        require(std::isfinite(timing.cycle_s) && timing.cycle_s > 0, "HBM2 clock period must be positive");
        for (double value : {timing.ras, timing.rtp, timing.wl, timing.burst, timing.wr})
            require(std::isfinite(value) && value >= 0, "HBM2 timing must be finite and nonnegative");
        for (double value : profile.background_w)
            require(std::isfinite(value) && value >= 0, "HBM2 background power must be nonnegative");
        for (double value : profile.command_j)
            require(std::isfinite(value) && value >= 0, "HBM2 command energy must be nonnegative");
        for (auto &rank : ranks) rank.banks.resize(banks_per_pseudo_channel);
    }
    void command(double cycle, Command command, size_t rank = 0, size_t bank = 0) {
        require(std::isfinite(cycle) && cycle >= now, "HBM2 command is earlier than the sampled boundary");
        require(rank < ranks.size() && bank < ranks[rank].banks.size(), "HBM2 command target out of range");
        events.push({cycle, sequence++, command, rank, bank});
    }
    const std::vector<Energy> &snapshot(double cycle) {
        require(std::isfinite(cycle) && cycle >= now, "HBM2 energy snapshot cannot move backwards");
        while (!events.empty() && events.top().time <= cycle) {
            const auto event = events.top(); events.pop();
            integrate(event.time); process(event);
        }
        integrate(cycle);
        return energy;
    }
    double sampled_cycle() const { return now; }
};
} // namespace softhier_hbm2
