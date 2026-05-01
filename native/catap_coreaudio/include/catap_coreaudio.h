#ifndef CATAP_COREAUDIO_H
#define CATAP_COREAUDIO_H

#include <stdint.h>

#include <CoreAudio/CoreAudio.h>

#ifdef __cplusplus
extern "C" {
#endif

#if defined(__GNUC__)
#define CATAP_EXPORT __attribute__((visibility("default")))
#else
#define CATAP_EXPORT
#endif

#define CATAP_ABI_VERSION 1u
#define CATAP_CHUNK_HAS_INPUT_SAMPLE_TIME 1u

typedef struct catap_audio_ring catap_audio_ring_t;
typedef struct catap_recorder catap_recorder_t;

typedef enum catap_status {
    CATAP_STATUS_OK = 0,
    CATAP_STATUS_RING_FULL = 1,
    CATAP_STATUS_RING_EMPTY = 2,
    CATAP_STATUS_BUFFER_TOO_SMALL = 3,
    CATAP_STATUS_BUFFER_TOO_LARGE = 4,
    CATAP_STATUS_INVALID_ARGUMENT = -1,
    CATAP_STATUS_OUT_OF_MEMORY = -2,
    CATAP_STATUS_UNSUPPORTED_AUDIO_LAYOUT = -3,
    CATAP_STATUS_INVALID_AUDIO_BUFFER = -4,
} catap_status_t;

typedef struct catap_audio_chunk_info {
    uint32_t byte_count;
    uint32_t frame_count;
    uint32_t flags;
    double input_sample_time;
} catap_audio_chunk_info_t;

typedef struct catap_audio_ring_stats {
    uint32_t slot_count;
    uint32_t slot_capacity;
    uint32_t queued_chunks;
    uint64_t dropped_chunks;
    uint64_t dropped_frames;
    uint64_t oversized_chunks;
} catap_audio_ring_stats_t;

typedef struct catap_recorder_config {
    uint32_t slot_count;
    uint32_t slot_capacity;
    uint32_t expected_channel_count;
    uint32_t bytes_per_frame;
} catap_recorder_config_t;

typedef struct catap_recorder_stats {
    uint64_t captured_chunks;
    uint64_t captured_frames;
    uint64_t callback_failures;
    int32_t last_error_status;
    catap_audio_ring_stats_t ring;
} catap_recorder_stats_t;

CATAP_EXPORT uint32_t catap_abi_version(void);
CATAP_EXPORT const char *catap_status_name(int32_t status);

CATAP_EXPORT int32_t catap_audio_ring_create(
    uint32_t slot_count,
    uint32_t slot_capacity,
    catap_audio_ring_t **out_ring
);
CATAP_EXPORT void catap_audio_ring_destroy(catap_audio_ring_t *ring);
CATAP_EXPORT void catap_audio_ring_reset(catap_audio_ring_t *ring);

CATAP_EXPORT int32_t catap_audio_ring_try_write(
    catap_audio_ring_t *ring,
    const void *data,
    uint32_t byte_count,
    uint32_t frame_count,
    double input_sample_time,
    uint32_t flags
);
CATAP_EXPORT int32_t catap_audio_ring_try_read(
    catap_audio_ring_t *ring,
    void *destination,
    uint32_t destination_capacity,
    catap_audio_chunk_info_t *out_info
);
CATAP_EXPORT int32_t catap_audio_ring_stats(
    catap_audio_ring_t *ring,
    catap_audio_ring_stats_t *out_stats
);

CATAP_EXPORT int32_t catap_recorder_create(
    const catap_recorder_config_t *config,
    catap_recorder_t **out_recorder
);
CATAP_EXPORT void catap_recorder_destroy(catap_recorder_t *recorder);
CATAP_EXPORT void catap_recorder_reset(catap_recorder_t *recorder);
CATAP_EXPORT int32_t catap_recorder_read(
    catap_recorder_t *recorder,
    void *destination,
    uint32_t destination_capacity,
    catap_audio_chunk_info_t *out_info
);
CATAP_EXPORT int32_t catap_recorder_stats(
    catap_recorder_t *recorder,
    catap_recorder_stats_t *out_stats
);
CATAP_EXPORT OSStatus catap_recorder_io_proc(
    AudioObjectID device,
    const AudioTimeStamp *now,
    const AudioBufferList *input_data,
    const AudioTimeStamp *input_time,
    AudioBufferList *output_data,
    const AudioTimeStamp *output_time,
    void *client_data
);

#ifdef __cplusplus
}
#endif

#endif
