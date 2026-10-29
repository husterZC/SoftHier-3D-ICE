/*
 * Copyright (C) 2020 GreenWaves Technologies, SAS, ETH Zurich and
 *                    University of Bologna
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
// Bounded adaptation of GVSoC's systemc_driver.cpp (Germain Haugou).
// Uses the installed engine without editing it or replacing installed binaries.
#include <gv/gvsoc.hpp>
#include <vp/controller.hpp>
#include <vp/top.hpp>
#include <vp/clock/clock_engine.hpp>
#include <vp/power_accounting_timing.h>
#include <systemc.h>
#include <algorithm>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <sys/resource.h>
#include <time.h>

static void mark(const char *phase, int64_t simulated_ps)
{
    timespec now;
    rusage usage;
    clock_gettime(CLOCK_MONOTONIC, &now);
    int64_t host_ns = (int64_t)now.tv_sec * 1000000000LL + now.tv_nsec;
    if (!strcmp(phase, "window_start")) clo_power_window_begin(host_ns, simulated_ps, 1);
    if (!strcmp(phase, "window_end")) clo_power_window_end(host_ns, simulated_ps, 1);
    getrusage(RUSAGE_SELF, &usage);
    FILE *file = fopen("diagnostic_events.jsonl", "a");
    if (!file) std::abort();
    fprintf(file, "{\"phase\":\"%s\",\"sim_ps\":%lld,\"host_ns\":%lld,"
                  "\"cpu_us\":%lld,\"clock_cycles\":[", phase,
            (long long)simulated_ps, (long long)now.tv_sec * 1000000000 + now.tv_nsec,
            (long long)(usage.ru_utime.tv_sec + usage.ru_stime.tv_sec) * 1000000 +
                usage.ru_utime.tv_usec + usage.ru_stime.tv_usec);
    bool first = true;
    for (auto *clock : gv::Controller::get().top_get()->get_time_engine()->get_clock_engines())
    {
        fprintf(file, "%s%lld", first ? "" : ",", (long long)clock->get_cycles());
        first = false;
    }
    fputs("]}\n", file);
    fclose(file);
}

class Diagnostic : public sc_core::sc_module, public gv::Gvsoc_user
{
public:
    SC_HAS_PROCESS(Diagnostic);
    Diagnostic(sc_core::sc_module_name name, gv::ControllerClient *client,
               int64_t start, int64_t window)
        : sc_module(name), client(client), start(start), window(window)
    {
        SC_THREAD(run);
    }
    void was_updated() override { sync.notify(); }
    void has_ended(int) override { ended = true; sync.notify(); }
    void run()
    {
        auto &controller = gv::Controller::get();
        int phase = 0;
        while (true)
        {
            int64_t time = (int64_t)sc_core::sc_time_stamp().to_double();
            controller.step_until_sync(time, client);
            if (ended)
            {
                mark("unexpected_workload_end", time);
                sc_core::sc_stop();
                break;
            }
            int64_t boundary = start + phase * window;
            if (time == boundary)
            {
                if (phase == 0)
                {
                    mark("ready", time); raise(SIGSTOP);
                    mark("window_start", time);
                }
                else if (phase == 1)
                {
                    mark("window_end", time); raise(SIGSTOP);
                    mark("profile_start", time);
                }
                else
                {
                    mark("profile_end", time); raise(SIGSTOP);
                    sc_core::sc_stop();
                    break;
                }
                ++phase;
                boundary = start + phase * window;
            }
            int64_t next = client->get_next_event_time();
            controller.wait_runnable();
            int64_t until = next < 0 ? boundary : std::min(next, boundary);
            if (until < time) std::abort();
            wait(until - time, sc_core::SC_PS, sync);
        }
    }
    gv::ControllerClient *client;
    int64_t start, window;
    bool ended = false;
    sc_core::sc_event sync;
};

int sc_main(int, char **) { sc_core::sc_start(); return 0; }

int main(int argc, char **argv)
{
    if (argc != 4) return 2;
    gv::GvsocConf conf;
    conf.config_path = argv[1];
    conf.api_mode = gv::Api_mode::Api_mode_sync;
    auto *gvsoc = gv::gvsoc_new(&conf);
    gvsoc->open();
    gvsoc->start();
    Diagnostic module("Gvsoc_SystemC_wrapper", (gv::ControllerClient *)gvsoc,
                      std::strtoll(argv[2], nullptr, 10), std::strtoll(argv[3], nullptr, 10));
    gvsoc->bind(&module);
    sc_core::sc_elab_and_sim(0, nullptr);
    // This is an intentionally partial run. Do not claim guest program PASS.
    // close() flushes energy reports; the engine is already stopped by sc_stop.
    gvsoc->close();
    return module.ended ? 3 : 0;
}
