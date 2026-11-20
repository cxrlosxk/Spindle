"""Regressions for fully automatic Twitch song request delivery."""
import threading
import time

from spindle.auto_queue import AutoQueueService
from spindle.commands import CommandEngine
from spindle.models import Track
from spindle.native_queue import Delivery
from spindle.queue_store import QueueStore
from spindle.settings import Settings


AMEN = Track('AMEN', 'Beyoncé', 'COWBOY CARTER', 180000, 'id-1', 'https://music.apple.com/us/song/amen/1')
NEEDY = Track('needy', 'Ariana Grande', 'thank u, next', 178000, 'id-2', 'https://music.apple.com/us/song/needy/2')


class Catalog:
    def best(self, text):
        return NEEDY if 'needy' in text.casefold() else AMEN


class Backend:
    def __init__(self, fail_first=0):
        self.calls = []
        self.fail_first = fail_first
        self.lock = threading.Lock()

    def enqueue(self, track, first_request=False):
        with self.lock:
            self.calls.append((track.title, first_request))
            if self.fail_first:
                self.fail_first -= 1
                return Delivery(False, 'Apple Music is temporarily unavailable')
            return Delivery(True, 'Verified in Apple Music Up Next.')


def until(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.01)
    return False


def test_viewers_do_not_approve_each_song_and_fifo_order_is_preserved(tmp_path):
    store = QueueStore(tmp_path / 'queue.db')
    backend = Backend()
    enabled = Settings(cooldown_seconds=0, native_queue_enabled=True)
    events = []
    worker = AutoQueueService(
        store, backend, lambda: enabled.native_queue_enabled,
        events.append, retry_seconds=.03, max_retry_seconds=.1,
    )
    engine = CommandEngine(store, Catalog(), enabled, native_queue=backend)
    engine.deferred_native_delivery = True
    engine.on_request_accepted = worker.wake
    worker.start()
    try:
        r1 = engine.handle('viewer1', 'fan', '!sr amen beyonce')
        r2 = engine.handle('viewer2', 'fan2', '!sr needy Ariana Grande')
        assert 'accepted' in r1 and 'automatically' in r1
        assert 'accepted' in r2
        assert until(lambda: len(store.queued()) == 2)
        assert backend.calls == [('AMEN', True), ('needy', False)]
        assert len(store.pending()) == 0
        assert any('Automatically added' in msg for msg in events)
    finally:
        worker.stop()
        store.close()


def test_transient_failure_retries_without_viewer_or_broadcaster_action(tmp_path):
    store = QueueStore(tmp_path / 'queue.db')
    backend = Backend(fail_first=1)
    settings = Settings(native_queue_enabled=True)
    worker = AutoQueueService(
        store, backend, lambda: settings.native_queue_enabled,
        retry_seconds=.02, max_retry_seconds=.1,
    )
    store.add(AMEN, 'viewer', 'fan', cooldown_seconds=0)
    worker.start()
    try:
        assert until(lambda: len(store.queued()) == 1)
        assert len(backend.calls) == 2
        assert all(first is True for _, first in backend.calls)
    finally:
        worker.stop()
        store.close()


def test_when_automatic_queue_is_off_requests_remain_pending(tmp_path):
    store = QueueStore(tmp_path / 'queue.db')
    backend = Backend()
    enabled = Settings(native_queue_enabled=False)
    worker = AutoQueueService(
        store, backend, lambda: enabled.native_queue_enabled,
        retry_seconds=.01,
    )
    store.add(AMEN, 'viewer', 'fan', cooldown_seconds=0)
    worker.start()
    try:
        time.sleep(.06)
        assert len(store.pending()) == 1
        assert backend.calls == []
        enabled.native_queue_enabled = True
        worker.wake()
        assert until(lambda: len(store.queued()) == 1)
    finally:
        worker.stop()
        store.close()


def test_pending_requests_survive_restart_and_continue_automatically(tmp_path):
    path = tmp_path / 'queue.db'
    first = QueueStore(path)
    first.add(AMEN, 'viewer', 'fan', cooldown_seconds=0)
    first.close()
    second = QueueStore(path)
    backend = Backend()
    worker = AutoQueueService(
        second, backend, lambda: True,
        retry_seconds=.01,
    )
    worker.start()
    try:
        assert until(lambda: len(second.queued()) == 1)
    finally:
        worker.stop()
        second.close()
