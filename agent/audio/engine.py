"""AudioEngine: a single dedicated thread that owns COM and serialises all audio.

Why a dedicated thread: pycaw sits on Windows COM,
which is apartment-threaded. Initialising COM once here and funnelling every audio
call through this one thread avoids the intermittent failures that come from
calling COM objects across threads.

The same thread also runs the periodic poll, so commands and polling never race
each other on the COM apartment. A short queue timeout interleaves the two:
pending commands run promptly; the poll fires once per interval.
"""
from __future__ import annotations

import concurrent.futures
import gc
import queue
import threading
import time
from dataclasses import asdict
from typing import Any, Callable

from ..log import get_logger
from .backend import AudioBackend

log = get_logger("audio.engine")

# fn(backend) -> Any
Job = Callable[[AudioBackend], Any]
PollCallback = Callable[[dict[str, Any]], None]


class EngineBusy(RuntimeError):
    """The owner queue has reached its global admission budget."""


class ObservationUncertain(RuntimeError):
    """A started native write/readback failed; the write may have taken effect."""


def _detach_owner_exception(error: BaseException) -> BaseException:
    """Return plain error data without any COM-owner stack or object references.

    Clearing only the outer traceback is insufficient: explicit causes and
    implicit contexts can retain native method frames too. A fresh exception
    prevents future.result() on another thread from reattaching a consumer stack
    to the original owner exception. Ordinary types/arguments/attributes survive;
    exceptions carrying non-plain native objects become a textual RuntimeError.
    """
    try:
        message = str(error)
    except Exception:
        message = type(error).__name__

    def plain(value, visiting):
        if type(value) in (str, int, float, bool, bytes, type(None)):
            return value
        identity = id(value)
        if identity in visiting:
            raise TypeError("cyclic exception data")
        visiting.add(identity)
        try:
            if type(value) is tuple:
                return tuple(plain(item, visiting) for item in value)
            if type(value) is list:
                return [plain(item, visiting) for item in value]
            if type(value) is dict:
                return {plain(key, visiting): plain(item, visiting) for key, item in value.items()}
            raise TypeError("native or non-plain exception data")
        finally:
            visiting.remove(identity)

    chain, seen = [error], set()
    while chain:
        item = chain.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        chain.extend(related for related in (item.__cause__, item.__context__) if related is not None)
        item.__traceback__ = None
        item.__cause__ = None
        item.__context__ = None

    try:
        args = plain(error.args, set())
        attributes = plain(error.__dict__, set())
        # Built-in errors and COMError expose useful fields outside __dict__.
        fields = {"errno", "strerror", "filename", "filename2", "winerror", "characters_written",
                  "name", "path", "hresult", "text", "details", "msg", "lineno", "offset",
                  "end_lineno", "end_offset", "value", "encoding", "object", "start", "end",
                  "reason", "print_file_and_line"}
        for cls in type(error).__mro__:
            slots = cls.__dict__.get("__slots__", ())
            fields.update((slots,) if isinstance(slots, str) else slots)
        for field in fields - {"__dict__", "__weakref__"}:
            try:
                attributes[field] = plain(getattr(error, field), set())
            except AttributeError:
                pass
        try:
            detached = BaseException.__new__(type(error))
            BaseException.__init__(detached, *args)
        except TypeError:
            # OSError/COMError need their native built-in constructor, which
            # receives only the checked plain argument values above.
            if type(error).__module__ not in {"builtins", "_ctypes", "ctypes", "comtypes"}:
                raise
            detached = type(error)(*args)
        for field, value in attributes.items():
            try:
                unchanged = getattr(detached, field) == value
            except AttributeError:
                unchanged = False
            if not unchanged:
                setattr(detached, field, value)
        # A specialised constructor can add fields: validate those too.
        plain(detached.args, set())
        plain(detached.__dict__, set())
        if str(detached) != message:
            raise ValueError("exception message requires unsafe owner state")
        return detached
    except Exception:
        detached = RuntimeError(message)
        detached.original_type = type(error).__module__ + "." + type(error).__qualname__
        return detached


class AudioEngine:
    def __init__(
        self,
        backend_factory: Callable[[], AudioBackend],
        poll_interval_s: float,
        on_poll: PollCallback | None = None,
        queue_limit: int = 64,
        discovery_interval_s: float = 2.0,
    ) -> None:
        self._backend_factory = backend_factory
        self._poll_interval = poll_interval_s
        self._on_poll: PollCallback = on_poll or (lambda _data: None)
        self._q: "queue.Queue[tuple[Job, concurrent.futures.Future, float]]" = queue.Queue(maxsize=queue_limit)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._init_error: BaseException | None = None
        self._submission_lock = threading.Lock()
        self._owner_seq = 0
        self._progress_lock = threading.Lock()
        self._last_progress = time.monotonic()
        self._job_started: float | None = None
        self._poll_failures = 0
        self._discovery_interval = discovery_interval_s
        self._devices = None
        self._last_discovery = 0.0
        self._last_gc_duration_ms = 0.0
        self._last_poll_duration_ms = 0.0

    def set_on_poll(self, on_poll: PollCallback) -> None:
        """Set the poll callback before start() (controller is built after the engine)."""
        self._on_poll = on_poll

    # ---- lifecycle --------------------------------------------------------
    def start(self, wait: bool = True, timeout: float = 5.0) -> None:
        if self._thread is not None:
            raise RuntimeError("engine already started")
        self._thread = threading.Thread(target=self._run, name="audio-engine", daemon=True)
        self._thread.start()
        if wait:
            if not self._ready.wait(timeout):
                raise TimeoutError("audio engine startup timed out; native startup may still be running")
            if self._init_error is not None:
                raise self._init_error

    def stop(self) -> None:
        # Pair admission with shutdown so no job can enter the queue after it
        # has been drained. Already running jobs finish on their COM apartment.
        with self._submission_lock:
            self._stop.set()
        # Future completion invokes callbacks synchronously. Run these without
        # the admission lock so a callback can safely attempt another submit.
        self._fail_pending(RuntimeError("engine stopped"))
        if self._thread:
            self._thread.join(timeout=3.0)

    # ---- command submission ----------------------------------------------
    def submit(self, fn: Job) -> concurrent.futures.Future:
        """Queue a job to run on the COM thread; returns a Future with the result."""
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._submission_lock:
            if self._stop.is_set():
                fut.set_exception(RuntimeError("engine stopped"))
                return fut
            try:
                self._q.put_nowait((fn, fut, time.monotonic()))
            except queue.Full:
                fut.set_exception(EngineBusy("audio engine queue is full"))
        return fut

    def submit_observation(self, target: dict[str, Any], write: Job) -> concurrent.futures.Future:
        """Write and actual readback share one owner turn, stamped before delivery."""
        def observe(backend):
            try:
                write(backend)
                if target["kind"] == "session":
                    item = next((s for s in backend.snapshot_sessions() if s.id == target["id"]), None)
                    if item is None:
                        raise ValueError("audio session is no longer available")
                else:
                    item = backend.get_master(target["kind"])
            except Exception as exc:
                raise ObservationUncertain(str(exc)) from exc
            self._owner_seq += 1
            return {"target": dict(target), "ownerSeq": self._owner_seq,
                    "observation": asdict(item)}
        return self.submit(observe)

    def health(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._progress_lock:
            age = now - self._last_progress
            job_age = now - self._job_started if self._job_started is not None else 0.0
            failures = self._poll_failures
            poll_duration, gc_duration = self._last_poll_duration_ms, self._last_gc_duration_ms
        with self._q.mutex:
            queued_age = now - self._q.queue[0][2] if self._q.queue else 0.0
        alive = bool(self._thread and self._thread.is_alive())
        ready = self._ready.is_set() and self._init_error is None and alive and not self._stop.is_set()
        return {"ready": ready, "status": "degraded" if ready and (failures or job_age > 5 or age > max(5, self._poll_interval * 3)) else
                "ready" if ready else "stopped" if self._stop.is_set() else "starting",
                "queueDepth": self._q.qsize(), "queueCapacity": self._q.maxsize,
                "jobAgeSeconds": round(job_age, 3), "lastProgressAgeSeconds": round(age, 3),
                "pollFailures": failures, "threadAlive": alive,
                "oldestQueuedAgeSeconds": round(queued_age, 3),
                "lastPollDurationMilliseconds": round(poll_duration, 3),
                "lastGcDurationMilliseconds": round(gc_duration, 3),
                "startupError": str(self._init_error) if self._init_error else ""}

    def submit_device_change(self, write: Job) -> concurrent.futures.Future:
        def observe(backend):
            try:
                write(backend)
                self._devices = backend.list_devices()
            except Exception as exc:
                self._devices = None
                raise ObservationUncertain(str(exc)) from exc
            self._last_discovery = time.monotonic()
            self._owner_seq += 1
            return {"ownerSeq": self._owner_seq,
                    "outputs": [asdict(item) for item in self._devices.outputs],
                    "inputs": [asdict(item) for item in self._devices.inputs]}
        return self.submit(observe)

    def _progress(self, *, started: bool = False, failed_poll: bool | None = None) -> None:
        with self._progress_lock:
            self._last_progress = time.monotonic()
            self._job_started = self._last_progress if started else None
            if failed_poll is not None:
                self._poll_failures = self._poll_failures + 1 if failed_poll else 0

    def _fail_pending(self, error: BaseException) -> None:
        while True:
            try:
                _fn, fut, _admitted_at = self._q.get_nowait()
            except queue.Empty:
                return
            if fut.set_running_or_notify_cancel():
                fut.set_exception(error)

    # ---- thread body ------------------------------------------------------
    def _run(self) -> None:
        try:
            backend = self._backend_factory()
            backend.setup()  # backend owns any COM init, on this thread
        except BaseException as exc:  # noqa: BLE001 - surface init failure to start()
            self._init_error = _detach_owner_exception(exc)
            with self._submission_lock:
                self._stop.set()
            self._fail_pending(self._init_error)
            self._ready.set()
            log.error("audio engine failed to initialise: %s", str(self._init_error))
            return

        self._ready.set()
        log.info("audio engine started")
        last_poll = 0.0  # poll once promptly so the first client gets fresh state
        slice_s = min(0.1, self._poll_interval)

        # COM interface pointers created here must also be *released* here, on this
        # apartment's thread. After each unit of COM work we run gc.collect() on this
        # thread so any transient comtypes objects caught in a reference cycle are
        # freed in-apartment, not later on another thread (which would access-violate).
        # We do NOT disable GC globally: that leaks cycles process-wide and was found
        # to destabilise unrelated event loops. The real crash fix was correcting the
        # mic endpoint's reference ownership (see pycaw_backend._endpoint).
        try:
            while not self._stop.is_set():
                did_work = False
                try:
                    fn, fut, _admitted_at = self._q.get(timeout=slice_s)
                except queue.Empty:
                    fn = fut = None
                if fn is not None:
                    # An asyncio requester can disappear or time out while its
                    # COM job is queued. Skip canceled writes; setting a result
                    # on their Future used to kill this entire thread.
                    if fut.set_running_or_notify_cancel():
                        if self._stop.is_set():
                            fut.set_exception(RuntimeError("engine stopped"))
                            continue
                        did_work = True
                        self._progress(started=True)
                        try:
                            result = fn(backend)
                        except BaseException as exc:  # noqa: BLE001 - relay to caller
                            fut.set_exception(_detach_owner_exception(exc))
                        else:
                            fut.set_result(result)
                        finally:
                            # Future callbacks may run now; all observations already
                            # contain their owner sequence and plain immutable values.
                            self._progress()

                now = time.monotonic()
                if now - last_poll >= self._poll_interval:
                    last_poll = now
                    did_work = True
                    self._progress(started=True)
                    poll_started = time.perf_counter()
                    try:
                        self._on_poll(self._gather(backend))
                    except Exception:  # noqa: BLE001 - a bad poll must not kill the thread
                        self._progress(failed_poll=True)
                        log.exception("poll failed")
                    else:
                        self._progress(failed_poll=False)
                    finally:
                        with self._progress_lock:
                            self._last_poll_duration_ms = (time.perf_counter() - poll_started) * 1000

                if did_work:
                    # Free transient COM objects now, on this thread, in-apartment.
                    gc_started = time.perf_counter()
                    gc.collect()
                    with self._progress_lock:
                        self._last_gc_duration_ms = (time.perf_counter() - gc_started) * 1000
        finally:
            gc.collect()
            try:
                backend.teardown()  # backend owns any COM uninit, on this thread
            except Exception:  # noqa: BLE001
                log.exception("backend teardown failed")
            log.info("audio engine stopped")

    def _gather(self, backend: AudioBackend) -> dict[str, Any]:
        now = time.monotonic()
        if self._devices is None or now - self._last_discovery >= self._discovery_interval:
            self._devices = backend.list_devices()
            self._last_discovery = now
        data = {
            "sessions": backend.snapshot_sessions(),
            "speaker": backend.get_master("speaker"),
            "mic": backend.get_master("mic"),
            "devices": self._devices,
            "meters": backend.get_peaks(),
        }
        self._owner_seq += 1
        data["ownerSeq"] = self._owner_seq
        return data
