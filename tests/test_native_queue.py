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


def test_windows_uia_bridge_initializes_without_apple_music():
    """CI smoke test for the real COM adapter; song insertion needs user's PC."""
    import sys
    if sys.platform != 'win32':
        pytest.skip('Windows UI Automation is not available on this host')
    from uiautomation import UIAutomationInitializerInThread
    from spindle.native_queue import WindowsQueueUIA
    with UIAutomationInitializerInThread():
        backend = WindowsQueueUIA()
        assert backend.api is not None


def _fake_queue_adapter(*, initial_open: bool, empty: bool, populated: bool = False):
    """Model Apple's accessible tree without depending on native Windows UIA."""
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter.types = SimpleNamespace(ButtonControl='button', TextControl='text')
    state = {'open': initial_open, 'clicks': 0}
    toggle = object()
    panel = object()
    row = object()
    adapter._find = lambda window, **criteria: (
        toggle if criteria.get('aid') == 'PlayQueueToggleButton' else None
    )
    adapter._queue_toggle_is_on = lambda button: state['open']
    adapter._press_queue_toggle = lambda button: state.update(
        open=not state['open'], clicks=state['clicks'] + 1
    )
    adapter._playing_next = lambda window: panel if state['open'] and populated else None
    adapter._empty_queue_visible = lambda window: bool(state['open'] and empty)
    adapter._rows = lambda lst: [row]
    adapter._row_texts = lambda item: ['needy', 'Ariana Grande']
    return adapter, state


def test_empty_playing_next_already_open_is_not_toggled_closed():
    """Regression for recorded 2026-11-20 failure: list missing, pane open."""
    adapter, state = _fake_queue_adapter(initial_open=True, empty=True)
    assert adapter._queue_snapshot(object()) == []
    assert state == {'open': True, 'clicks': 0}


def test_empty_playing_next_closed_is_read_then_restored():
    adapter, state = _fake_queue_adapter(initial_open=False, empty=True)
    assert adapter._queue_snapshot(object()) == []
    assert state == {'open': False, 'clicks': 2}


def test_populated_playing_next_reads_rows_without_toggling():
    adapter, state = _fake_queue_adapter(initial_open=True, empty=False, populated=True)
    assert adapter._queue_snapshot(object()) == [('needy', 'Ariana Grande')]
    assert state['clicks'] == 0


def test_unreadable_empty_queue_is_not_falsely_verified(monkeypatch):
    from spindle.native_queue import NativeQueueError
    adapter, state = _fake_queue_adapter(initial_open=True, empty=False)
    # Make the visibility timeout deterministic without a 4-second delay.
    adapter._wait = lambda *args, **kwargs: None
    with pytest.raises(NativeQueueError, match='did not expose'):
        adapter._queue_snapshot(object())
    assert state['clicks'] == 0


def test_native_empty_label_is_respected_when_present():
    """The 'No upcoming songs.' accessibility label should count as empty."""
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter.types = SimpleNamespace(TextControl='text')
    label = SimpleNamespace(CurrentIsOffscreen=False)
    adapter._find = lambda window, **kw: label if kw.get('name') == 'No upcoming songs.' else None
    assert adapter._empty_queue_visible(object()) is True
    label.CurrentIsOffscreen = True
    assert adapter._empty_queue_visible(object()) is False


def _compact_search_fixture():
    """In-memory WinUI accessibility fixture for a narrow Apple Music window."""
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter.types = SimpleNamespace(
        ButtonControl='button', ListItemControl='list', TabItemControl='tab',
        EditControl='edit', GroupControl='group',
    )
    state = {'opened': False, 'activated': 0, 'query': '', 'submitted': 0}
    icon = SimpleNamespace(
        CurrentName='Search', CurrentAutomationId='',
        CurrentIsOffscreen=False, CurrentControlType='button',
    )
    box = object()
    result_row = object()
    songs = SimpleNamespace(GetRuntimeId=lambda: 'new-rows')
    section = object()

    class SearchControl:
        def GetInvokePattern(self):
            return self

        def Invoke(self, **kwargs):
            state['opened'] = True
            state['activated'] += 1

    class SearchBoxControl:
        def GetValuePattern(self):
            return self

        def SetValue(self, value, **kwargs):
            state['query'] = value

    adapter._all = lambda root, **kw: [icon] if kw.get('kind') == 'button' else []
    def find(root, **kw):
        if kw.get('aid') == 'TextBox':
            return box if state['opened'] else None
        if kw.get('aid') == 'QueryButton':
            return None  # AutoSuggestBox that updates automatically.
        if kw.get('name') == 'Content':
            return section if state['query'] else None
        if kw.get('name') == 'Songs':
            return songs if state['query'] else None
        return None

    adapter._find = find
    adapter._control = lambda node: SearchControl() if node is icon else SearchBoxControl()
    adapter._wait = lambda fn, *args, **kw: fn()
    adapter._rows = lambda section: [result_row]
    return adapter, state, box, icon


def test_compact_search_icon_is_activated_and_query_runs():
    """Regression: the recorded compact Apple Music sidebar hides TextBox."""
    from spindle.native_queue import WindowsQueueUIA
    adapter, state, box, icon = _compact_search_fixture()
    rows = adapter._search_results(object(), SONG)
    assert rows
    assert state['opened'] is True
    assert state['activated'] == 1
    assert state['query'] == 'AMEN Beyoncé'


def test_expanded_search_does_not_reactivate_navigation():
    adapter, state, box, icon = _compact_search_fixture()
    state['opened'] = True
    assert adapter._search_results(object(), SONG)
    assert state['activated'] == 0


def test_compact_search_absent_is_actionable_and_safe():
    from spindle.native_queue import NativeQueueError
    adapter, state, box, icon = _compact_search_fixture()
    adapter._all = lambda root, **kw: []
    with pytest.raises(NativeQueueError, match='magnifying-glass'):
        adapter._open_compact_search(object())
    assert state['activated'] == 0


def test_compact_search_ambiguous_buttons_do_not_get_clicked():
    from spindle.native_queue import NativeQueueError
    from types import SimpleNamespace
    adapter, state, box, icon = _compact_search_fixture()
    second = SimpleNamespace(
        CurrentName='Search', CurrentAutomationId='',
        CurrentIsOffscreen=False, CurrentControlType='button',
    )
    adapter._all = lambda root, **kw: [icon, second] if kw.get('kind') == 'button' else []
    with pytest.raises(NativeQueueError, match='multiple Search controls'):
        adapter._open_compact_search(object())
    assert state['activated'] == 0


def test_compact_search_activation_without_field_reports_next_action():
    from spindle.native_queue import NativeQueueError
    adapter, state, box, icon = _compact_search_fixture()
    old_find = adapter._find
    adapter._find = lambda root, **kw: (
        None if kw.get('aid') == 'TextBox' else old_find(root, **kw)
    )
    with pytest.raises(NativeQueueError, match='Open Search manually'):
        adapter._open_compact_search(object())
    assert state['activated'] == 1
