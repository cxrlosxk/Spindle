"""Regression coverage for the observed focus theft and duplicate queueing.

Windows accessibility patterns can activate WinUI even without a direct
SetForegroundWindow call. Game-safe mode must refuse to invoke them while
Chrome, Marvel Rivals, or any other app is foreground.
"""
from types import SimpleNamespace
import pytest

from spindle.models import Track
from spindle.native_queue import (
    WindowsQueueUIA, ForegroundProtectionError, Delivery,
)
from spindle.queue_store import QueueStore
from spindle.auto_queue import AutoQueueService

SONG = Track('needy', 'Ariana Grande', 'thank u, next',
             180_000, 'needy-id', 'https://music.apple.com/us/song/needy/1')


def protected_adapter(foreground=202, music=101):
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter.protect_foreground = True
    adapter.allow_focus_handoff = False
    adapter._hwnd = music
    adapter.user32 = SimpleNamespace(
        IsWindow=lambda hwnd: True,
        GetForegroundWindow=lambda: foreground,
    )
    return adapter


def test_game_safe_refuses_all_mutating_actions_while_chrome_is_foreground():
    adapter = protected_adapter()
    adapter._control = lambda *args: pytest.fail('No UIA control may be invoked')
    with pytest.raises(ForegroundProtectionError, match='protecting your active app'):
        adapter._invoke(object())
    with pytest.raises(ForegroundProtectionError):
        adapter._press_queue_toggle(object())
    with pytest.raises(ForegroundProtectionError):
        adapter._set_search_text(object(), 'needy Ariana Grande')
    with pytest.raises(ForegroundProtectionError):
        adapter._scroll_song_section(object())
    with pytest.raises(ForegroundProtectionError):
        adapter._open_compact_search(object())
    with pytest.raises(ForegroundProtectionError):
        adapter._post_music_return()


def test_game_safe_allows_controls_only_when_apple_music_already_foreground():
    adapter = protected_adapter(foreground=101, music=101)
    adapter._assert_focus_safe()  # Safe to operate; no focus transfer needed.


def test_find_window_never_restores_minimized_apple_music_over_game(monkeypatch):
    import sys
    if sys.platform != 'win32':
        pytest.skip('Windows-only HWND callback')
    import ctypes
    from ctypes import wintypes
    adapter = protected_adapter()
    adapter.ctypes = ctypes
    adapter.wintypes = wintypes
    seen = []
    class User32:
        def EnumWindows(self, callback, param):
            callback(101, 0)
        def IsWindowVisible(self, hwnd):
            return True
        def GetWindowTextW(self, hwnd, buff, size):
            buff.value = 'Apple Music'
        def GetClassNameW(self, hwnd, buff, size):
            buff.value = 'WinUIDesktopWin32WindowClass'
        def IsWindow(self, hwnd):
            return True
        def GetForegroundWindow(self):
            return 202  # Browser/game, not Apple Music.
        def IsIconic(self, hwnd):
            seen.append('is_iconic')
            return True
        def ShowWindowAsync(self, hwnd, state):
            pytest.fail('Game-safe mode MUST NOT restore Apple Music')
    adapter.user32 = User32()
    adapter.api = SimpleNamespace(
        ElementFromHandle=lambda hwnd: pytest.fail('No UIA use while gaming')
    )
    with pytest.raises(ForegroundProtectionError):
        adapter._find_window()
    assert seen == []


def test_queue_action_sent_but_verification_failed_is_uncertain():
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter._find_window = lambda: object()
    state = {'snapshots': 0, 'adds': 0}
    def snapshot(window):
        state['snapshots'] += 1
        if state['snapshots'] == 1:
            return []
        raise RuntimeError('Apple accessibility verification failed')
    adapter._queue_snapshot = snapshot
    adapter._target_row = lambda window, track: object()
    adapter._open_more = lambda row: None
    adapter._select_queue_action = lambda window, first_request: state.__setitem__(
        'adds', state['adds'] + 1,
    )
    adapter._capture_failed_queue = lambda window: ''
    adapter._finish_window = lambda: None
    delivery = adapter.deliver(SONG, True)
    assert not delivery.verified
    assert delivery.uncertain
    assert state['adds'] == 1


def test_crash_safe_verification_state_survives_restart(tmp_path):
    path = tmp_path / 'requests.db'
    store = QueueStore(path)
    request_id, _ = store.add(SONG, 'fan', 'viewer', cooldown_seconds=0)
    assert store.begin_delivery(request_id)
    store.close()
    reopened = QueueStore(path)
    try:
        assert len(reopened.verifying()) == 1
        assert reopened.pending() == []
        assert reopened.mark_queued(request_id)
        assert len(reopened.queued()) == 1
    finally:
        reopened.close()


def test_pre_action_failure_releases_request_for_safe_retry(tmp_path):
    store = QueueStore(tmp_path / 'requests.db')
    request_id, _ = store.add(SONG, 'fan', 'viewer', cooldown_seconds=0)
    try:
        assert store.begin_delivery(request_id)
        assert store.release_unsubmitted(request_id)
        assert store.pending()[0].id == request_id
    finally:
        store.close()


def test_uncertain_delivery_never_inserts_twice_even_after_retry_and_restart(tmp_path):
    path = tmp_path / 'requests.db'
    store = QueueStore(path)
    request_id, _ = store.add(SONG, 'fan', 'viewer', cooldown_seconds=0)
    state = {'inserts': 0, 'verified': False, 'reads': 0}
    class Native:
        def enqueue(self, track, first_request=False):
            state['inserts'] += 1
            return Delivery(False, 'Apple may already have accepted', uncertain=True)
        def check_present(self, track):
            state['reads'] += 1
            return Delivery(state['verified'], 'checked')

    native = Native()
    worker = AutoQueueService(store, native, lambda: True)
    worker._wait = lambda delay: None
    try:
        worker._deliver_one()
        assert state['inserts'] == 1
        assert len(store.verifying()) == 1
        for _ in range(3):
            worker.next_retry[request_id] = 0
            worker._deliver_one()
        assert state['inserts'] == 1
        assert state['reads'] == 3
    finally:
        store.close()

    restarted = QueueStore(path)
    new_worker = AutoQueueService(restarted, native, lambda: True)
    new_worker._wait = lambda delay: None
    state['verified'] = True
    try:
        new_worker._deliver_one()
        assert len(restarted.queued()) == 1
        assert state['inserts'] == 1
    finally:
        restarted.close()


def test_foreground_protection_prevents_delivery_without_verifying_state_lock(tmp_path):
    store = QueueStore(tmp_path / 'requests.db')
    request_id, _ = store.add(SONG, 'fan', 'viewer', cooldown_seconds=0)
    class Native:
        def enqueue(self, track, first_request=False):
            return Delivery(False, 'Game-safe mode is protecting Chrome')
    worker = AutoQueueService(store, Native(), lambda: True)
    worker._wait = lambda delay: None
    try:
        worker._deliver_one()
        assert store.verifying() == []
        assert len(store.pending()) == 1
    finally:
        store.close()
