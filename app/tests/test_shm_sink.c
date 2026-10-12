#include "common.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "shm_sink.h"

static bool
publish_with_format(struct sc_shm_sink *sink, int w, int h, int value,
                    bool negative, enum AVPixelFormat format) {
    AVFrame frame = {0};
    frame.width = w;
    frame.height = h;
    frame.format = format;
    frame.pts = value * 1000;
    frame.color_range = AVCOL_RANGE_MPEG;
    frame.colorspace = AVCOL_SPC_BT709;
    uint8_t *buffers[3];
    for (unsigned p = 0; p < 3; ++p) {
        int pw = p ? (w + 1) / 2 : w;
        int ph = p ? (h + 1) / 2 : h;
        int stride = pw + 7;
        buffers[p] = malloc((size_t) stride * ph);
        assert(buffers[p]);
        memset(buffers[p], 0xee, (size_t) stride * ph);
        frame.linesize[p] = negative ? -stride : stride;
        frame.data[p] = buffers[p] + (negative ? (size_t) (ph - 1) * stride : 0);
        for (int row = 0; row < ph; ++row) {
            for (int col = 0; col < pw; ++col) {
                frame.data[p][(ptrdiff_t) row * frame.linesize[p] + col] =
                    (uint8_t) (value + p * 64 + row + col);
            }
        }
    }
    bool ok = sink->frame_sink.ops->push(&sink->frame_sink, &frame);
    for (unsigned p = 0; p < 3; ++p) free(buffers[p]);
    return ok;
}

static bool
publish(struct sc_shm_sink *sink, int w, int h, int value, bool negative) {
    return publish_with_format(sink, w, h, value, negative, AV_PIX_FMT_YUV420P);
}

struct lock_context {
    HANDLE mutex, ready, release;
};

static DWORD WINAPI
hold_mutex(void *userdata) {
    struct lock_context *ctx = userdata;
    assert(WaitForSingleObject(ctx->mutex, 1000) == WAIT_OBJECT_0);
    SetEvent(ctx->ready);
    assert(WaitForSingleObject(ctx->release, 1000) == WAIT_OBJECT_0);
    ReleaseMutex(ctx->mutex);
    return 0;
}

static void
test_sink(void) {
    char name[64];
    snprintf(name, sizeof(name), "test-%lu", (unsigned long) GetCurrentProcessId());
    struct sc_shm_sink sink, duplicate;
    assert(sc_shm_sink_init(&sink, name));
    assert(!sc_shm_sink_init(&duplicate, name));
    AVCodecContext codec = { .pix_fmt = AV_PIX_FMT_YUV420P };
    assert(sink.frame_sink.ops->open(&sink.frame_sink, &codec, NULL));
    assert(publish(&sink, 4, 4, 1, false));
    assert(sink.header->sequence == 1);
    assert(sink.header->lengths[0] == 16);
    assert(sink.header->lengths[1] == 4);
    uint8_t *pixels = (uint8_t *) sink.header + SC_SHM_HEADER_SIZE;
    assert(pixels[0] == 1 && pixels[3] == 4 && pixels[12] == 4);
    assert(pixels[16] == 65 && pixels[20] == 129);
    assert(publish(&sink, 5, 3, 2, true));
    assert(sink.header->sequence == 2);
    assert(sink.header->width == 5 && sink.header->height == 3);
    assert(sink.header->lengths[0] == 15 && sink.header->lengths[1] == 6);
    assert(pixels[0] == 2 && pixels[10] == 4 && pixels[15] == 66);

    struct lock_context lock = {
        .mutex = sink.mutex,
        .ready = CreateEventA(NULL, TRUE, FALSE, NULL),
        .release = CreateEventA(NULL, TRUE, FALSE, NULL),
    };
    HANDLE thread = CreateThread(NULL, 0, hold_mutex, &lock, 0, NULL);
    assert(thread);
    assert(WaitForSingleObject(lock.ready, 1000) == WAIT_OBJECT_0);
    assert(publish(&sink, 4, 4, 3, false)); // Must return while reader holds lock.
    assert(sink.header->sequence == 2 && sink.skipped == 1);
    SetEvent(lock.release);
    assert(WaitForSingleObject(thread, 1000) == WAIT_OBJECT_0);
    CloseHandle(thread);
    CloseHandle(lock.ready);
    CloseHandle(lock.release);
    assert(publish(&sink, 4, 4, 4, false));
    assert(sink.header->skipped == 1);
    codec.pix_fmt = AV_PIX_FMT_YUVJ420P;
    assert(sink.frame_sink.ops->open(&sink.frame_sink, &codec, NULL));
    assert(publish_with_format(&sink, 4, 4, 5, false, AV_PIX_FMT_YUVJ420P));
    assert(sink.header->color_range == AVCOL_RANGE_JPEG);
    assert(pixels[0] == 5 && pixels[16] == 69 && pixels[20] == 133);

    AVFrame invalid = { .width = 100000, .height = 100000,
                        .format = AV_PIX_FMT_YUV420P };
    assert(!sink.frame_sink.ops->push(&sink.frame_sink, &invalid));
    invalid.format = AV_PIX_FMT_RGB24;
    assert(!sink.frame_sink.ops->push(&sink.frame_sink, &invalid));
    invalid.format = AV_PIX_FMT_YUV420P;
    invalid.width = invalid.height = 4; // Missing plane data is rejected.
    assert(!sink.frame_sink.ops->push(&sink.frame_sink, &invalid));
    codec.pix_fmt = AV_PIX_FMT_RGB24;
    assert(!sink.frame_sink.ops->open(&sink.frame_sink, &codec, NULL));
    sink.frame_sink.ops->close(&sink.frame_sink);
    assert(sink.header->status == 2);
    sc_shm_sink_destroy(&sink);
    assert(!sink.header && !sink.mutex);
    assert(sc_shm_sink_init(&sink, name)); // Name can be reused after cleanup.
    sc_shm_sink_destroy(&sink);
    assert(!sc_shm_sink_init(&sink, "bad/name"));

    // Collision after creating the mutex must release partial resources.
    char event_name[128];
    snprintf(event_name, sizeof(event_name), "Local\\scrcpy.shm.%s.event", name);
    HANDLE conflicting_event = CreateEventA(NULL, FALSE, FALSE, event_name);
    assert(conflicting_event);
    assert(!sc_shm_sink_init(&sink, name));
    CloseHandle(conflicting_event);
    assert(sc_shm_sink_init(&sink, name));
    sc_shm_sink_destroy(&sink);
}

static int
serve(const char *name) {
    struct sc_shm_sink sink;
    if (!sc_shm_sink_init(&sink, name)) return 2;
    AVCodecContext codec = { .pix_fmt = AV_PIX_FMT_YUV420P };
    assert(sink.frame_sink.ops->open(&sink.frame_sink, &codec, NULL));
    puts("READY");
    fflush(stdout);
    char line[256];
    while (fgets(line, sizeof(line), stdin)) {
        int w, h, value, negative, count, interval;
        if (sscanf(line, "publishj %d %d %d %d", &w, &h, &value, &negative) == 4) {
            if (!publish_with_format(&sink, w, h, value, negative,
                                     AV_PIX_FMT_YUVJ420P)) break;
            printf("PUBLISHED %llu\n", (unsigned long long) sink.header->sequence);
        } else if (sscanf(line, "publish %d %d %d %d", &w, &h, &value, &negative) == 4) {
            if (!publish(&sink, w, h, value, negative)) break;
            printf("PUBLISHED %llu\n", (unsigned long long) sink.header->sequence);
        } else if (sscanf(line, "stream %d %d %d %d", &w, &h, &count, &interval) == 4) {
            puts("STREAM");
            fflush(stdout);
            // High-resolution timer avoids Windows' coarse Sleep rounding.
            HANDLE timer = CreateWaitableTimerExA(NULL, NULL, 2, TIMER_ALL_ACCESS);
            if (!timer) timer = CreateWaitableTimerA(NULL, FALSE, NULL);
            assert(timer);
            LARGE_INTEGER freq, deadline;
            QueryPerformanceFrequency(&freq);
            QueryPerformanceCounter(&deadline);
            LONGLONG step = freq.QuadPart * interval / 1000;
            for (int i = 0; i < count; ++i) {
                if (!publish(&sink, w, h, i % 64, false)) break;
                deadline.QuadPart += step;
                LARGE_INTEGER now;
                QueryPerformanceCounter(&now);
                if (deadline.QuadPart > now.QuadPart) {
                    LARGE_INTEGER due = {
                        .QuadPart = -(deadline.QuadPart - now.QuadPart)
                                    * 10000000 / freq.QuadPart,
                    };
                    assert(SetWaitableTimer(timer, &due, 0, NULL, NULL, FALSE));
                    assert(WaitForSingleObject(timer, 1000) == WAIT_OBJECT_0);
                }
            }
            CloseHandle(timer);
            puts("DONE");
        } else if (!strncmp(line, "close", 5)) {
            sink.frame_sink.ops->close(&sink.frame_sink);
            puts("CLOSED");
        } else if (!strncmp(line, "quit", 4)) {
            break;
        }
        fflush(stdout);
    }
    sc_shm_sink_destroy(&sink);
    return 0;
}

int
main(int argc, char **argv) {
    if (argc == 3 && !strcmp(argv[1], "--serve")) return serve(argv[2]);
    test_sink();
    return 0;
}
