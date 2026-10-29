// The test inserts the actual engine methods below. Only their surrounding
// time/trace services are stubbed; production queue operations are not copied.
#include <cassert>
#include <cstdint>
#include <climits>
#include <iostream>
#include <random>
#include <vector>
#include <algorithm>
#include <chrono>
#define unlikely(x) (x)
#define likely(x) (x)
#define vp_assert(condition, ...) assert(condition)
namespace vp {
struct Block {};
struct ClockEngine;
struct ClockEvent {
    int id = 0;
    bool enqueued = false;
    int64_t cycle = 0;
    ClockEvent *next = nullptr;
    ClockEngine *clock = nullptr;
    Block *_this = nullptr;
    void (*meth)(Block *, ClockEvent *) = nullptr;
    bool is_enqueued() { return enqueued; }
    void disable() { enqueued = false; }
};
struct Trace { void set(int64_t) {} void event_real(int64_t) {} };
struct Time {
    bool running = true, queued = false;
    int64_t now = 0, wake = -1;
    bool is_running() { return running; }
    int64_t get_time() { return now; }
    void enqueue_to_engine(int64_t when) { queued = true; wake = when; }
};
struct TimeEngine { void *first_client = (void *)1; bool stop_req = true; int64_t time = 0; };
struct ClockEngine {
    ClockEvent *delayed_queue = nullptr, *permanent_first = nullptr;
    ClockEvent *delayed_insert_tail = nullptr;
    int64_t delayed_insert_cycle = 0, cycles = 0, period = 10, stop_time = 0;
    int64_t next_delayed_cycle = INT64_MAX;
    Trace clock_trace, cycles_trace;
    Time time;
    TimeEngine engine, *time_engine = &engine;
    int64_t get_cycles() { return cycles; }
    bool has_events() { return delayed_queue || permanent_first; }
    bool dequeue_from_engine() { time.queued = false; return true; }
    void sync() { if (!time.is_running() && !permanent_first) update(); }
    void update();
    ClockEvent *enqueue(ClockEvent *, int64_t);
    ClockEvent *reenqueue(ClockEvent *, int64_t);
    void cancel(ClockEvent *);
    int64_t exec();
    void reset(bool);
    void event_del(Block *, ClockEvent *);
};
}
// ACTUAL_ENGINE_METHODS

static std::vector<int> fired;
static int reschedules = 0;
static void callback(vp::Block *, vp::ClockEvent *event)
{
    fired.push_back(event->id);
    if (event->id == 99 && reschedules++ < 3) event->clock->enqueue(event, 0);
}
static void drain(vp::ClockEngine &clock)
{
    int limit = 10000;
    while (clock.delayed_queue) {
        assert(--limit > 0);
        clock.time.now = clock.delayed_queue->cycle * clock.period;
        clock.exec();
    }
}
static void snapshot(vp::ClockEngine &clock)
{
    int64_t previous = INT64_MIN;
    int count = 0;
    for (auto *event = clock.delayed_queue; event; event = event->next) {
        assert(++count < 1000 && event->enqueued && event->cycle >= previous);
        previous = event->cycle;
        std::cout << event->id << ':' << event->cycle << ',';
    }
    assert(clock.next_delayed_cycle == (clock.delayed_queue ? clock.delayed_queue->cycle : INT64_MAX));
    std::cout << '|' << clock.cycles << ':' << clock.time.wake << '\n';
}
int main()
{
    vp::ClockEngine clock;
    std::vector<vp::ClockEvent> events(64);
    for (int i = 0; i < 64; ++i) { events[i].id = i; events[i].meth = callback; }
    // Equal deadlines must remain FIFO, including cancellation and reinsertion.
    for (int i = 0; i < 8; ++i) clock.enqueue(&events[i], 1);
    clock.cancel(&events[0]); clock.cancel(&events[4]); clock.cancel(&events[7]);
    clock.enqueue(&events[7], 1); clock.enqueue(&events[4], 1); clock.enqueue(&events[0], 1);
    drain(clock);
    assert((fired == std::vector<int>{1, 2, 3, 5, 6, 7, 4, 0}));
    fired.clear();
    // Reentrant zero-cycle scheduling exercises invalidation before callbacks.
    vp::ClockEvent self; self.id = 99; self.meth = callback;
    clock.enqueue(&self, 1); drain(clock);
    assert((fired == std::vector<int>{99, 99, 99, 99}));
    // Delete a cancelled cached event before inserting another at that deadline.
    auto *removed = new vp::ClockEvent; removed->meth = callback;
    clock.enqueue(removed, 5); clock.cancel(removed); clock.event_del(nullptr, removed);
    clock.enqueue(&events[0], 5); drain(clock);
    // Identical random operation streams against original and patched methods.
    std::mt19937 random(19);
    for (int i = 0; i < 20000; ++i) {
        auto *event = &events[random() % events.size()];
        switch (random() % 8) {
        case 0: case 1: case 2: clock.enqueue(event, random() % 20); break;
        case 3: clock.cancel(event); break;
        case 4: clock.reenqueue(event, random() % 20); break;
        case 5: if (clock.delayed_queue) clock.exec(); break;
        case 6: clock.reset(i & 1); break;
        case 7:
            // Stopped-clock, cross-domain arrivals and changed clock periods.
            clock.time.running = false;
            clock.period = 1 + random() % 20;
            clock.time.now = clock.stop_time + std::min<int64_t>(random() % 50,
                clock.delayed_queue ? (clock.delayed_queue->cycle - clock.cycles) * clock.period : 50);
            clock.enqueue(event, 1 + random() % 20);
            clock.time.running = true;
            break;
        }
        snapshot(clock);
    }
    drain(clock);
    for (int value : fired) std::cout << value << ',';
    std::cout << '\n';
}
