"""Opt-in macOS integration smoke tests."""

from __future__ import annotations

import os
import platform

import pytest

RUN_INTEGRATION = os.getenv("CATAP_RUN_INTEGRATION") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(platform.system() != "Darwin", reason="macOS-only"),
]


def test_list_audio_processes_smoke() -> None:
    if not RUN_INTEGRATION:
        pytest.skip("set CATAP_RUN_INTEGRATION=1 to run integration smoke tests")

    from catap import list_audio_processes

    processes = list_audio_processes()
    assert isinstance(processes, list)

    for process in processes[:5]:
        assert isinstance(process.audio_object_id, int)
        assert process.audio_object_id > 0
        assert isinstance(process.pid, int)
        assert process.pid >= 0
        assert isinstance(process.name, str)
        assert process.name
        assert isinstance(process.is_outputting, bool)
