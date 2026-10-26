/* Small synchronous shim for the existing GVSoC executable-hook interface. */
#define _POSIX_C_SOURCE 200809L
#define _XOPEN_SOURCE 700
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include "../../providers/softhier/native/closed_loop_timing.h"

static double seconds(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}

static void fail(const char *message) {
    fprintf(stderr, "power hook client: %s\n", message);
    exit(1);
}

static char *canonical_path(const char *path) {
    char *resolved = realpath(path, NULL);
    if (resolved) return resolved;
    /* GVSoC removes the old response before invocation; resolve its parent. */
    char *copy = strdup(path);
    if (!copy) fail("cannot allocate hook path");
    char *slash = strrchr(copy, '/');
    const char *name = slash ? slash + 1 : copy;
    if (!*name) fail("hook path must name a file");
    char *parent;
    if (slash) {
        *slash = '\0';
        parent = realpath(slash == copy ? "/" : copy, NULL);
    } else parent = realpath(".", NULL);
    if (!parent) fail("hook path parent does not exist");
    size_t size = strlen(parent) + strlen(name) + 2;
    resolved = malloc(size);
    if (!resolved) fail("cannot allocate hook path");
    snprintf(resolved, size, "%s%s%s", parent, strcmp(parent, "/") ? "/" : "", name);
    free(parent);
    free(copy);
    return resolved;
}

static void ready(int fd, short events, double deadline) {
    for (;;) {
        double remaining = deadline - seconds();
        if (remaining <= 0) fail("worker deadline exceeded; exchange was not retried");
        struct pollfd p = {.fd = fd, .events = events};
        int millis = remaining > 1 ? 1000 : (int)ceil(remaining * 1000);
        int result = poll(&p, 1, millis);
        if (result > 0) return;
        if (result < 0 && errno != EINTR) fail("poll failed");
    }
}

static void transfer(int fd, void *buffer, size_t size, int writing, double deadline) {
    char *p = buffer;
    while (size) {
        ready(fd, writing ? POLLOUT : POLLIN, deadline);
        ssize_t count = writing ? send(fd, p, size, 0) : recv(fd, p, size, 0);
        if (count < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) continue;
        if (count <= 0) fail("worker disconnected; exchange was not retried");
        p += count;
        size -= (size_t)count;
    }
}

int main(int argc, char **argv) {
    const char *names[] = {"--phase", "--config", "--request", "--response"};
    const char *values[4] = {0};
    for (int i = 1; i < argc; i += 2) {
        int found = 0;
        if (i + 1 == argc) fail("missing argument value");
        for (int j = 0; j < 4; j++) if (!strcmp(argv[i], names[j])) {
            if (values[j]) fail("duplicate argument");
            values[j] = argv[i + 1]; found = 1;
        }
        if (!found) fail("unknown argument");
    }
    size_t size = 0;
    for (int i = 0; i < 4; i++) {
        if (!values[i]) fail("missing required hook argument");
        if (i) values[i] = canonical_path(values[i]);
        size += strlen(values[i]) + 1;
    }
    if (size > 65536) fail("hook request too large");
    const char *path = getenv("POWER_HOOK_SOCKET");
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (!path || strlen(path) >= sizeof(address.sun_path)) fail("invalid POWER_HOOK_SOCKET");
    strcpy(address.sun_path, path);
    const char *timeout = getenv("POWER_HOOK_TIMEOUT_SECONDS");
    char *end = NULL;
    double limit = timeout ? strtod(timeout, &end) : 180.;
    if (!isfinite(limit) || limit <= 0 || (timeout && (end == timeout || *end))) fail("invalid timeout");
    double deadline = seconds() + limit + 2.;
    signal(SIGPIPE, SIG_IGN);
    int64_t started = clo_now();
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0 || fcntl(fd, F_SETFL, O_NONBLOCK) < 0) fail("cannot create worker connection");
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) < 0) {
        if (errno != EINPROGRESS) fail("cannot connect to worker");
        ready(fd, POLLOUT, deadline);
        int error = 0; socklen_t length = sizeof(error);
        if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &length) || error) fail("worker connection failed");
    }
    uint32_t length = htonl((uint32_t)size);
    transfer(fd, &length, sizeof(length), 1, deadline);
    for (int i = 0; i < 4; i++) transfer(fd, (void *)values[i], strlen(values[i]) + 1, 1, deadline);
    uint32_t reply[2];
    transfer(fd, reply, sizeof(reply), 0, deadline);
    size = ntohl(reply[1]);
    if (size > 4096) fail("invalid worker response");
    char message[4097] = {0};
    transfer(fd, message, size, 0, deadline);
    close(fd);
    for (int i = 1; i < 4; i++) free((void *)values[i]);
    clo_emit("hook_client", "worker_roundtrip", started, clo_now(), 0, 0);
    if (ntohl(reply[0])) fail(message[0] ? message : "worker failed");
    return 0;
}
