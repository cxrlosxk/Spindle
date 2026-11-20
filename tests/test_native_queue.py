"""Regression suite: native insertion is only 'queued' after verification."""
from dataclasses import replace
from pathlib import Path
from spindle.catalog import norm
from spindle.commands import CommandEngine
from spindle.models import Track
from spindle.native_queue import NativeQueueBridge, Delivery, strict_track_match, observed_increment
from spindle.queue_store import QueueStore, QueueError
from spindle.settings import Settings
import pytest

SONG = Track('AMEN', 'Beyoncé', 'COWBOY CARTER', 200000, '123', 'https://music.apple.com/us/song/amen/123')


class FakeBackend:
    def __init__(self, verified=True):
        self.verified = verified
        self.received = []

    def deliver(self, track, first_request=False):
        self.received.append((track.title, track.artist, first_request))
        return Delivery(self.verified, 'Visible in Apple Music Playing Next' if self.verified else 'Not visible')


def test_exact_title_and_artist_required():
    assert strict_track_match('AMEN', 'Beyonce', SONG)
    assert not strict_track_match('AMEN (Live)', 'Beyoncé', SONG)
    assert not strict_track_match('AMEN', 'Another Artist', SONG)
    assert not strict_track_match('Amen (Karaoke)', 'Beyoncé', SONG)
    assert observed_increment([], [('AMEN','Beyoncé')], SONG)
    assert not observed_increment([('AMEN','Beyoncé')], [('AMEN','Beyoncé')], SONG)
    assert not observed_increment([], [('AMEN','Other Artist')], SONG)


def test_verified_native_add_is_queued_persisted_and_not_duplicated(tmp_path):
    store = QueueStore(tmp_path/'requests.db')
    backend = FakeBackend()
    bridge = NativeQueueBridge(lambda: backend, timeout=3)
    cfg = Settings(cooldown_seconds=0, native_queue_enabled=True)
    notes = []
    engine = CommandEngine(store, type('CatalogStub', (), {'best': lambda self, q: SONG})(),
                           cfg, native_queue=bridge, on_status=notes.append)
    response = engine.handle('viewer-1','fan','!sr AMEN Beyonce')
    assert 'Added to Apple Music Up Next' in response
    assert backend.received == [('AMEN', 'Beyoncé', True)]
    assert len(store.queued()) == 1
    assert len(store.pending()) == 0
    assert '1 verified' in engine.handle('viewer-2','fan2','!queue')
    assert 'already' in engine.handle('viewer-2','fan2','!sr AMEN Beyonce')
    store.close()
    again = QueueStore(tmp_path/'requests.db')
    assert len(again.queued()) == 1
    again.close()


def test_unverified_native_add_stays_pending_and_chat_is_truthful(tmp_path):
    store = QueueStore(tmp_path/'requests.db')
    backend = FakeBackend(verified=False)
    bridge = NativeQueueBridge(lambda: backend, timeout=3)
    cfg = Settings(cooldown_seconds=0, native_queue_enabled=True)
    notes = []
    engine = CommandEngine(store, type('CatalogStub', (), {'best': lambda self,q: SONG})(),
                           cfg, native_queue=bridge, on_status=notes.append)
    response = engine.handle('viewer-1', 'fan', '!sr AMEN Beyonce')
    assert 'Request saved in Spindle' in response
    assert 'Could not verify' in response
    assert 'Added to Apple Music' not in response
    assert len(store.pending()) == 1
    assert len(store.queued()) == 0
    assert any('Native queue could not be verified' in x for x in notes)
    store.close()


def test_disabled_native_setting_never_mutates_apple_music(tmp_path):
    store = QueueStore(tmp_path/'requests.db')
    backend = FakeBackend()
    bridge = NativeQueueBridge(lambda: backend)
    engine = CommandEngine(store, type('CatalogStub',(),{'best':lambda self,q: SONG})(),
                           Settings(cooldown_seconds=0, native_queue_enabled=False),native_queue=bridge)
    assert 'Not yet in Apple Music' in engine.handle('1','fan','!sr AMEN Beyonce')
    assert backend.received == []
    store.close()


def test_native_track_completes_only_after_playback_transition(tmp_path):
    store = QueueStore(tmp_path/'requests.db')
    rid, _ = store.add(SONG, '1','fan',cooldown_seconds=0)
    store.mark_queued(rid)
    engine = CommandEngine(store,object(),Settings())
    engine.set_current(SONG)  # Startup already playing same title: do not auto-complete
    assert len(store.queued()) == 1
    engine.set_current(Track('Yesterday','The Beatles',playing=True))
    engine.set_current(replace(SONG, playing=True))
    assert not store.queued()
    store.close()


def test_native_queue_setting_can_be_loaded(tmp_path):
    s=Settings(native_queue_enabled=True)
    s.save(tmp_path)
    assert Settings.load(tmp_path).native_queue_enabled


def test_bridge_worker_reuses_one_backend():
    made=[]
    def factory():
        backend=FakeBackend()
        made.append(backend)
        return backend
    bridge=NativeQueueBridge(factory, timeout=3)
    assert bridge.enqueue(SONG).verified
    assert bridge.enqueue(replace(SONG,title='Halo')).verified
    assert len(made) == 1
    assert len(made[0].received) == 2
