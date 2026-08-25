"""Audio property watch tests."""

from __future__ import annotations

import threading

import pytest

import catap.events as events_module
from catap.events import AudioPropertyEvent, AudioPropertyWatch, _selector_to_int

_TFMT = int.from_bytes(b"tfmt", "big")
_TDSC = int.from_bytes(b"tdsc", "big")
_SCOPE_GLOBAL = int.from_bytes(b"glob", "big")
_SCOPE_INPUT = int.from_bytes(b"inpt", "big")
_ELEMENT_MAIN = 0


def _address(
    selector: int,
    scope: int = _SCOPE_GLOBAL,
    element: int = _ELEMENT_MAIN,
) -> list[int]:
    return [selector, scope, element]


class _RecordingListeners:
    def __init__(self) -> None:
        self.added: list[tuple[int, int, object, int, int]] = []
        self.removed: list[tuple[int, int, object, int, int]] = []

    def add(
        self,
        object_id: int,
        selector: int,
        proc: object,
        scope: int,
        element: int,
    ) -> None:
        self.added.append((object_id, selector, proc, scope, element))

    def remove(
        self,
        object_id: int,
        selector: int,
        proc: object,
        scope: int,
        element: int,
    ) -> None:
        self.removed.append((object_id, selector, proc, scope, element))


def _install_listeners(
    monkeypatch: pytest.MonkeyPatch,
    listeners: _RecordingListeners,
) -> None:
    monkeypatch.setattr(events_module, "_add_property_listener", listeners.add)
    monkeypatch.setattr(
        events_module, "_remove_property_listener", listeners.remove
    )


def test_selector_fourcc_renders_printable_selector() -> None:
    event = AudioPropertyEvent(object_id=1, selector=_TFMT)

    assert event.selector_fourcc == "tfmt"


def test_selector_fourcc_falls_back_to_hex_for_unprintable_selector() -> None:
    event = AudioPropertyEvent(object_id=1, selector=0x01020304)

    assert event.selector_fourcc == "0x1020304"


def test_selector_fourcc_falls_back_to_hex_for_oversized_selector() -> None:
    event = AudioPropertyEvent(object_id=1, selector=2**40)

    assert event.selector_fourcc == f"{2**40:#x}"


def test_selector_to_int_accepts_fourcc_strings_and_ints() -> None:
    assert _selector_to_int("tfmt") == _TFMT
    assert _selector_to_int(_TDSC) == _TDSC


@pytest.mark.parametrize("selector", ["tap", "taps!"], ids=["short", "long"])
def test_selector_to_int_rejects_wrong_length_strings(selector: str) -> None:
    with pytest.raises(ValueError, match="must be 4 characters"):
        _selector_to_int(selector)


def test_watch_dispatches_events_to_callback_off_hal_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners = _RecordingListeners()
    _install_listeners(monkeypatch, listeners)

    received: list[AudioPropertyEvent] = []
    callback_threads: list[str] = []
    delivered = threading.Event()

    def callback(event: AudioPropertyEvent) -> None:
        received.append(event)
        callback_threads.append(threading.current_thread().name)
        delivered.set()

    watch = AudioPropertyWatch(12, ["tfmt"], callback)

    assert [added[:2] for added in listeners.added] == [(12, _TFMT)]
    assert watch.is_active is True

    assert watch._on_native_event(12, 1, [_address(_TFMT)], None) == 0
    assert delivered.wait(timeout=5)

    watch.close()

    assert received == [AudioPropertyEvent(object_id=12, selector=_TFMT)]
    assert callback_threads == ["catap-property-watch"]
    assert [removed[:2] for removed in listeners.removed] == [(12, _TFMT)]
    assert watch.is_active is False
    assert watch._thread is not None
    assert watch._thread.is_alive() is False


def test_watch_filters_callback_addresses_to_its_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners = _RecordingListeners()
    _install_listeners(monkeypatch, listeners)

    received: list[AudioPropertyEvent] = []
    delivered = threading.Event()

    def callback(event: AudioPropertyEvent) -> None:
        received.append(event)
        delivered.set()

    with AudioPropertyWatch(12, [_TFMT], callback) as watch:
        assert (
            watch._on_native_event(
                12,
                3,
                [
                    _address(_TFMT),
                    _address(_TDSC),
                    _address(_TFMT, _SCOPE_INPUT),
                ],
                None,
            )
            == 0
        )
        assert delivered.wait(timeout=5)

    assert received == [
        AudioPropertyEvent(
            object_id=12,
            selector=_TFMT,
            scope=_SCOPE_GLOBAL,
            element=_ELEMENT_MAIN,
        )
    ]


def test_watch_property_preserves_scope_and_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners = _RecordingListeners()
    _install_listeners(monkeypatch, listeners)

    received: list[AudioPropertyEvent] = []
    delivered = threading.Event()

    def callback(event: AudioPropertyEvent) -> None:
        received.append(event)
        delivered.set()

    with events_module.watch_property(
        12,
        _TFMT,
        callback,
        scope=_SCOPE_INPUT,
        element=7,
    ) as watch:
        assert (
            watch._on_native_event(
                12,
                1,
                [_address(_TFMT, _SCOPE_INPUT, 7)],
                None,
            )
            == 0
        )
        assert delivered.wait(timeout=5)

    assert received == [
        AudioPropertyEvent(
            object_id=12,
            selector=_TFMT,
            scope=_SCOPE_INPUT,
            element=7,
        )
    ]
    assert [added[1:] for added in listeners.added] == [
        (_TFMT, watch._proc, _SCOPE_INPUT, 7)
    ]
    assert [removed[1:] for removed in listeners.removed] == [
        (_TFMT, watch._proc, _SCOPE_INPUT, 7)
    ]


def test_wildcard_registration_accepts_any_callback_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners = _RecordingListeners()
    _install_listeners(monkeypatch, listeners)

    received: list[AudioPropertyEvent] = []
    delivered = threading.Event()

    def callback(event: AudioPropertyEvent) -> None:
        received.append(event)
        delivered.set()

    with AudioPropertyWatch(
        12,
        [events_module.kAudioObjectPropertySelectorWildcard],
        callback,
        scope=events_module.kAudioObjectPropertyScopeWildcard,
        element=events_module.kAudioObjectPropertyElementWildcard,
    ) as watch:
        assert (
            watch._on_native_event(
                12,
                1,
                [_address(_TDSC, _SCOPE_INPUT, 7)],
                None,
            )
            == 0
        )
        assert delivered.wait(timeout=5)

    assert received == [
        AudioPropertyEvent(
            object_id=12,
            selector=_TDSC,
            scope=_SCOPE_INPUT,
            element=7,
        )
    ]


def test_wildcard_callback_address_matches_specific_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners = _RecordingListeners()
    _install_listeners(monkeypatch, listeners)

    received: list[AudioPropertyEvent] = []
    delivered = threading.Event()

    def callback(event: AudioPropertyEvent) -> None:
        received.append(event)
        delivered.set()

    wildcard = events_module.kAudioObjectPropertySelectorWildcard
    wildcard_scope = events_module.kAudioObjectPropertyScopeWildcard
    wildcard_element = events_module.kAudioObjectPropertyElementWildcard
    with AudioPropertyWatch(12, [_TFMT], callback) as watch:
        assert (
            watch._on_native_event(
                12,
                1,
                [_address(wildcard, wildcard_scope, wildcard_element)],
                None,
            )
            == 0
        )
        assert delivered.wait(timeout=5)

    assert received == [
        AudioPropertyEvent(
            object_id=12,
            selector=wildcard,
            scope=wildcard_scope,
            element=wildcard_element,
        )
    ]


def test_callback_exception_is_collected_without_stopping_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners = _RecordingListeners()
    _install_listeners(monkeypatch, listeners)

    delivered = threading.Event()
    received: list[int] = []

    def callback(event: AudioPropertyEvent) -> None:
        if event.selector == _TFMT:
            raise ValueError("callback boom")
        received.append(event.selector)
        delivered.set()

    with AudioPropertyWatch(12, [_TFMT, _TDSC], callback) as watch:
        assert (
            watch._on_native_event(
                12,
                2,
                [_address(_TFMT), _address(_TDSC)],
                None,
            )
            == 0
        )
        assert delivered.wait(timeout=5)

    assert received == [_TDSC]
    assert len(watch.failures) == 1
    assert isinstance(watch.failures[0], RuntimeError)
    assert "callback boom" in str(watch.failures[0])
    assert isinstance(watch.failures[0].__cause__, ValueError)


def test_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    listeners = _RecordingListeners()
    _install_listeners(monkeypatch, listeners)

    watch = AudioPropertyWatch(12, ["tfmt"], lambda event: None)

    watch.close()
    watch.close()

    assert len(listeners.removed) == 1
    assert watch.is_active is False


def test_close_treats_destroyed_object_listener_removal_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners = _RecordingListeners()
    abandoned: list[object] = []
    stale_error = OSError("bad object")
    stale_error.status = events_module.kAudioHardwareBadObjectError  # type: ignore[attr-defined]

    def _remove_destroyed(
        object_id: int,
        selector: int,
        proc: object,
        scope: int,
        element: int,
    ) -> None:
        del object_id, selector, proc, scope, element
        raise stale_error

    monkeypatch.setattr(events_module, "_add_property_listener", listeners.add)
    monkeypatch.setattr(
        events_module, "_remove_property_listener", _remove_destroyed
    )
    monkeypatch.setattr(events_module, "_ABANDONED_LISTENER_PROCS", abandoned)

    watch = AudioPropertyWatch(12, ["tfmt"], lambda event: None)
    watch.close()

    assert watch._registered_selectors == []
    assert abandoned == []
    assert watch.is_active is False


def test_close_failure_keeps_watch_active_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners = _RecordingListeners()
    active_watches: set[AudioPropertyWatch] = set()
    abandoned: list[object] = []
    remove_calls = 0
    delivered = threading.Event()

    def _remove_once_then_succeed(
        object_id: int,
        selector: int,
        proc: object,
        scope: int,
        element: int,
    ) -> None:
        nonlocal remove_calls
        remove_calls += 1
        if remove_calls == 1:
            error = OSError("transient remove failure")
            error.status = -50  # type: ignore[attr-defined]
            raise error
        listeners.remove(object_id, selector, proc, scope, element)

    monkeypatch.setattr(events_module, "_add_property_listener", listeners.add)
    monkeypatch.setattr(
        events_module,
        "_remove_property_listener",
        _remove_once_then_succeed,
    )
    monkeypatch.setattr(events_module, "_ACTIVE_WATCHES", active_watches)
    monkeypatch.setattr(events_module, "_ABANDONED_LISTENER_PROCS", abandoned)

    watch = AudioPropertyWatch(12, [_TFMT], lambda event: delivered.set())

    with pytest.raises(OSError, match="transient remove failure"):
        watch.close()

    assert watch.is_active is True
    assert watch._registered_selectors == [_TFMT]
    assert watch._thread is not None
    assert watch._thread.is_alive()
    assert watch in active_watches
    assert abandoned == [watch._proc]
    assert watch._on_native_event(12, 1, [_address(_TFMT)], None) == 0
    assert delivered.wait(timeout=5)

    watch.close()

    assert remove_calls == 2
    assert watch.is_active is False
    assert watch._registered_selectors == []
    assert watch._thread.is_alive() is False
    assert watch not in active_watches
    assert abandoned == []


def test_close_keeps_completed_teardown_progress_across_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners = _RecordingListeners()
    removed_selectors: list[int] = []
    abandoned: list[object] = []

    def _remove_with_interrupt(
        object_id: int,
        selector: int,
        proc: object,
        scope: int,
        element: int,
    ) -> None:
        del object_id, proc, scope, element
        removed_selectors.append(selector)
        if selector == _TDSC and removed_selectors.count(_TDSC) == 1:
            raise KeyboardInterrupt("teardown interrupted")

    monkeypatch.setattr(events_module, "_add_property_listener", listeners.add)
    monkeypatch.setattr(
        events_module, "_remove_property_listener", _remove_with_interrupt
    )
    monkeypatch.setattr(events_module, "_ABANDONED_LISTENER_PROCS", abandoned)

    watch = AudioPropertyWatch(12, [_TFMT, _TDSC], lambda event: None)

    with pytest.raises(KeyboardInterrupt, match="teardown interrupted"):
        watch.close()

    assert removed_selectors == [_TFMT, _TDSC]
    assert watch._registered_selectors == [_TDSC]
    assert abandoned == [watch._proc]
    assert watch.is_active is True

    watch.close()

    assert removed_selectors == [_TFMT, _TDSC, _TDSC]
    assert watch._registered_selectors == []
    assert abandoned == []
    assert watch.is_active is False


def test_concurrent_close_waits_for_in_progress_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners = _RecordingListeners()
    remove_entered = threading.Event()
    allow_remove = threading.Event()
    second_started = threading.Event()
    first_done = threading.Event()
    second_done = threading.Event()
    failures: list[BaseException] = []

    def _blocking_remove(
        object_id: int,
        selector: int,
        proc: object,
        scope: int,
        element: int,
    ) -> None:
        remove_entered.set()
        if not allow_remove.wait(timeout=5):
            raise TimeoutError("test did not release listener removal")
        listeners.remove(object_id, selector, proc, scope, element)

    def _close(done: threading.Event, started: threading.Event | None = None) -> None:
        if started is not None:
            started.set()
        try:
            watch.close()
        except BaseException as exc:
            failures.append(exc)
        finally:
            done.set()

    monkeypatch.setattr(events_module, "_add_property_listener", listeners.add)
    monkeypatch.setattr(events_module, "_remove_property_listener", _blocking_remove)
    watch = AudioPropertyWatch(12, [_TFMT], lambda event: None)

    first = threading.Thread(target=_close, args=(first_done,))
    second = threading.Thread(target=_close, args=(second_done, second_started))
    try:
        first.start()
        assert remove_entered.wait(timeout=5)
        second.start()
        assert second_started.wait(timeout=5)
        assert second_done.wait(timeout=0.05) is False
    finally:
        allow_remove.set()
        first.join(timeout=5)
        second.join(timeout=5)

    assert first_done.is_set()
    assert second_done.is_set()
    assert failures == []
    assert len(listeners.removed) == 1
    assert watch.is_active is False


def test_close_retries_if_active_root_release_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners = _RecordingListeners()
    _install_listeners(monkeypatch, listeners)

    class _InterruptingActiveSet(set[AudioPropertyWatch]):
        discard_calls = 0

        def discard(self, watch: AudioPropertyWatch) -> None:
            super().discard(watch)
            self.discard_calls += 1
            if self.discard_calls == 1:
                raise KeyboardInterrupt("after active root release")

    active_watches = _InterruptingActiveSet()
    monkeypatch.setattr(events_module, "_ACTIVE_WATCHES", active_watches)
    watch = AudioPropertyWatch(12, [_TFMT], lambda event: None)

    with pytest.raises(KeyboardInterrupt, match="active root release"):
        watch.close()

    assert watch not in active_watches
    assert watch.is_active is True
    assert watch._registered_selectors == []
    assert watch._thread is not None
    assert watch._thread.is_alive() is False

    watch.close()

    assert active_watches.discard_calls == 2
    assert watch.is_active is False


def test_closed_watch_drops_late_native_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners = _RecordingListeners()
    _install_listeners(monkeypatch, listeners)
    delivered = threading.Event()
    watch = AudioPropertyWatch(12, [_TFMT], lambda event: delivered.set())

    watch.close()
    assert watch._on_native_event(12, 1, [_address(_TFMT)], None) == 0

    assert delivered.wait(timeout=0.05) is False
    assert watch._events.empty()


def test_watch_requires_at_least_one_selector() -> None:
    with pytest.raises(ValueError, match="At least one property selector"):
        AudioPropertyWatch(12, [], lambda event: None)


def test_partial_registration_failure_removes_registered_listeners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners = _RecordingListeners()
    active_watches: set[AudioPropertyWatch] = set()

    def _add_then_fail(
        object_id: int,
        selector: int,
        proc: object,
        scope: int,
        element: int,
    ) -> None:
        if listeners.added:
            error = OSError("second add failed")
            error.status = -50  # type: ignore[attr-defined]
            raise error
        listeners.add(object_id, selector, proc, scope, element)

    monkeypatch.setattr(events_module, "_add_property_listener", _add_then_fail)
    monkeypatch.setattr(
        events_module, "_remove_property_listener", listeners.remove
    )
    monkeypatch.setattr(events_module, "_ACTIVE_WATCHES", active_watches)

    with pytest.raises(OSError, match="second add failed"):
        AudioPropertyWatch(12, ["tfmt", "tdsc"], lambda event: None)

    assert [added[:2] for added in listeners.added] == [(12, _TFMT)]
    assert [removed[:2] for removed in listeners.removed] == [(12, _TFMT)]
    assert active_watches == set()


def test_interrupted_registration_defensively_removes_prebooked_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners = _RecordingListeners()
    active_watches: set[AudioPropertyWatch] = set()
    abandoned: list[object] = []

    def _add_then_interrupt(
        object_id: int,
        selector: int,
        proc: object,
        scope: int,
        element: int,
    ) -> None:
        listeners.add(object_id, selector, proc, scope, element)
        raise KeyboardInterrupt("interrupted after native add")

    monkeypatch.setattr(events_module, "_add_property_listener", _add_then_interrupt)
    monkeypatch.setattr(events_module, "_remove_property_listener", listeners.remove)
    monkeypatch.setattr(events_module, "_ACTIVE_WATCHES", active_watches)
    monkeypatch.setattr(events_module, "_ABANDONED_LISTENER_PROCS", abandoned)

    with pytest.raises(KeyboardInterrupt, match="interrupted after native add"):
        AudioPropertyWatch(12, [_TFMT], lambda event: None)

    assert [added[:2] for added in listeners.added] == [(12, _TFMT)]
    assert [removed[:2] for removed in listeners.removed] == [(12, _TFMT)]
    assert active_watches == set()
    assert abandoned == []


def test_failed_constructor_parks_listener_and_drops_callback_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_watches: set[AudioPropertyWatch] = set()
    abandoned: list[object] = []
    captured_procs: list[object] = []

    def _add_then_interrupt(
        object_id: int,
        selector: int,
        proc: object,
        scope: int,
        element: int,
    ) -> None:
        del object_id, selector, scope, element
        captured_procs.append(proc)
        raise KeyboardInterrupt("interrupted after native add")

    def _remove_fails(
        object_id: int,
        selector: int,
        proc: object,
        scope: int,
        element: int,
    ) -> None:
        del object_id, selector, proc, scope, element
        error = OSError("remove failed")
        error.status = -50  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(
        events_module,
        "AudioObjectPropertyListenerProc",
        lambda callback: callback,
    )
    monkeypatch.setattr(events_module, "_add_property_listener", _add_then_interrupt)
    monkeypatch.setattr(events_module, "_remove_property_listener", _remove_fails)
    monkeypatch.setattr(events_module, "_ACTIVE_WATCHES", active_watches)
    monkeypatch.setattr(events_module, "_ABANDONED_LISTENER_PROCS", abandoned)

    with pytest.raises(KeyboardInterrupt, match="interrupted after native add"):
        AudioPropertyWatch(12, [_TFMT], lambda event: None)

    proc = captured_procs[0]
    watch = proc.__self__  # type: ignore[attr-defined]
    queued_before_callback = watch._events.qsize()
    assert proc(12, 1, [_address(_TFMT)], None) == 0  # type: ignore[operator]

    assert active_watches == {watch}
    assert abandoned == [proc]
    assert watch._registered_selectors == [_TFMT]
    assert watch._accept_events is False
    assert watch._thread is None
    assert watch._events.qsize() == queued_before_callback


def test_close_from_dispatcher_thread_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added: list[tuple[int, int]] = []
    removed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        events_module,
        "_add_property_listener",
        lambda object_id, selector, proc, scope, element: added.append(
            (object_id, selector)
        ),
    )
    monkeypatch.setattr(
        events_module,
        "_remove_property_listener",
        lambda object_id, selector, proc, scope, element: removed.append(
            (object_id, selector)
        ),
    )

    watch = events_module.AudioPropertyWatch(1, ["tst "], lambda event: None)
    dispatcher_thread = watch._thread
    try:
        watch._thread = threading.current_thread()
        with pytest.raises(RuntimeError, match="from its own callback"):
            watch.close()
    finally:
        watch._thread = dispatcher_thread
        watch.close()
    assert removed
