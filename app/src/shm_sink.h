#ifndef SC_SHM_SINK_H
#define SC_SHM_SINK_H

#include "common.h"
#include <stdint.h>
#include <windows.h>
#include "trait/frame_sink.h"

#define SC_SHM_CAPACITY (64u * 1024u * 1024u)
#define SC_SHM_HEADER_SIZE 128u
#define SC_SHM_MAGIC "SCRCSHM1"

/* Protocol v1, little endian. Offsets are checked in shm_sink.c.
 * All fields/pixels require the named mutex, except status (Interlocked).
 * Plane offsets are relative to the start of the payload, after this header.
 */
struct sc_shm_header {
    char magic[8];                    // 0
    uint32_t version, header_size;     // 8, 12
    uint32_t capacity, producer_pid;   // 16, 20
    uint32_t status, format;           // 24, 28: 0 init, 1 live, 2 closed; 1 I420
    uint64_t sequence;                 // 32
    uint32_t width, height;            // 40, 44
    uint32_t offsets[3], lengths[3];   // 48, 60
    uint32_t color_range, color_space; // 72, 76: FFmpeg enum values
    int64_t pts, qpc, qpc_frequency;   // 80, 88, 96
    uint64_t skipped;                  // 104
    uint8_t reserved[16];              // 112
};

struct sc_shm_sink {
    struct sc_frame_sink frame_sink;
    HANDLE mapping, mutex, event;
    struct sc_shm_header *header;
    uint64_t skipped;
};

bool sc_shm_sink_init(struct sc_shm_sink *sink, const char *name);
void sc_shm_sink_destroy(struct sc_shm_sink *sink);

#endif
