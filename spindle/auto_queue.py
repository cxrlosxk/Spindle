"""Automatic, ordered Apple Music delivery worker.

Viewer !sr requests are accepted immediately and delivered asynchronously.
No per-request broadcaster approval is required. A failed attempt remains
pending and is retried with bounded backoff after Apple Music becomes usable.
The native adapter verifies Playing Next before any DB status changes.
"""
from __future__ import annotations

import threading
import time
from .models import Track


class AutoQueueService:
    def __init__(self, store, native_queue, is_enabled, on_status=None,
                 on_change=None, retry_seconds=8.0, max_retry_seconds=60.0):
        self.store = store
        self.native_queue = native_queue
        self.is_enabled = is_enabled
        self.on_status = on_status or (lambda message: None)
        self.on_change = on_change or (lambda: None)
        self.retry_seconds = retry_seconds
        self.max_retry_seconds = max_retry_seconds
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = None
        self.next_retry = {}
        self.failure_count = {}
        self._last_error = {}

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(
                target=self._run, name='SpindleAutoQueue', daemon=True,
            )
            self.thread.start()
        self.wake()

    def wake(self, request_id=None):
        """Handle new requests, settings changes, and player availability."""
        if request_id is not None:
            self.next_retry.pop(request_id, None)
            self.failure_count.pop(request_id, None)
        self.wake_event.set()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)

    def _wait(self, seconds):
        self.wake_event.wait(timeout=max(0.05, seconds))
        self.wake_event.clear()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self._deliver_one()
            except Exception as exc:
                self.on_status('Automatic queue worker error: ' + type(exc).__name__)
                self._wait(10)

    def _deliver_one(self):
        if not self.is_enabled():
            self._wait(2)
            return
        pending = self.store.pending()
        if not pending:
            self._wait(2)
            return

        # FIFO is deliberate. Never queue a later viewer ahead of a failed
        # earlier viewer just because an Apple Music window is loading.
        request = pending[0]
        now = time.monotonic()
        deadline = self.next_retry.get(request.id, 0)
        if now < deadline:
            self._wait(min(deadline - now, 3))
            return

        track = Track(
            request.title, request.artist, request.album,
            request.duration_ms, request.track_id, request.url,
        )
        try:
            delivery = self.native_queue.enqueue(
                track, first_request=not bool(self.store.queued()),
            )
        except Exception as exc:
            from .native_queue import Delivery
            delivery = Delivery(False, type(exc).__name__)

        # The user can remove a request while the worker is talking to Apple.
        # Never restore a deleted entry to the active queue.
        if delivery.verified:
            if self.store.mark_queued(request.id):
                self.on_status(
                    'Automatically added to Apple Music Up Next: '
                    + track.title + ' — ' + track.artist
                )
                self.on_change()
            self.next_retry.pop(request.id, None)
            self.failure_count.pop(request.id, None)
            self._last_error.pop(request.id, None)
            self.wake()
            return

        count = self.failure_count.get(request.id, 0) + 1
        self.failure_count[request.id] = count
        seconds = min(self.max_retry_seconds, self.retry_seconds * (2 ** min(count-1, 4)))
        self.next_retry[request.id] = time.monotonic() + seconds
        reason = str(delivery.reason)[:180]
        if self._last_error.get(request.id) != reason:
            self.on_status(
                f'Automatic queue request #{request.id} pending; '
                f'will retry. {reason}'
            )
            self._last_error[request.id] = reason
        self._wait(min(seconds, 3))
