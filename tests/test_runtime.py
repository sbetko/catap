"""Runtime compatibility tests."""

from __future__ import annotations

from catap._runtime import get_runtime_support_error


def test_runtime_support_error_for_non_macos(monkeypatch) -> None:
    monkeypatch.setattr("catap._runtime.platform.system", lambda: "Linux")

    assert get_runtime_support_error() == "catap only supports macOS 14.2 or later."


def test_runtime_support_error_for_old_macos(monkeypatch) -> None:
    monkeypatch.setattr("catap._runtime.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "catap._runtime.platform.mac_ver",
        lambda: ("13.6.7", ("", "", ""), ""),
    )

    assert (
        get_runtime_support_error()
        == "catap requires macOS 14.2 or later. Detected macOS 13.6.7."
    )


def test_runtime_support_error_absent_for_supported_macos(monkeypatch) -> None:
    monkeypatch.setattr("catap._runtime.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "catap._runtime.platform.mac_ver",
        lambda: ("14.5", ("", "", ""), ""),
    )

    assert get_runtime_support_error() is None
