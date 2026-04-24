"""Shared Core Audio helper tests."""

from __future__ import annotations

import ctypes
import struct
from typing import Any

import pytest

import catap.bindings._coreaudio as coreaudio_module


class _TinyStruct(ctypes.Structure):
    _fields_ = [("value", ctypes.c_uint32)]


def _set_uint32(pointer: Any, value: int) -> None:
    ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value = value


def _set_void_p(pointer: Any, value: int) -> None:
    ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value = value


def _get_data_size_returning(size_bytes: int) -> object:
    def get_property_data_size(
        object_id: int,
        address: object,
        qualifier_size: int,
        qualifier_data: object,
        size: Any,
    ) -> int:
        del object_id, address, qualifier_size, qualifier_data
        _set_uint32(size, size_bytes)
        return 0

    return get_property_data_size


def test_get_property_data_size_returns_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get_property_data_size(
        object_id: int,
        address: object,
        qualifier_size: int,
        qualifier_data: object,
        size: object,
    ) -> int:
        del address, qualifier_size, qualifier_data
        assert object_id == 7
        _set_uint32(size, 12)
        return 0

    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyDataSize",
        get_property_data_size,
    )

    address = coreaudio_module._property_address(0x1234)

    assert coreaudio_module.get_property_data_size(7, address) == 12


def test_get_property_data_size_raises_status_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyDataSize",
        lambda object_id, address, qualifier_size, qualifier_data, size: -50,
    )
    address = coreaudio_module._property_address(0x1234)

    with pytest.raises(OSError, match="status -50") as exc_info:
        coreaudio_module.get_property_data_size(7, address)

    assert exc_info.value.status == -50  # type: ignore[attr-defined]


def test_get_property_bytes_returns_empty_for_zero_sized_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyDataSize",
        _get_data_size_returning(0),
    )
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyData",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("data fetch should be skipped")
        ),
    )

    assert coreaudio_module.get_property_bytes(7, 0x1234) == b""


def test_get_property_bytes_returns_actual_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get_property_data_size(
        object_id: int,
        address: object,
        qualifier_size: int,
        qualifier_data: object,
        size: object,
    ) -> int:
        del object_id, address, qualifier_size, qualifier_data
        _set_uint32(size, 8)
        return 0

    def get_property_data(
        object_id: int,
        address: object,
        qualifier_size: int,
        qualifier_data: object,
        actual_size: object,
        buffer: Any,
    ) -> int:
        del object_id, address, qualifier_size, qualifier_data
        ctypes.memmove(buffer, b"abcZZZZZ", 8)
        _set_uint32(actual_size, 3)
        return 0

    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyDataSize",
        get_property_data_size,
    )
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyData",
        get_property_data,
    )

    assert coreaudio_module.get_property_bytes(7, 0x1234) == b"abc"


def test_get_property_bytes_raises_status_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyDataSize",
        lambda object_id, address, qualifier_size, qualifier_data, size: (
            _set_uint32(size, 4) or 0
        ),
    )
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyData",
        lambda object_id,
        address,
        qualifier_size,
        qualifier_data,
        actual_size,
        buffer: 12,
    )

    with pytest.raises(OSError, match="status 12") as exc_info:
        coreaudio_module.get_property_bytes(7, 0x1234)

    assert exc_info.value.status == 12  # type: ignore[attr-defined]


def test_get_property_struct_fetches_ctypes_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get_property_data(
        object_id: int,
        address: object,
        qualifier_size: int,
        qualifier_data: object,
        actual_size: Any,
        value: Any,
    ) -> int:
        del object_id, address, qualifier_size, qualifier_data
        _set_uint32(actual_size, ctypes.sizeof(_TinyStruct))
        ctypes.cast(value, ctypes.POINTER(_TinyStruct)).contents.value = 42
        return 0

    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyData",
        get_property_data,
    )

    value = coreaudio_module.get_property_struct(7, 0x1234, _TinyStruct)

    assert isinstance(value, _TinyStruct)
    assert value.value == 42


def test_get_property_struct_raises_status_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyData",
        lambda object_id,
        address,
        qualifier_size,
        qualifier_data,
        actual_size,
        value: 42,
    )

    with pytest.raises(OSError, match="status 42") as exc_info:
        coreaudio_module.get_property_struct(7, 0x1234, _TinyStruct)

    assert exc_info.value.status == 42  # type: ignore[attr-defined]


def test_get_property_cfstring_returns_none_for_zero_sized_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyDataSize",
        _get_data_size_returning(0),
    )
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyData",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("data fetch should be skipped")
        ),
    )

    assert coreaudio_module.get_property_cfstring(7, 0x1234) is None


def test_get_property_cfstring_returns_none_for_empty_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get_property_data(
        object_id: int,
        address: object,
        qualifier_size: int,
        qualifier_data: object,
        actual_size: Any,
        value: Any,
    ) -> int:
        del object_id, address, qualifier_size, qualifier_data, value
        _set_uint32(actual_size, ctypes.sizeof(ctypes.c_void_p))
        return 0

    released_refs: list[int] = []
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyDataSize",
        _get_data_size_returning(ctypes.sizeof(ctypes.c_void_p)),
    )
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyData",
        get_property_data,
    )
    monkeypatch.setattr(
        coreaudio_module,
        "_CFRelease",
        lambda ref: released_refs.append(ref.value),
    )

    assert coreaudio_module.get_property_cfstring(7, 0x1234) is None
    assert released_refs == []


def test_get_property_cfstring_raises_status_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyDataSize",
        _get_data_size_returning(ctypes.sizeof(ctypes.c_void_p)),
    )
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyData",
        lambda object_id,
        address,
        qualifier_size,
        qualifier_data,
        actual_size,
        value: 42,
    )

    with pytest.raises(OSError, match="status 42") as exc_info:
        coreaudio_module.get_property_cfstring(7, 0x1234)

    assert exc_info.value.status == 42  # type: ignore[attr-defined]


def test_get_property_cfstring_wraps_and_releases_core_foundation_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objc_refs: list[int] = []
    released_refs: list[int] = []

    def get_property_data_size(
        object_id: int,
        address: object,
        qualifier_size: int,
        qualifier_data: object,
        size: object,
    ) -> int:
        del object_id, address, qualifier_size, qualifier_data
        _set_uint32(size, ctypes.sizeof(ctypes.c_void_p))
        return 0

    def get_property_data(
        object_id: int,
        address: object,
        qualifier_size: int,
        qualifier_data: object,
        actual_size: object,
        value: object,
    ) -> int:
        del object_id, address, qualifier_size, qualifier_data
        _set_uint32(actual_size, ctypes.sizeof(ctypes.c_void_p))
        _set_void_p(value, 12345)
        return 0

    def objc_object(*, c_void_p: int) -> str:
        objc_refs.append(c_void_p)
        return "Built-in Speakers"

    def cf_release(ref: ctypes.c_void_p) -> None:
        assert ref.value is not None
        released_refs.append(ref.value)

    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyDataSize",
        get_property_data_size,
    )
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyData",
        get_property_data,
    )
    monkeypatch.setattr(coreaudio_module.objc, "objc_object", objc_object)
    monkeypatch.setattr(coreaudio_module, "_CFRelease", cf_release)

    assert coreaudio_module.get_property_cfstring(7, 0x1234) == "Built-in Speakers"
    assert objc_refs == [12345]
    assert released_refs == [12345]


def test_get_property_objc_object_wraps_non_empty_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objc_refs: list[int] = []

    def get_property_data(
        object_id: int,
        address: object,
        qualifier_size: int,
        qualifier_data: object,
        actual_size: Any,
        value: Any,
    ) -> int:
        del object_id, address, qualifier_size, qualifier_data
        _set_uint32(actual_size, ctypes.sizeof(ctypes.c_void_p))
        _set_void_p(value, 24680)
        return 0

    def objc_object(*, c_void_p: int) -> str:
        objc_refs.append(c_void_p)
        return "objc-value"

    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyDataSize",
        _get_data_size_returning(ctypes.sizeof(ctypes.c_void_p)),
    )
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyData",
        get_property_data,
    )
    monkeypatch.setattr(coreaudio_module.objc, "objc_object", objc_object)

    assert coreaudio_module.get_property_objc_object(7, 0x1234) == "objc-value"
    assert objc_refs == [24680]


def test_get_property_objc_object_rejects_zero_sized_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyDataSize",
        _get_data_size_returning(0),
    )

    with pytest.raises(OSError, match="returned no object"):
        coreaudio_module.get_property_objc_object(7, 0x1234)


def test_get_property_objc_object_rejects_empty_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get_property_data(
        object_id: int,
        address: object,
        qualifier_size: int,
        qualifier_data: object,
        actual_size: Any,
        value: object,
    ) -> int:
        del object_id, address, qualifier_size, qualifier_data, value
        _set_uint32(actual_size, ctypes.sizeof(ctypes.c_void_p))
        return 0

    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyDataSize",
        _get_data_size_returning(ctypes.sizeof(ctypes.c_void_p)),
    )
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyData",
        get_property_data,
    )

    with pytest.raises(OSError, match="returned an empty object"):
        coreaudio_module.get_property_objc_object(7, 0x1234)


def test_get_property_objc_object_raises_status_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyDataSize",
        _get_data_size_returning(ctypes.sizeof(ctypes.c_void_p)),
    )
    monkeypatch.setattr(
        coreaudio_module,
        "_AudioObjectGetPropertyData",
        lambda object_id,
        address,
        qualifier_size,
        qualifier_data,
        actual_size,
        value: 42,
    )

    with pytest.raises(OSError, match="status 42") as exc_info:
        coreaudio_module.get_property_objc_object(7, 0x1234)

    assert exc_info.value.status == 42  # type: ignore[attr-defined]


def test_get_property_object_ids_returns_empty_for_empty_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coreaudio_module, "get_property_bytes", lambda *args: b"")

    assert coreaudio_module.get_property_object_ids(1, 2) == []


def test_get_property_object_ids_decodes_complete_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "get_property_bytes",
        lambda *args: struct.pack("<III", 10, 20, 30),
    )

    assert coreaudio_module.get_property_object_ids(1, 2) == [10, 20, 30]


def test_get_property_object_ids_ignores_trailing_partial_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "get_property_bytes",
        lambda *args: struct.pack("<II", 10, 20) + b"\x99\x88",
    )

    assert coreaudio_module.get_property_object_ids(1, 2) == [10, 20]


def test_get_optional_property_cfstring_swallows_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "get_property_cfstring",
        lambda *args: (_ for _ in ()).throw(OSError("missing property")),
    )

    assert coreaudio_module.get_optional_property_cfstring(1, 2) is None


def test_get_optional_property_cfstring_returns_string_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "get_property_cfstring",
        lambda *args: "Built-in Speakers",
    )

    assert coreaudio_module.get_optional_property_cfstring(1, 2) == "Built-in Speakers"


def test_get_optional_property_cfstring_preserves_none_from_empty_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "get_property_cfstring",
        lambda *args: None,
    )

    assert coreaudio_module.get_optional_property_cfstring(1, 2) is None


def test_get_optional_property_cfstring_does_not_swallow_unrelated_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coreaudio_module,
        "get_property_cfstring",
        lambda *args: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        coreaudio_module.get_optional_property_cfstring(1, 2)
