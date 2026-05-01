#include "catap_coreaudio.h"

#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef struct catap_audio_slot {
    uint8_t *data;
    catap_audio_chunk_info_t info;
} catap_audio_slot_t;

struct catap_audio_ring {
    uint32_t slot_count;
    uint32_t physical_slot_count;
    uint32_t slot_capacity;
    catap_audio_slot_t *slots;
    uint8_t *storage;
    atomic_uint write_index;
    atomic_uint read_index;
    atomic_ullong dropped_chunks;
    atomic_ullong dropped_frames;
    atomic_ullong oversized_chunks;
};

struct catap_recorder {
    catap_audio_ring_t *ring;
    uint32_t expected_channel_count;
    uint32_t bytes_per_frame;
    atomic_ullong captured_chunks;
    atomic_ullong captured_frames;
    atomic_ullong callback_failures;
    atomic_int last_error_status;
};

static uint32_t catap_next_index(const catap_audio_ring_t *ring, uint32_t index) {
    ++index;
    if (index == ring->physical_slot_count) {
        return 0;
    }
    return index;
}

static uint32_t catap_audio_ring_queued_count(const catap_audio_ring_t *ring) {
    const uint32_t write_index =
        atomic_load_explicit(&ring->write_index, memory_order_acquire);
    const uint32_t read_index =
        atomic_load_explicit(&ring->read_index, memory_order_acquire);

    if (write_index >= read_index) {
        return write_index - read_index;
    }
    return ring->physical_slot_count - read_index + write_index;
}

static void catap_record_drop(catap_audio_ring_t *ring, uint32_t frame_count) {
    atomic_fetch_add_explicit(&ring->dropped_chunks, 1u, memory_order_relaxed);
    atomic_fetch_add_explicit(
        &ring->dropped_frames, (unsigned long long)frame_count, memory_order_relaxed
    );
}

static void catap_recorder_record_failure(
    catap_recorder_t *recorder,
    int32_t status
) {
    atomic_fetch_add_explicit(
        &recorder->callback_failures, 1u, memory_order_relaxed
    );
    atomic_store_explicit(
        &recorder->last_error_status, status, memory_order_relaxed
    );
}

uint32_t catap_abi_version(void) {
    return CATAP_ABI_VERSION;
}

const char *catap_status_name(int32_t status) {
    switch (status) {
    case CATAP_STATUS_OK:
        return "OK";
    case CATAP_STATUS_RING_FULL:
        return "RING_FULL";
    case CATAP_STATUS_RING_EMPTY:
        return "RING_EMPTY";
    case CATAP_STATUS_BUFFER_TOO_SMALL:
        return "BUFFER_TOO_SMALL";
    case CATAP_STATUS_BUFFER_TOO_LARGE:
        return "BUFFER_TOO_LARGE";
    case CATAP_STATUS_INVALID_ARGUMENT:
        return "INVALID_ARGUMENT";
    case CATAP_STATUS_OUT_OF_MEMORY:
        return "OUT_OF_MEMORY";
    case CATAP_STATUS_UNSUPPORTED_AUDIO_LAYOUT:
        return "UNSUPPORTED_AUDIO_LAYOUT";
    case CATAP_STATUS_INVALID_AUDIO_BUFFER:
        return "INVALID_AUDIO_BUFFER";
    default:
        return "UNKNOWN";
    }
}

int32_t catap_audio_ring_create(
    uint32_t slot_count,
    uint32_t slot_capacity,
    catap_audio_ring_t **out_ring
) {
    if (slot_count == 0 || slot_capacity == 0 || out_ring == NULL) {
        return CATAP_STATUS_INVALID_ARGUMENT;
    }
    if (slot_count == UINT32_MAX) {
        return CATAP_STATUS_INVALID_ARGUMENT;
    }

    const uint32_t physical_slot_count = slot_count + 1u;
    if ((size_t)physical_slot_count > SIZE_MAX / (size_t)slot_capacity) {
        return CATAP_STATUS_OUT_OF_MEMORY;
    }

    catap_audio_ring_t *ring = calloc(1, sizeof(*ring));
    if (ring == NULL) {
        return CATAP_STATUS_OUT_OF_MEMORY;
    }

    ring->slots = calloc(physical_slot_count, sizeof(*ring->slots));
    if (ring->slots == NULL) {
        free(ring);
        return CATAP_STATUS_OUT_OF_MEMORY;
    }

    ring->storage = calloc((size_t)physical_slot_count, (size_t)slot_capacity);
    if (ring->storage == NULL) {
        free(ring->slots);
        free(ring);
        return CATAP_STATUS_OUT_OF_MEMORY;
    }

    ring->slot_count = slot_count;
    ring->physical_slot_count = physical_slot_count;
    ring->slot_capacity = slot_capacity;
    for (uint32_t index = 0; index < physical_slot_count; ++index) {
        ring->slots[index].data =
            ring->storage + ((size_t)index * (size_t)slot_capacity);
    }

    atomic_init(&ring->write_index, 0u);
    atomic_init(&ring->read_index, 0u);
    atomic_init(&ring->dropped_chunks, 0u);
    atomic_init(&ring->dropped_frames, 0u);
    atomic_init(&ring->oversized_chunks, 0u);

    *out_ring = ring;
    return CATAP_STATUS_OK;
}

void catap_audio_ring_destroy(catap_audio_ring_t *ring) {
    if (ring == NULL) {
        return;
    }
    free(ring->storage);
    free(ring->slots);
    free(ring);
}

void catap_audio_ring_reset(catap_audio_ring_t *ring) {
    if (ring == NULL) {
        return;
    }
    atomic_store_explicit(&ring->write_index, 0u, memory_order_release);
    atomic_store_explicit(&ring->read_index, 0u, memory_order_release);
    atomic_store_explicit(&ring->dropped_chunks, 0u, memory_order_relaxed);
    atomic_store_explicit(&ring->dropped_frames, 0u, memory_order_relaxed);
    atomic_store_explicit(&ring->oversized_chunks, 0u, memory_order_relaxed);
}

int32_t catap_audio_ring_try_write(
    catap_audio_ring_t *ring,
    const void *data,
    uint32_t byte_count,
    uint32_t frame_count,
    double input_sample_time,
    uint32_t flags
) {
    if (ring == NULL || (byte_count > 0 && data == NULL)) {
        return CATAP_STATUS_INVALID_ARGUMENT;
    }
    if (byte_count > ring->slot_capacity) {
        catap_record_drop(ring, frame_count);
        atomic_fetch_add_explicit(&ring->oversized_chunks, 1u, memory_order_relaxed);
        return CATAP_STATUS_BUFFER_TOO_LARGE;
    }

    const uint32_t write_index =
        atomic_load_explicit(&ring->write_index, memory_order_relaxed);
    const uint32_t next_write_index = catap_next_index(ring, write_index);
    const uint32_t read_index =
        atomic_load_explicit(&ring->read_index, memory_order_acquire);

    if (next_write_index == read_index) {
        catap_record_drop(ring, frame_count);
        return CATAP_STATUS_RING_FULL;
    }

    catap_audio_slot_t *slot = &ring->slots[write_index];
    if (byte_count > 0) {
        memcpy(slot->data, data, byte_count);
    }
    slot->info.byte_count = byte_count;
    slot->info.frame_count = frame_count;
    slot->info.flags = flags & CATAP_CHUNK_HAS_INPUT_SAMPLE_TIME;
    slot->info.input_sample_time =
        (slot->info.flags & CATAP_CHUNK_HAS_INPUT_SAMPLE_TIME)
            ? input_sample_time
            : 0.0;

    atomic_store_explicit(&ring->write_index, next_write_index, memory_order_release);
    return CATAP_STATUS_OK;
}

int32_t catap_audio_ring_try_read(
    catap_audio_ring_t *ring,
    void *destination,
    uint32_t destination_capacity,
    catap_audio_chunk_info_t *out_info
) {
    if (ring == NULL || out_info == NULL) {
        return CATAP_STATUS_INVALID_ARGUMENT;
    }

    const uint32_t read_index =
        atomic_load_explicit(&ring->read_index, memory_order_relaxed);
    const uint32_t write_index =
        atomic_load_explicit(&ring->write_index, memory_order_acquire);

    if (read_index == write_index) {
        return CATAP_STATUS_RING_EMPTY;
    }

    catap_audio_slot_t *slot = &ring->slots[read_index];
    if (slot->info.byte_count > destination_capacity) {
        return CATAP_STATUS_BUFFER_TOO_SMALL;
    }
    if (slot->info.byte_count > 0 && destination == NULL) {
        return CATAP_STATUS_INVALID_ARGUMENT;
    }

    if (slot->info.byte_count > 0) {
        memcpy(destination, slot->data, slot->info.byte_count);
    }
    *out_info = slot->info;

    atomic_store_explicit(
        &ring->read_index, catap_next_index(ring, read_index), memory_order_release
    );
    return CATAP_STATUS_OK;
}

int32_t catap_audio_ring_stats(
    catap_audio_ring_t *ring,
    catap_audio_ring_stats_t *out_stats
) {
    if (ring == NULL || out_stats == NULL) {
        return CATAP_STATUS_INVALID_ARGUMENT;
    }

    out_stats->slot_count = ring->slot_count;
    out_stats->slot_capacity = ring->slot_capacity;
    out_stats->queued_chunks = catap_audio_ring_queued_count(ring);
    out_stats->dropped_chunks =
        atomic_load_explicit(&ring->dropped_chunks, memory_order_relaxed);
    out_stats->dropped_frames =
        atomic_load_explicit(&ring->dropped_frames, memory_order_relaxed);
    out_stats->oversized_chunks =
        atomic_load_explicit(&ring->oversized_chunks, memory_order_relaxed);
    return CATAP_STATUS_OK;
}

int32_t catap_recorder_create(
    const catap_recorder_config_t *config,
    catap_recorder_t **out_recorder
) {
    if (
        config == NULL ||
        out_recorder == NULL ||
        config->slot_count == 0 ||
        config->slot_capacity == 0 ||
        config->expected_channel_count == 0 ||
        config->bytes_per_frame == 0
    ) {
        return CATAP_STATUS_INVALID_ARGUMENT;
    }

    catap_recorder_t *recorder = calloc(1, sizeof(*recorder));
    if (recorder == NULL) {
        return CATAP_STATUS_OUT_OF_MEMORY;
    }

    const int32_t status = catap_audio_ring_create(
        config->slot_count, config->slot_capacity, &recorder->ring
    );
    if (status != CATAP_STATUS_OK) {
        free(recorder);
        return status;
    }

    recorder->expected_channel_count = config->expected_channel_count;
    recorder->bytes_per_frame = config->bytes_per_frame;
    atomic_init(&recorder->captured_chunks, 0u);
    atomic_init(&recorder->captured_frames, 0u);
    atomic_init(&recorder->callback_failures, 0u);
    atomic_init(&recorder->last_error_status, CATAP_STATUS_OK);

    *out_recorder = recorder;
    return CATAP_STATUS_OK;
}

void catap_recorder_destroy(catap_recorder_t *recorder) {
    if (recorder == NULL) {
        return;
    }
    catap_audio_ring_destroy(recorder->ring);
    free(recorder);
}

void catap_recorder_reset(catap_recorder_t *recorder) {
    if (recorder == NULL) {
        return;
    }
    catap_audio_ring_reset(recorder->ring);
    atomic_store_explicit(&recorder->captured_chunks, 0u, memory_order_relaxed);
    atomic_store_explicit(&recorder->captured_frames, 0u, memory_order_relaxed);
    atomic_store_explicit(&recorder->callback_failures, 0u, memory_order_relaxed);
    atomic_store_explicit(
        &recorder->last_error_status, CATAP_STATUS_OK, memory_order_relaxed
    );
}

int32_t catap_recorder_read(
    catap_recorder_t *recorder,
    void *destination,
    uint32_t destination_capacity,
    catap_audio_chunk_info_t *out_info
) {
    if (recorder == NULL) {
        return CATAP_STATUS_INVALID_ARGUMENT;
    }
    return catap_audio_ring_try_read(
        recorder->ring, destination, destination_capacity, out_info
    );
}

int32_t catap_recorder_stats(
    catap_recorder_t *recorder,
    catap_recorder_stats_t *out_stats
) {
    if (recorder == NULL || out_stats == NULL) {
        return CATAP_STATUS_INVALID_ARGUMENT;
    }

    out_stats->captured_chunks =
        atomic_load_explicit(&recorder->captured_chunks, memory_order_relaxed);
    out_stats->captured_frames =
        atomic_load_explicit(&recorder->captured_frames, memory_order_relaxed);
    out_stats->callback_failures =
        atomic_load_explicit(&recorder->callback_failures, memory_order_relaxed);
    out_stats->last_error_status =
        atomic_load_explicit(&recorder->last_error_status, memory_order_relaxed);
    return catap_audio_ring_stats(recorder->ring, &out_stats->ring);
}

OSStatus catap_recorder_io_proc(
    AudioObjectID device,
    const AudioTimeStamp *now,
    const AudioBufferList *input_data,
    const AudioTimeStamp *input_time,
    AudioBufferList *output_data,
    const AudioTimeStamp *output_time,
    void *client_data
) {
    (void)device;
    (void)now;
    (void)output_data;
    (void)output_time;

    catap_recorder_t *recorder = client_data;
    if (recorder == NULL || input_data == NULL) {
        return noErr;
    }

    if (input_data->mNumberBuffers == 0) {
        return noErr;
    }
    if (input_data->mNumberBuffers != 1) {
        catap_recorder_record_failure(
            recorder, CATAP_STATUS_UNSUPPORTED_AUDIO_LAYOUT
        );
        return noErr;
    }

    const AudioBuffer *buffer = &input_data->mBuffers[0];
    const uint32_t byte_count = buffer->mDataByteSize;
    if (byte_count == 0) {
        return noErr;
    }
    if (buffer->mData == NULL) {
        catap_recorder_record_failure(recorder, CATAP_STATUS_INVALID_AUDIO_BUFFER);
        return noErr;
    }
    if (buffer->mNumberChannels != recorder->expected_channel_count) {
        catap_recorder_record_failure(
            recorder, CATAP_STATUS_UNSUPPORTED_AUDIO_LAYOUT
        );
        return noErr;
    }
    if (byte_count % recorder->bytes_per_frame != 0) {
        catap_recorder_record_failure(recorder, CATAP_STATUS_INVALID_AUDIO_BUFFER);
        return noErr;
    }

    const uint32_t frame_count = byte_count / recorder->bytes_per_frame;
    if (frame_count == 0) {
        return noErr;
    }

    const uint32_t has_sample_time =
        input_time != NULL &&
        (input_time->mFlags & kAudioTimeStampSampleTimeValid) != 0;
    const double input_sample_time =
        has_sample_time ? input_time->mSampleTime : 0.0;
    const int32_t status = catap_audio_ring_try_write(
        recorder->ring,
        buffer->mData,
        byte_count,
        frame_count,
        input_sample_time,
        has_sample_time ? CATAP_CHUNK_HAS_INPUT_SAMPLE_TIME : 0u
    );

    if (status == CATAP_STATUS_OK) {
        atomic_fetch_add_explicit(
            &recorder->captured_chunks, 1u, memory_order_relaxed
        );
        atomic_fetch_add_explicit(
            &recorder->captured_frames,
            (unsigned long long)frame_count,
            memory_order_relaxed
        );
    }
    return noErr;
}
