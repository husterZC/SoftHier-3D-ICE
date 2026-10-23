/* Optional host timing for experiments/closed_loop_overhead.
 * Shared with 3D-ICE/include/closed_loop_timing.h. No clocks or I/O when unset.
 * Each process appends bounded JSON records to its own file.
 */
#ifndef CLOSED_LOOP_TIMING_H
#define CLOSED_LOOP_TIMING_H
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <sys/resource.h>

static void clo_exit_record(void);

static inline int clo_fd(void)
{
    static int fd = -2;
    if (fd == -2) {
        const char *dir = getenv("CLO_TIMING_DIR");
        fd = -1;
        if (dir && dir[0]) {
            char path[4096];
            int n = snprintf(path, sizeof(path), "%s/%ld.jsonl", dir, (long)getpid());
            if (n <= 0 || n >= (int)sizeof(path)) abort();
            fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
            if (fd < 0) { perror("closed-loop timing"); abort(); }
            atexit(clo_exit_record);
        }
    }
    return fd;
}

static inline int64_t clo_now(void)
{
    struct timespec ts;
    if (clo_fd() < 0) return 0;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static inline void clo_emit(const char *actor, const char *phase, int64_t start,
                            int64_t end, int64_t sim_ps, int64_t value)
{
    char line[1024];
    struct rusage r;
    int fd = clo_fd();
    if (fd < 0) return;
    getrusage(RUSAGE_SELF, &r);
    int n = snprintf(line, sizeof(line),
        "{\"actor\":\"%s\",\"phase\":\"%s\",\"pid\":%ld,"
        "\"start_ns\":%lld,\"end_ns\":%lld,\"sim_ps\":%lld,\"value\":%lld,"
        "\"user_us\":%lld,\"system_us\":%lld,\"maxrss_kb\":%ld}\n",
        actor, phase, (long)getpid(), (long long)start, (long long)end,
        (long long)sim_ps, (long long)value,
        (long long)r.ru_utime.tv_sec * 1000000 + r.ru_utime.tv_usec,
        (long long)r.ru_stime.tv_sec * 1000000 + r.ru_stime.tv_usec, r.ru_maxrss);
    if (n <= 0 || n >= (int)sizeof(line) || write(fd, line, n) != n) abort();
}
static void clo_exit_record(void)
{
    int64_t t = clo_now();
    clo_emit("native", "process_exit", t, t, 0, 0);
}
#endif
