"""AudioEngine: a single dedicated thread that owns COM and serialises all audio.

Why a dedicated thread (BUILD-PLAN.md challenge 1): pycaw sits on Windows COM,
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
from typing import Any, Callable

from ..log import get_logger
from .backend import AudioBackend

log = get_logger("audio.engine")

# fn(backend) -> Any
Job = Callable[[AudioBackend], Any]
PollCallback = Callable[[dict[str, Any]], None]


class AudioEngine:
    def __init__(
        self,
        backend_factory: Callable[[], AudioBackend],
        poll_interval_s: float,
        on_poll: PollCallback | None = None,
    ) -> None:
        self._backend_factory = backend_factory
        self._poll_interval = poll_interval_s
        self._on_poll: PollCallback = on_poll or (lambda _data: None)
        self._q: "queue.Queue[tuple[Job, concurrent.futures.Future]]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._init_error: BaseException | None = None

    def set_on_poll(self, on_poll: PollCallback) -> None:
        """Set the poll callback before start() (controller is built after the engine)."""
        self._on_poll = on_poll

    # ---- lifecycle --------------------------------------------------------
    def start(self, wait: bool = True, timeout: float = 5.0) -> None:
        self._thread = threading.Thread(target=self._run, name="audio-engine", daemon=True)
        self._thread.start()
        if wait:
            self._ready.wait(timeout)
            if self._init_error is not None:
                raise self._init_error

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    # ---- command submission ----------------------------------------------
    def submit(self, fn: Job) -> concurrent.futures.Future:
        """Queue a job to run on the COM thread; returns a Future with the result."""
        fut: concurrent.futures.Future = concurrent.futures.Future()
        if self._stop.is_set():
            fut.set_exception(RuntimeError("engine stopped"))
            return fut
        self._q.put((fn, fut))
        return fut

    # ---- thread body ------------------------------------------------------
    def _run(self) -> None:
        try:
            backend = self._backend_factory()
            backend.setup()  # backend owns any COM init, on this thread
        except BaseException as exc:  # noqa: BLE001 - surface init failure to start()
            self._init_error = exc
            self._ready.set()
            log.exception("audio engine failed to initialise")
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
                    fn, fut = self._q.get(timeout=slice_s)
                except queue.Empty:
                    fn = fut = None
                if fn is not None:
                    did_work = True
                    try:
                        fut.set_result(fn(backend))
                    except BaseException as exc:  # noqa: BLE001 - relay to caller
                        fut.set_exception(exc)

                now = time.monotonic()
                if now - last_poll >= self._poll_interval:
                    last_poll = now
                    did_work = True
                    try:
                        self._on_poll(self._gather(backend))
                    except Exception:  # noqa: BLE001 - a bad poll must not kill the thread
                        log.exception("poll failed")

                if did_work:
                    # Free transient COM objects now, on this thread, in-apartment.
                    gc.collect()
        finally:
            gc.collect()
            try:
                backend.teardown()  # backend owns any COM uninit, on this thread
            except Exception:  # noqa: BLE001
                log.exception("backend teardown failed")
            log.info("audio engine stopped")

    @staticmethod
    def _gather(backend: AudioBackend) -> dict[str, Any]:
        return {
            "sessions": backend.snapshot_sessions(),
            "speaker": backend.get_master("speaker"),
            "mic": backend.get_master("mic"),
            "devices": backend.list_devices(),
            "meters": backend.get_peaks(),
        }
