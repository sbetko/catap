# Headless Signal Fixtures

`catap` ships internal devtools for driving integration tests against
deterministic audio sources.

Start a farm of independent tone-producing processes:

```bash
uv run python -m catap._devtools.tone_farm \
  --count 4 \
  --manifest /tmp/catap-tone-farm.json
```

The manifest maps each tone ID to its PID, Core Audio process object ID,
frequency, amplitude, waveform, channel mode, and device metadata. By default
the manager keeps tones running until interrupted; pass `--seconds N` for a
finite run.

Record the full system mix and validate all expected tones:

```bash
uv run catap record --system --duration 5 --output /tmp/mix.wav

uv run python -m catap._devtools.tone_analyzer /tmp/mix.wav \
  --manifest /tmp/catap-tone-farm.json \
  --json
```

macOS often reports these headless worker processes as `Unknown`, so
name-based CLI targeting is ambiguous. Use manifest identifiers instead:

```bash
catap record \
  --audio-id "$(jq '.tones[0].audio_object_id' /tmp/catap-tone-farm.json)" \
  --duration 5 \
  --output /tmp/tone-001.wav

catap record --system \
  --exclude-audio-id "$(jq '.tones[0].audio_object_id' /tmp/catap-tone-farm.json)" \
  --duration 5 \
  --output /tmp/mix-without-tone-001.wav
```

The same exact-targeting flow also supports `--pid` and `--exclude-pid`
when the OS process ID is more convenient.

Opt-in CI or local integration coverage:

```bash
CATAP_RUN_TONE_INTEGRATION=1 \
  uv run --group dev pytest tests/test_integration.py::test_cli_records_headless_tone_by_audio_object_id
```

That test starts the tone farm, records one fixture process by Core Audio
process object ID, and verifies that the selected tone is present and
another fixture tone is absent.
