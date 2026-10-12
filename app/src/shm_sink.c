#include "shm_sink.h"

#include <assert.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <libavutil/pixdesc.h>
#include "util/log.h"

_Static_assert(sizeof(struct sc_shm_header) == SC_SHM_HEADER_SIZE, "SHM size");
#define CHECK_OFFSET(FIELD, OFFSET) \
    _Static_assert(offsetof(struct sc_shm_header, FIELD) == OFFSET, "SHM offset")
CHECK_OFFSET(version, 8);
CHECK_OFFSET(header_size, 12);
CHECK_OFFSET(capacity, 16);
CHECK_OFFSET(producer_pid, 20);
CHECK_OFFSET(status, 24);
CHECK_OFFSET(format, 28);
CHECK_OFFSET(sequence, 32);
CHECK_OFFSET(width, 40);
CHECK_OFFSET(height, 44);
CHECK_OFFSET(offsets, 48);
CHECK_OFFSET(lengths, 60);
CHECK_OFFSET(color_range, 72);
CHECK_OFFSET(color_space, 76);
CHECK_OFFSET(pts, 80);
CHECK_OFFSET(qpc, 88);
CHECK_OFFSET(qpc_frequency, 96);
CHECK_OFFSET(skipped, 104);
CHECK_OFFSET(reserved, 112);

#define DOWNCAST(SINK) container_of(SINK, struct sc_shm_sink, frame_sink)

static void
sc_shm_set_status(struct sc_shm_sink *sink, LONG status) {
    InterlockedExchange((volatile LONG *) &sink->header->status, status);
    SetEvent(sink->event);
}

static bool
sc_shm_open(struct sc_frame_sink *fs, const AVCodecContext *ctx,
            const struct sc_stream_session *session) {
    (void) session;
    if (ctx->pix_fmt != AV_PIX_FMT_YUV420P
            && ctx->pix_fmt != AV_PIX_FMT_YUVJ420P) {
        LOGE("Shared memory requires decoded YUV420P or YUVJ420P frames "
             "(codec format %d)", ctx->pix_fmt);
        return false;
    }
    sc_shm_set_status(DOWNCAST(fs), 1);
    return true;
}

static void
sc_shm_close(struct sc_frame_sink *fs) {
    sc_shm_set_status(DOWNCAST(fs), 2);
}

static bool
sc_shm_push(struct sc_frame_sink *fs, const AVFrame *frame) {
    struct sc_shm_sink *sink = DOWNCAST(fs);
    if ((frame->format != AV_PIX_FMT_YUV420P
            && frame->format != AV_PIX_FMT_YUVJ420P)
            || frame->width <= 0 || frame->height <= 0) {
        const char *format = av_get_pix_fmt_name(frame->format);
        LOGE("Shared memory requires valid planar 8-bit YUV420 frames "
             "(received %s [%d], %dx%d)", format ? format : "unknown",
             frame->format, frame->width, frame->height);
        return false;
    }
    uint32_t w = frame->width;
    uint32_t h = frame->height;
    uint32_t cw = (w + 1) / 2, ch = (h + 1) / 2;
    uint64_t y_size = (uint64_t) w * h;
    uint64_t c_size = (uint64_t) cw * ch;
    if (y_size + 2 * c_size > SC_SHM_CAPACITY) {
        LOGE("Shared-memory frame exceeds 64 MiB; reduce the video size");
        return false;
    }
    for (unsigned p = 0; p < 3; ++p) {
        int64_t stride = frame->linesize[p];
        if (!frame->data[p] || (stride < 0 ? -stride : stride) < (p ? cw : w)) {
            LOGE("Invalid shared-memory source plane");
            return false;
        }
    }
    DWORD result = WaitForSingleObject(sink->mutex, 0);
    if (result == WAIT_TIMEOUT) {
        ++sink->skipped;
        return true; // Decoder never waits for a slow reader.
    }
    if (result != WAIT_OBJECT_0) {
        if (result == WAIT_ABANDONED) {
            ReleaseMutex(sink->mutex);
        }
        LOGE("Shared-memory mutex failed or was abandoned");
        sc_shm_set_status(sink, 2);
        return false;
    }
    struct sc_shm_header *header = sink->header;
    uint8_t *payload = (uint8_t *) header + SC_SHM_HEADER_SIZE;
    uint32_t offset = 0;
    for (unsigned p = 0; p < 3; ++p) {
        uint32_t pw = p ? cw : w, ph = p ? ch : h;
        header->offsets[p] = offset;
        header->lengths[p] = pw * ph;
        for (uint32_t row = 0; row < ph; ++row) {
            memcpy(payload + offset + (size_t) row * pw,
                   frame->data[p] + (ptrdiff_t) row * frame->linesize[p], pw);
        }
        offset += pw * ph;
    }
    header->width = w;
    header->height = h;
    header->color_range = frame->format == AV_PIX_FMT_YUVJ420P
                       ? AVCOL_RANGE_JPEG : frame->color_range;
    header->color_space = frame->colorspace;
    header->pts = frame->pts;
    header->skipped = sink->skipped;
    ++header->sequence;
    LARGE_INTEGER counter;
    QueryPerformanceCounter(&counter);
    header->qpc = counter.QuadPart;
    ReleaseMutex(sink->mutex);
    if (!SetEvent(sink->event)) {
        LOGE("Could not signal shared-memory frame");
        return false;
    }
    return true;
}

void
sc_shm_sink_destroy(struct sc_shm_sink *sink) {
    if (sink->header) {
        sc_shm_set_status(sink, 2);
        UnmapViewOfFile(sink->header);
    }
    if (sink->mapping) CloseHandle(sink->mapping);
    if (sink->event) CloseHandle(sink->event);
    if (sink->mutex) CloseHandle(sink->mutex);
    sink->header = NULL;
    sink->mapping = sink->event = sink->mutex = NULL;
}

bool
sc_shm_sink_init(struct sc_shm_sink *sink, const char *name) {
    memset(sink, 0, sizeof(*sink));
    static const struct sc_frame_sink_ops ops = {
        .open = sc_shm_open,
        .close = sc_shm_close,
        .push = sc_shm_push,
    };
    sink->frame_sink.ops = &ops;
    if (!*name || strlen(name) > 64
            || strspn(name, "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                      "abcdefghijklmnopqrstuvwxyz0123456789_.-") != strlen(name)) {
        LOGE("Invalid shared-memory name");
        return false;
    }
    char object[128];
    bool owns_mutex = false;
    snprintf(object, sizeof(object), "Local\\scrcpy.shm.%s.mutex", name);
    sink->mutex = CreateMutexA(NULL, TRUE, object);
    if (!sink->mutex || GetLastError() == ERROR_ALREADY_EXISTS) goto error;
    owns_mutex = true;
    snprintf(object, sizeof(object), "Local\\scrcpy.shm.%s.event", name);
    sink->event = CreateEventA(NULL, FALSE, FALSE, object);
    if (!sink->event || GetLastError() == ERROR_ALREADY_EXISTS) goto error;
    snprintf(object, sizeof(object), "Local\\scrcpy.shm.%s.frame", name);
    sink->mapping = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE,
                                     0, SC_SHM_HEADER_SIZE + SC_SHM_CAPACITY,
                                     object);
    if (!sink->mapping || GetLastError() == ERROR_ALREADY_EXISTS) goto error;
    sink->header = MapViewOfFile(sink->mapping, FILE_MAP_WRITE, 0, 0, 0);
    if (!sink->header) goto error;
    // Own the mutex from creation until the complete header is initialized.
    memset(sink->header, 0, SC_SHM_HEADER_SIZE);
    memcpy(sink->header->magic, SC_SHM_MAGIC, 8);
    sink->header->version = 1;
    sink->header->header_size = SC_SHM_HEADER_SIZE;
    sink->header->capacity = SC_SHM_CAPACITY;
    sink->header->producer_pid = GetCurrentProcessId();
    sink->header->format = 1;
    LARGE_INTEGER frequency;
    QueryPerformanceFrequency(&frequency);
    sink->header->qpc_frequency = frequency.QuadPart;
    ReleaseMutex(sink->mutex);
    LOGI("Shared-memory output: %s (YUV420P, newest frame only)", name);
    return true;

error:
    LOGE("Could not create shared-memory output '%s' (Windows error %lu); "
         "close any previous producer/reader using this name", name,
         (unsigned long) GetLastError());
    if (owns_mutex) ReleaseMutex(sink->mutex);
    sc_shm_sink_destroy(sink);
    return false;
}
