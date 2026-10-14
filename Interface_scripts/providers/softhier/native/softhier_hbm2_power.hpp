// SPDX-License-Identifier: Apache-2.0
// Provider extension for DRAMPower 5.4.1. This is an HBM2 core backend;
// it never constructs DDR4 or computes controller/PHY/interface energy.
#pragma once
#include "softhier_hbm2_core.hpp"
#include "DRAMSys/configuration/memspec/MemSpecHBM2.h"
#include <DRAMPower/dram/dram_base.h>
#include <nlohmann/json.hpp>
#include <cstdlib>
#include <fstream>
#include <set>

namespace DRAMPower {
class SoftHierHBM2 final : public dram_base<CmdType> {
    static double number(const nlohmann::json &value, const char *name, bool positive = false) {
        if (!value.is_number()) throw std::runtime_error(std::string("HBM2 invalid number: ") + name);
        const double result = value.get<double>();
        if (!std::isfinite(result) || (positive ? result <= 0 : result < 0))
            throw std::runtime_error(std::string("HBM2 invalid number: ") + name);
        return result;
    }
    static softhier_hbm2::Profile load_profile() {
        const char *path = std::getenv("SOFTHIER_DRAM_POWER_CONFIG");
        if (!path || !*path) throw std::runtime_error("HBM2 power requires SOFTHIER_DRAM_POWER_CONFIG");
        std::ifstream stream(path);
        if (!stream) throw std::runtime_error("cannot open HBM2 electrical profile");
        const auto config = nlohmann::json::parse(stream);
        if (config.at("schema_version") != 1 || config.at("memory_type") != "HBM2" ||
            config.at("scope") != "per_pseudo_channel" || config.value("synthetic", false))
            throw std::runtime_error("incompatible or synthetic HBM2 electrical profile");
        if (!config.at("provenance").is_string() || config.at("provenance").get<std::string>().empty())
            throw std::runtime_error("HBM2 electrical profile needs provenance");
        number(config.at("characterization_temperature_k"), "characterization_temperature_k", true);
        const auto &rails = config.at("rails");
        if (!rails.is_array() || rails.empty()) throw std::runtime_error("HBM2 profile needs core rails");
        std::set<std::string> names;
        softhier_hbm2::Profile result;
        const std::array<const char *, 5> states{{"precharged", "active", "power_down_active", "power_down_precharged", "self_refresh"}};
        const std::array<const char *, 6> commands{{"ACT", "PRE", "RD", "WR", "REFAB", "REFPB"}};
        for (const auto &rail : rails) {
            const auto name = rail.at("name").get<std::string>();
            if (name.empty() || !names.insert(name).second) throw std::runtime_error("invalid HBM2 rail name");
            const double voltage = number(rail.at("voltage_v"), "voltage_v", true);
            const auto &background = rail.at("background_current_a");
            const auto &charges = rail.at("command_charge_c");
            if (!background.is_object() || background.size() != states.size() ||
                !charges.is_object() || charges.size() != commands.size())
                throw std::runtime_error("HBM2 profile requires exact state and command fields");
            for (size_t i = 0; i < states.size(); ++i)
                result.background_w[i] += voltage * number(background.at(states[i]), states[i]);
            for (size_t i = 0; i < commands.size(); ++i)
                result.command_j[i] += voltage * number(charges.at(commands[i]), commands[i]);
        }
        return result;
    }
    static softhier_hbm2::Timing timing(const DRAMSys::MemSpecHBM2 &spec) {
        return {spec.tCK.to_seconds(), spec.tRAS / spec.tCK, spec.tRTP / spec.tCK,
                spec.tWL / spec.tCK, static_cast<double>(spec.defaultBurstLength) / spec.dataRate, spec.tWR / spec.tCK};
    }
    softhier_hbm2::Core core;
    size_t ranks, banks;
    double cycle_picoseconds;
    template<CmdType Type> void route(softhier_hbm2::Command command) {
        routeCommand<Type>([this, command](const Command &cmd) {
            // DRAMSys rank-wide refresh/power commands do not carry meaningful
            // bank coordinates (its generic converter can underflow for rank>0).
            constexpr bool bank_command = Type == CmdType::ACT || Type == CmdType::PRE ||
                Type == CmdType::RD || Type == CmdType::WR || Type == CmdType::RDA ||
                Type == CmdType::WRA || Type == CmdType::REFB;
            core.command(cmd.timestamp, command, cmd.targetCoordinate.rank,
                         bank_command ? cmd.targetCoordinate.bank : 0);
        });
    }
    timestamp_t update_toggling_rate(timestamp_t timestamp,
        const std::optional<DRAMUtils::Config::ToggleRateDefinition> &) override { return timestamp; }
public:
    explicit SoftHierHBM2(const DRAMSys::MemSpecHBM2 &spec)
        : dram_base<CmdType>({}), core(load_profile(), timing(spec), spec.ranksPerChannel, spec.banksPerRank),
          ranks(spec.ranksPerChannel), banks(spec.banksPerRank), cycle_picoseconds(spec.tCK / sc_core::sc_time(1, sc_core::SC_PS)) {
        using C = softhier_hbm2::Command;
        route<CmdType::ACT>(C::ACT); route<CmdType::PRE>(C::PRE); route<CmdType::PREA>(C::PREA);
        route<CmdType::RD>(C::RD); route<CmdType::WR>(C::WR); route<CmdType::RDA>(C::RDA); route<CmdType::WRA>(C::WRA);
        route<CmdType::REFA>(C::REFAB); route<CmdType::REFB>(C::REFPB);
        route<CmdType::PDEA>(C::PDEA); route<CmdType::PDEP>(C::PDEP);
        route<CmdType::PDXA>(C::PDXA); route<CmdType::PDXP>(C::PDXP);
        route<CmdType::SREFEN>(C::SREFEN); route<CmdType::SREFEX>(C::SREFEX);
        route<CmdType::NOP>(C::NOP); route<CmdType::END_OF_SIMULATION>(C::NOP);
        auto unsupported = [](const Command &) { throw std::runtime_error("unsupported command in HBM2 core power backend"); };
        routeCommand<CmdType::REFP2B>(unsupported); routeCommand<CmdType::PRESB>(unsupported);
        routeCommand<CmdType::REFSB>(unsupported); routeCommand<CmdType::DSMEN>(unsupported); routeCommand<CmdType::DSMEX>(unsupported);
        // Initialise base last_command_time before a first idle-only report.
        doCoreCommand(Command(0, CmdType::NOP));
    }
    void snapshot_ps(uint64_t time_ps, double &command_j, double &background_j) {
        const auto &values = core.snapshot(static_cast<double>(time_ps) / cycle_picoseconds);
        command_j = background_j = 0;
        for (const auto &value : values) {
            command_j += value.commands(); background_j += value.background();
        }
    }
    energy_t calcCoreEnergy(timestamp_t timestamp) override {
        // Existing DRAMSys's end report may ask for the last command timestamp,
        // which predates an idle-only hook snapshot. Reports use the later bound.
        const auto &values = core.snapshot(std::max<double>(timestamp, core.sampled_cycle()));
        energy_t result(ranks * banks);
        for (size_t rank = 0; rank < ranks; ++rank) {
            const auto &value = values[rank];
            // The electrical profile is per pseudo-channel; report shared energy
            // once in its first bank, without multiplying by banks/devices/stacks.
            auto &entry = result.bank_energy[rank * banks];
            entry.E_act = value.command_j[0]; entry.E_pre = value.command_j[1];
            entry.E_RD = value.command_j[2]; entry.E_WR = value.command_j[3];
            entry.E_ref_AB = value.command_j[4]; entry.E_ref_PB = value.command_j[5];
            entry.E_bg_pre = value.background_j[0]; entry.E_bg_act = value.background_j[1];
            result.E_PDNA += value.background_j[2]; result.E_PDNP += value.background_j[3];
            result.E_sref += value.background_j[4];
        }
        return result;
    }
    interface_energy_info_t calcInterfaceEnergy(timestamp_t) override { return {}; }
    SimulationStats getStats() override {
        // DRAMSys does not consume this API. Failing explicitly avoids claiming
        // complete DRAMPower cycle statistics for this energy-only extension.
        throw std::runtime_error("HBM2 extension exposes energy, not detailed cycle statistics");
    }
    uint64_t getBankCount() override { return banks; }
    uint64_t getRankCount() override { return ranks; }
    uint64_t getDeviceCount() override { return 1; }
};
} // namespace DRAMPower
