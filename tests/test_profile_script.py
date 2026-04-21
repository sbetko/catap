"""Regression tests for the profiling helper script."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_profile_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "profile_catap.py"
    spec = importlib.util.spec_from_file_location("profile_catap_script", script_path)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_profile_io_proc_runs_with_streaming_target() -> None:
    profile_script = _load_profile_script()

    results = profile_script._profile_io_proc()

    assert results["no_queue"]["iterations"] == 20_000
    assert results["with_queue"]["iterations"] == 5_000
    assert results["with_queue"]["queued_items"] == 5_000
    assert "ncalls" in results["profile"]
