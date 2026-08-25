"""Watch Core Audio property changes.

Core Audio delivers property-change notifications on its own internal
threads. The watches in this module hand events off to a catap-owned
dispatcher thread, so user callbacks never run on a HAL thread and can
safely call back into Core Audio (for example to re-list processes).
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass

from catap._recording_support import _combine_errors, _translate_exception
from catap.bindings._coreaudio import (
    AudioObjectPropertyListenerProc,
    add_property_listener as _add_property_listener,
    kAudioHardwareBadObjectError,
    kAudioHardwareUnknownPropertyError,
    kAudioObjectPropertyElementMain,
    kAudioObjectPropertyScopeGlobal,
    kAudioObjectSystemObject,
    remove_property_listener as _remove_property_listener,
)

kAudioHardwarePropertyDevices = int.from_bytes(b"dev#", "big")
kAudioHardwarePropertyDefaultOutputDevice = int.from_bytes(b"dOut", "big")
kAudioHardwarePropertyProcessObjectList = int.from_bytes(b"prs#", "big")
kAudioHardwarePropertyTapList = int.from_bytes(b"tps#", "big")
kAudioTapPropertyDescription = int.from_bytes(b"tdsc", "big")
kAudioTapPropertyFormat = int.from_bytes(b"tfmt", "big")
kAudioObjectPropertySelectorWildcard = int.from_bytes(b"****", "big")
kAudioObjectPropertyScopeWildcard = int.from_bytes(b"****", "big")
kAudioObjectPropertyElementWildcard = 0xFFFFFFFF

# Core Audio holds raw listener function pointers until deregistration
# succeeds. If removal fails, retain the ctypes callback for process lifetime
# rather than letting a live HAL notification call freed memory.
_ABANDONED_LISTENER_PROCS: list[object] = []
_ABANDONED_LISTENER_PROCS_LOCK = threading.Lock()

# Every open watch is rooted here so that dropping the last user reference to
# an unclosed watch cannot garbage-collect the ctypes trampoline while Core
# Audio still holds its raw function pointer. close() releases the root.
_ACTIVE_WATCHES: set[AudioPropertyWatch] = set()
_ACTIVE_WATCHES_LOCK = threading.Lock()


def _selector_to_int(selector: int | str) -> int:
    """Accept a fourcc string or raw integer property selector."""
    if isinstance(selector, int):
        return selector
    encoded = selector.encode("latin-1")
    if len(encoded) != 4:
        raise ValueError(
            f"Property selectors must be 4 characters, got {selector!r}"
        )
    return int.from_bytes(encoded, "big")


def _selector_to_fourcc(selector: int) -> str:
    """Render a selector integer as its printable fourcc when possible."""
    try:
        text = selector.to_bytes(4, "big").decode("latin-1")
    except OverflowError:
        return f"{selector:#x}"
    if all(" " <= char <= "~" for char in text):
        return text
    return f"{selector:#x}"


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioPropertyEvent:
    """One property-change notification from Core Audio."""

    object_id: int
    selector: int
    scope: int = kAudioObjectPropertyScopeGlobal
    element: int = kAudioObjectPropertyElementMain

    @property
    def selector_fourcc(self) -> str:
        """Printable fourcc form of the changed property selector."""
        return _selector_to_fourcc(self.selector)


class AudioPropertyWatch:
    """Watches selected properties of one Core Audio object.

    The callback runs on a dedicated dispatcher thread owned by this watch.
    Callback exceptions do not stop the watch; they are collected on
    ``failures`` for the owner to inspect.
    """

    def __init__(
        self,
        object_id: int,
        selectors: Sequence[int | str],
        callback: Callable[[AudioPropertyEvent], None],
        *,
        scope: int = kAudioObjectPropertyScopeGlobal,
        element: int = kAudioObjectPropertyElementMain,
    ) -> None:
        if not selectors:
            raise ValueError("At least one property selector is required")

        self.object_id = object_id
        self.selectors = tuple(_selector_to_int(s) for s in selectors)
        self.failures: list[RuntimeError] = []
        self._callback = callback
        self._scope = scope
        self._element = element
        self._events: queue.SimpleQueue[AudioPropertyEvent | None] = (
            queue.SimpleQueue()
        )
        self._registered_selectors: list[int] = []
        self._closed = False
        self._close_lock = threading.Lock()
        self._event_gate = threading.Lock()
        self._accept_events = True
        self._thread: threading.Thread | None = None

        # Bound method references keep self alive while Core Audio holds the
        # proc, and the proc object itself must stay referenced until every
        # selector is deregistered.
        self._proc = AudioObjectPropertyListenerProc(self._on_native_event)

        try:
            # Root the watch before the first native registration. A Python
            # asynchronous exception can be raised immediately after the C
            # call has installed the raw callback, before Python can record
            # that fact; the root keeps the trampoline alive throughout that
            # interruption window.
            with _ACTIVE_WATCHES_LOCK:
                _ACTIVE_WATCHES.add(self)
            for selector in self.selectors:
                # Record a possibly-live registration before entering Core
                # Audio. An uncertain add outcome must be cleaned up (or kept
                # rooted if removal is refused); forgetting an installed raw
                # function pointer can become a use-after-free.
                with self._event_gate:
                    self._registered_selectors.append(selector)
                try:
                    _add_property_listener(
                        object_id, selector, self._proc, scope, element
                    )
                except OSError as exc:
                    # The binding raises status-bearing OSErrors only after
                    # Core Audio returns a nonzero status, so this selector was
                    # not installed. Keep the conservative pre-registration
                    # ledger entry for asynchronous Python exceptions, whose
                    # delivery can occur after the C call succeeded.
                    if getattr(exc, "status", None) is not None:
                        self._forget_registered_selector(selector)
                    raise
            self._thread = threading.Thread(
                target=self._dispatch_loop,
                name="catap-property-watch",
                daemon=True,
            )
            self._thread.start()
        except BaseException:
            try:
                self._teardown_listeners()
            finally:
                # Even if cleanup itself is interrupted, any native callback
                # that may remain installed must stop feeding this
                # constructor's dispatcher-less queue.
                self._stop_dispatcher()
                thread = self._thread
                if not self._registered_selectors and (
                    thread is None or not thread.is_alive()
                ):
                    with _ACTIVE_WATCHES_LOCK:
                        _ACTIVE_WATCHES.discard(self)
            raise

    def _on_native_event(
        self,
        object_id: int,
        number_of_addresses: int,
        addresses: object,
        client_data: object,
    ) -> int:
        """Enqueue events from Core Audio's notification thread."""
        del client_data
        try:
            with self._event_gate:
                if not self._accept_events:
                    return 0
                registered_selectors = tuple(self._registered_selectors)
                for index in range(number_of_addresses):
                    address = addresses[index]  # type: ignore[index]
                    selector = int(address[0])
                    scope = int(address[1])
                    element = int(address[2])
                    if not self._matches_address(
                        selector,
                        scope,
                        element,
                        registered_selectors,
                    ):
                        continue
                    self._events.put(
                        AudioPropertyEvent(
                            object_id=object_id,
                            selector=selector,
                            scope=scope,
                            element=element,
                        )
                    )
        except BaseException:
            # Never let an exception escape into the HAL notification thread.
            pass
        return 0

    def _matches_address(
        self,
        selector: int,
        scope: int,
        element: int,
        registered_selectors: tuple[int, ...],
    ) -> bool:
        """Return whether a callback address matches this watch."""
        selector_matches = selector == kAudioObjectPropertySelectorWildcard or any(
            registered in (selector, kAudioObjectPropertySelectorWildcard)
            for registered in registered_selectors
        )
        scope_matches = (
            self._scope == scope
            or self._scope == kAudioObjectPropertyScopeWildcard
            or scope == kAudioObjectPropertyScopeWildcard
        )
        element_matches = (
            self._element == element
            or self._element == kAudioObjectPropertyElementWildcard
            or element == kAudioObjectPropertyElementWildcard
        )
        return selector_matches and scope_matches and element_matches

    def _dispatch_loop(self) -> None:
        while True:
            event = self._events.get()
            if event is None:
                return
            try:
                self._callback(event)
            except BaseException as exc:
                failure = _translate_exception(
                    RuntimeError,
                    f"Audio property watch callback failed: {exc}",
                    exc,
                )
                assert isinstance(failure, RuntimeError)
                self.failures.append(failure)

    def _teardown_listeners(self) -> list[BaseException]:
        """Deregister listeners, retaining the proc if any removal fails."""
        errors: list[BaseException] = []
        # Work from a snapshot, but remove each successful registration from
        # the live ownership ledger immediately. If an asynchronous exception
        # interrupts teardown later, already-completed progress remains known
        # and only possibly-live registrations are retried.
        try:
            for selector in tuple(self._registered_selectors):
                try:
                    _remove_property_listener(
                        self.object_id,
                        selector,
                        self._proc,
                        self._scope,
                        self._element,
                    )
                except OSError as exc:
                    # A destroyed object (for example a vanished tap) takes
                    # its listener registrations with it, so a bad-object or
                    # unknown-property refusal means there is nothing left to
                    # remove.
                    if getattr(exc, "status", None) not in {
                        kAudioHardwareBadObjectError,
                        kAudioHardwareUnknownPropertyError,
                    }:
                        errors.append(exc)
                    else:
                        self._forget_registered_selector(selector)
                except BaseException as exc:
                    errors.append(exc)
                else:
                    self._forget_registered_selector(selector)
        finally:
            self._sync_listener_proc_root()
        return errors

    def _sync_listener_proc_root(self) -> None:
        """Park or release the callback according to the live ledger."""
        with self._event_gate:
            remaining = bool(self._registered_selectors)
        if remaining:
            with _ABANDONED_LISTENER_PROCS_LOCK:
                if self._proc not in _ABANDONED_LISTENER_PROCS:
                    _ABANDONED_LISTENER_PROCS.append(self._proc)
        else:
            # A previous close attempt may have parked the callback after a
            # transient removal failure. Once every retry succeeds, release
            # that process-lifetime fallback root.
            with _ABANDONED_LISTENER_PROCS_LOCK:
                _ABANDONED_LISTENER_PROCS[:] = [
                    proc for proc in _ABANDONED_LISTENER_PROCS if proc is not self._proc
                ]

    def _forget_registered_selector(self, selector: int) -> None:
        """Record successful removal of one possibly-live registration."""
        with self._event_gate, suppress(ValueError):
            self._registered_selectors.remove(selector)

    def _stop_dispatcher(self) -> list[BaseException]:
        """Stop accepting events and join the dispatcher, allowing retries."""
        errors: list[BaseException] = []
        try:
            with self._event_gate:
                # Stop native producers before queueing the sentinel. Any
                # callback that was already inside the gate queues ahead of
                # it; callbacks arriving afterward drop their events.
                self._accept_events = False
        except BaseException as exc:
            errors.append(exc)
            return errors

        thread = self._thread
        if thread is not None and thread.is_alive():
            try:
                self._events.put(None)
                thread.join(timeout=5.0)
            except BaseException as exc:
                errors.append(exc)
            else:
                if thread.is_alive():
                    errors.append(
                        RuntimeError("Property watch dispatcher did not stop")
                    )
        return errors

    @property
    def is_active(self) -> bool:
        """True until ``close()`` completes."""
        return not self._closed

    def close(self) -> None:
        """Stop watching and release the dispatcher thread. Idempotent."""
        if threading.current_thread() is self._thread:
            raise RuntimeError(
                "Cannot close an AudioPropertyWatch from its own callback; "
                "signal the owning thread with threading.Event and close "
                "there instead"
            )

        errors: list[BaseException]
        with self._close_lock:
            if self._closed:
                return
            errors = self._teardown_listeners()
            if not self._registered_selectors:
                errors.extend(self._stop_dispatcher())
            if not errors:
                # Release the GC root before publishing the terminal flag.
                # If an asynchronous exception interrupts the set operation,
                # a retry can safely repeat this already-completed teardown;
                # publishing ``_closed`` first would make that retry return
                # while the watch remained rooted forever.
                with _ACTIVE_WATCHES_LOCK:
                    _ACTIVE_WATCHES.discard(self)
                self._closed = True
        if errors:
            raise _combine_errors("Failed to close audio property watch", errors)

    def __enter__(self) -> AudioPropertyWatch:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.close()


def watch_property(
    object_id: int,
    selectors: Sequence[int | str] | int | str,
    callback: Callable[[AudioPropertyEvent], None],
    *,
    scope: int = kAudioObjectPropertyScopeGlobal,
    element: int = kAudioObjectPropertyElementMain,
) -> AudioPropertyWatch:
    """Watch arbitrary properties on any Core Audio object."""
    if isinstance(selectors, (int, str)):
        selectors = [selectors]
    return AudioPropertyWatch(
        object_id,
        selectors,
        callback,
        scope=scope,
        element=element,
    )


def watch_audio_processes(
    callback: Callable[[AudioPropertyEvent], None],
) -> AudioPropertyWatch:
    """Fire when the set of Core Audio process objects changes."""
    return AudioPropertyWatch(
        kAudioObjectSystemObject,
        [kAudioHardwarePropertyProcessObjectList],
        callback,
    )


def watch_audio_taps(
    callback: Callable[[AudioPropertyEvent], None],
) -> AudioPropertyWatch:
    """Fire when the set of visible Core Audio taps changes."""
    return AudioPropertyWatch(
        kAudioObjectSystemObject,
        [kAudioHardwarePropertyTapList],
        callback,
    )


def watch_audio_devices(
    callback: Callable[[AudioPropertyEvent], None],
) -> AudioPropertyWatch:
    """Fire when the set of Core Audio devices changes."""
    return AudioPropertyWatch(
        kAudioObjectSystemObject,
        [kAudioHardwarePropertyDevices],
        callback,
    )


def watch_default_output_device(
    callback: Callable[[AudioPropertyEvent], None],
) -> AudioPropertyWatch:
    """Fire when the default output device changes."""
    return AudioPropertyWatch(
        kAudioObjectSystemObject,
        [kAudioHardwarePropertyDefaultOutputDevice],
        callback,
    )


def watch_tap(
    tap_id: int,
    callback: Callable[[AudioPropertyEvent], None],
) -> AudioPropertyWatch:
    """Fire when a tap's stream format or description changes."""
    return AudioPropertyWatch(
        tap_id,
        [kAudioTapPropertyFormat, kAudioTapPropertyDescription],
        callback,
    )


__all__ = [
    "AudioPropertyEvent",
    "AudioPropertyWatch",
    "watch_audio_devices",
    "watch_audio_processes",
    "watch_audio_taps",
    "watch_default_output_device",
    "watch_property",
    "watch_tap",
]
