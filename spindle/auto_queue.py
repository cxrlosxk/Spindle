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

    @staticmethod
    def _track(request):
        return Track(
            request.title, request.artist, request.album,
            request.duration_ms, request.track_id, request.url,
        )

    def _backoff(self, request_id, minimum=None):
        count = self.failure_count.get(request_id, 0) + 1
        self.failure_count[request_id] = count
        seconds = min(
            self.max_retry_seconds,
            self.retry_seconds * (2 ** min(count - 1, 4)),
        )
        if minimum is not None:
            seconds = max(seconds, minimum)
        self.next_retry[request_id] = time.monotonic() + seconds
        return seconds

    def _quiet_status(self, request_id, reason, message):
        reason = str(reason)[:180]
        if self._last_error.get(request_id) != reason:
            self.on_status(message + ' ' + reason)
            self._last_error[request_id] = reason

    def _reconcile(self, request):
        """No further Add actions until a possibly successful request is resolved."""
        request_id = request.id
        deadline = self.next_retry.get(request_id, 0)
        if time.monotonic() < deadline:
            self._wait(min(deadline - time.monotonic(), 3))
            return
        if not callable(getattr(self.native_queue, 'check_present', None)):
            self._quiet_status(
                request_id, 'Read-only verification unavailable.',
                f'Request #{request_id} is awaiting verification; no duplicate '
                'will be inserted.',
            )
            self._backoff(request_id, minimum=30)
            self._wait(3)
            return
        try:
            result = self.native_queue.check_present(self._track(request))
        except Exception as exc:
            from .native_queue import Delivery
            result = Delivery(False, type(exc).__name__)
        if result.verified:
            if self.store.mark_queued(request_id):
                self.on_status(
                    f'Apple Music Playing Next verified request #{request_id}: '
                    f'{request.title} — {request.artist}'
                )
                self.on_change()
            self.next_retry.pop(request_id, None)
            self.failure_count.pop(request_id, None)
            self._last_error.pop(request_id, None)
            self.wake()
            return
        self._quiet_status(
            request_id, result.reason,
            f'Request #{request_id} may already be in Apple Music. '
            'Checking only; Spindle will not automatically add it twice.',
        )
        self._backoff(request_id, minimum=30)
        self._wait(3)

    def _deliver_one(self):
        if not self.is_enabled():
            self._wait(2)
            return

        # Durable state survives restarts, crashes and external UIA timeouts.
        # An ambiguous insertion is NEVER retried as a new Add action.
        verifying = self.store.verifying()
        if verifying:
            self._reconcile(verifying[0])
            return

        pending = self.store.pending()
        if not pending:
            self._wait(2)
            return

        # Preserve FIFO; never bypass an earlier request that's still pending.
        request = pending[0]
        request_id = request.id
        now = time.monotonic()
        deadline = self.next_retry.get(request_id, 0)
        if now < deadline:
            self._wait(min(deadline - now, 3))
            return

        # Persist BEFORE touching Apple Music. If the app exits mid-request,
        # the next process starts in verification-only mode.
        if not self.store.begin_delivery(request_id):
            self._wait(0.1)
            return
        self.on_change()
        track = self._track(request)
        try:
            delivery = self.native_queue.enqueue(
                track, first_request=not bool(self.store.queued()),
            )
        except Exception as exc:
            from .native_queue import Delivery
            # Unknown external failure: might have occurred after Add.
            delivery = Delivery(False, type(exc).__name__, uncertain=True)

        if delivery.verified:
            if self.store.mark_queued(request_id):
                self.on_status(
                    'Automatically added to Apple Music Up Next: '
                    + track.title + ' — ' + track.artist
                )
                self.on_change()
            self.next_retry.pop(request_id, None)
            self.failure_count.pop(request_id, None)
            self._last_error.pop(request_id, None)
            self.wake()
            return

        if delivery.uncertain:
            self._quiet_status(
                request_id, delivery.reason,
                f'Request #{request_id} may already be queued. '
                'Spindle will verify without repeating Add to Queue.',
            )
            self._backoff(request_id, minimum=30)
            self.on_change()
            self._wait(3)
            return

        # Proven pre-insertion failures (including foreground protection)
        # may safely return to Pending for another attempt later.
        released = self.store.release_unsubmitted(request_id)
        if released:
            self.on_change()
        self._quiet_status(
            request_id, delivery.reason,
            f'Automatic queue request #{request_id} pending; '
            'will retry without interrupting the active game.',
        )
        self._backoff(request_id)
        self._wait(3)
