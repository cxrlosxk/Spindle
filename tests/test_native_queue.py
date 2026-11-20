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

    adapter._all = lambda root, **kw: (
        [box] if kw.get('aid') == 'TextBox' and state['opened'] else
        [] if kw.get('aid') == 'QueryButton' else
        [icon] if kw.get('kind') == 'button' else []
    )
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
    adapter._usable_result = lambda elem: True
    adapter._scroll_song_section = lambda elem: False
    # This fixture models a real query submission and a replaced results
    # page, rather than Apple's auto-suggestion dropdown.
    adapter._submit_search = lambda window: 'mock-full-search'
    adapter._search_page_state = lambda window: (
        ('new-rows', 'Results for ' + state['query'], (('AMEN', 'Beyoncé'),))
        if state['query'] else (None, '', ())
    )
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
    with pytest.raises(NativeQueueError, match='compact Search'):
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
    old_all = adapter._all
    adapter._all = lambda root, **kw: (
        [] if kw.get('aid') == 'TextBox' else old_all(root, **kw)
    )
    with pytest.raises(NativeQueueError, match='brief-focus'):
        adapter._open_compact_search(object())
    assert state['activated'] == 1


def test_control_wrapper_handles_custom_winui_control_type():
    """The third-party wrapper can return None for newer WinUI controls."""
    from spindle.native_queue import WindowsQueueUIA
    from types import SimpleNamespace
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    element = object()
    calls = []
    class GenericControl:
        @staticmethod
        def CreateControlFromElement(e):
            calls.append(('factory',e))
            return None
        def __init__(self, **kwargs):
            calls.append(('generic',kwargs['element']))
    adapter.auto = SimpleNamespace(Control=GenericControl)
    result = adapter._control(element)
    assert isinstance(result, GenericControl)
    assert calls == [('factory', element), ('generic', element)]


def test_control_wrapper_missing_element_is_actionable_error():
    from spindle.native_queue import WindowsQueueUIA, NativeQueueError
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    with pytest.raises(NativeQueueError, match='was not found'):
        adapter._control(None)


def test_hidden_search_field_is_ignored_until_sidebar_is_open():
    from spindle.native_queue import WindowsQueueUIA
    from types import SimpleNamespace
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter.types = SimpleNamespace(EditControl='edit')
    hidden = SimpleNamespace(
        CurrentIsOffscreen=True,
        CurrentBoundingRectangle=SimpleNamespace(left=0,right=200,top=0,bottom=20),
    )
    shown = SimpleNamespace(
        CurrentIsOffscreen=False,
        CurrentBoundingRectangle=SimpleNamespace(left=0,right=200,top=0,bottom=20),
    )
    adapter._all = lambda root, **kw: [hidden]
    assert adapter._visible_search_field(object()) is None
    adapter._all = lambda root, **kw: [hidden, shown]
    assert adapter._visible_search_field(object()) is shown


def test_winui_queue_menu_in_separate_same_process_popup():
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter.types = SimpleNamespace(MenuItemControl='menuitem')
    main = SimpleNamespace(CurrentProcessId=42, CurrentIsOffscreen=False)
    own_popup = SimpleNamespace(CurrentProcessId=42, CurrentIsOffscreen=False)
    other_app = SimpleNamespace(CurrentProcessId=7, CurrentIsOffscreen=False)
    safe = SimpleNamespace(CurrentName='Play Next', CurrentIsOffscreen=False)
    unsafe = SimpleNamespace(CurrentName='Play Next', CurrentIsOffscreen=False)
    desktop = object()
    adapter.api = SimpleNamespace(GetRootElement=lambda: desktop)
    def all_items(root, *, kind=None, scope=4):
        if root is desktop:
            return [main, own_popup, other_app]
        if root is own_popup:
            return [safe]
        if root is other_app:
            return [unsafe]
        return []
    adapter._all = all_items
    adapter._wait = lambda fn, *args, **kwargs: fn()
    invoked = []
    adapter._invoke = lambda item: invoked.append(item)
    adapter._select_queue_action(main, first_request=True)
    assert invoked == [safe]


def test_menu_refuses_play_now_and_ambiguous_queue_actions():
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA, NativeQueueError
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter._wait = lambda fn, *args, **kwargs: fn()
    adapter._visible_menu_items = lambda window: [
        SimpleNamespace(CurrentName='Play', CurrentIsOffscreen=False),
        SimpleNamespace(CurrentName='Play Now', CurrentIsOffscreen=False),
    ]
    adapter._invoke = lambda item: pytest.fail('Must never start playback now')
    with pytest.raises(NativeQueueError, match='No unambiguous Play Next'):
        adapter._select_queue_action(object(), first_request=True)
    candidate = SimpleNamespace(CurrentName='Play Next', CurrentIsOffscreen=False)
    adapter._visible_menu_items = lambda window: [candidate, candidate]
    with pytest.raises(NativeQueueError, match='No unambiguous'):
        adapter._select_queue_action(object(), first_request=True)


def test_invoke_uses_winui_legacy_default_action_when_invoke_missing():
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    invoked = []
    class Pattern:
        def DoDefaultAction(self, **kwargs):
            invoked.append('legacy')
    control = SimpleNamespace(
        GetInvokePattern=lambda: None,
        GetLegacyIAccessiblePattern=lambda: Pattern(),
    )
    adapter._control = lambda element: control
    adapter._invoke(object())
    assert invoked == ['legacy']


def test_invoke_never_uses_mouse_or_keyboard_on_missing_patterns():
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA, NativeQueueError
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    control = SimpleNamespace(
        GetInvokePattern=lambda: None,
        GetLegacyIAccessiblePattern=lambda: None,
    )
    adapter._control = lambda element: control
    with pytest.raises(NativeQueueError, match='no usable Invoke or Legacy'):
        adapter._invoke(object())


def test_search_text_uses_legacy_pattern_when_value_is_unavailable():
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    saved = []
    legacy = SimpleNamespace(SetValue=lambda text, **kwargs: saved.append(text))
    control = SimpleNamespace(
        GetValuePattern=lambda: None,
        GetLegacyIAccessiblePattern=lambda: legacy,
    )
    adapter._control = lambda e: control
    adapter._set_search_text(object(), 'Fantasy Mariah Carey')
    assert saved == ['Fantasy Mariah Carey']


def test_stale_submit_button_is_ignored_before_invoking():
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter.types = SimpleNamespace(ButtonControl='button')
    hidden = SimpleNamespace(CurrentIsOffscreen=True)
    good = SimpleNamespace(
        CurrentIsOffscreen=False,
        CurrentBoundingRectangle=SimpleNamespace(left=-1600, right=-1540, top=30, bottom=70),
    )
    adapter._all = lambda root, **kw: [hidden, good]
    assert adapter._visible_action_button(object(), 'QueryButton') is good
    adapter._all = lambda root, **kw: [hidden]
    assert adapter._visible_action_button(object(), 'QueryButton') is None


def test_search_filters_virtualized_zero_size_rows():
    """Observed diagnostics contained many ListViewItem rows at (0,0,0,0)."""
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    stale = SimpleNamespace(
        CurrentIsOffscreen=False,
        CurrentBoundingRectangle=SimpleNamespace(
            left=0, top=0, right=0, bottom=0,
        ),
    )
    visible = SimpleNamespace(
        CurrentIsOffscreen=False,
        CurrentBoundingRectangle=SimpleNamespace(
            left=-3500, top=-300, right=-3100, bottom=-250,
        ),
    )
    hidden = SimpleNamespace(CurrentIsOffscreen=True)
    assert not adapter._usable_result(stale)
    assert not adapter._usable_result(hidden)
    assert adapter._usable_result(visible)


def test_duplicate_search_ignores_hidden_rows_and_selects_visible_match():
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    stale = SimpleNamespace(
        CurrentIsOffscreen=False,
        CurrentBoundingRectangle=SimpleNamespace(left=0, top=0, right=0, bottom=0),
    )
    active = SimpleNamespace(
        CurrentIsOffscreen=False,
        CurrentBoundingRectangle=SimpleNamespace(left=-400, top=20, right=-100, bottom=60),
    )
    adapter._search_results = lambda window, track: [stale, active]
    adapter._row_texts = lambda row: ['9 to 5', 'Dolly Parton']
    selected = adapter._target_row(object(), Track('9 to 5', 'Dolly Parton', '9 to 5 and Odd Jobs'))
    assert selected is active
    assert adapter._selection_note == ''


def test_exact_album_match_wins_when_multiple_live_results():
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    first = SimpleNamespace(
        CurrentIsOffscreen=False,
        CurrentBoundingRectangle=SimpleNamespace(left=0, top=0, right=100, bottom=30),
    )
    second = SimpleNamespace(
        CurrentIsOffscreen=False,
        CurrentBoundingRectangle=SimpleNamespace(left=0, top=32, right=100, bottom=65),
    )
    adapter._search_results = lambda window, track: [first, second]
    adapter._row_texts = lambda row: (
        ['Forever', 'Mariah Carey', 'Greatest Hits'] if row is first
        else ['Forever', 'Mariah Carey', 'Daydream']
    )
    selected = adapter._target_row(object(), Track('Forever', 'Mariah Carey', 'Daydream'))
    assert selected is second
    assert adapter._selection_note == ''


def test_same_artist_title_multiple_editions_produces_candid_note():
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    def row(top):
        return SimpleNamespace(
            CurrentIsOffscreen=False,
            CurrentBoundingRectangle=SimpleNamespace(left=0, top=top, right=100, bottom=top+30),
        )
    first, second = row(20), row(60)
    adapter._search_results = lambda window, track: [first, second]
    adapter._row_texts = lambda row: ['Forever', 'Mariah Carey']
    assert adapter._target_row(object(), Track('Forever', 'Mariah Carey', 'Daydream')) is first
    assert 'album or recording edition' in adapter._selection_note


def test_more_requires_named_visible_song_button():
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA, NativeQueueError
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter.types = SimpleNamespace(ButtonControl='button')
    other = SimpleNamespace(CurrentName='Play', CurrentAutomationId='PlayButton')
    adapter._all = lambda root, **kwargs: [other]
    adapter._usable_result = lambda control: True
    adapter._invoke = lambda control: pytest.fail('Would invoke arbitrary button')
    with pytest.raises(NativeQueueError, match='song-specific More'):
        adapter._open_more(object())



def test_song_more_can_be_invoked_by_name_even_before_hover():
    """WinUI can hide More's bounds until the pointer hovers over the song."""
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter.types = SimpleNamespace(ButtonControl='button')
    icon = SimpleNamespace(
        CurrentName='More', CurrentAutomationId='',
        CurrentIsOffscreen=False,
        CurrentBoundingRectangle=SimpleNamespace(left=0, top=0, right=0, bottom=0),
    )
    adapter._all = lambda root, **kwargs: [icon]
    called = []
    adapter._invoke = lambda elem: called.append(elem)
    adapter._open_more(object())
    assert called == [icon]


def test_compact_search_uses_observed_autosuggestbox_when_icon_unlabeled():
    """Real diagnostic: AutoSuggestBox is a GroupControl with InvokePattern."""
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter.types = SimpleNamespace(
        ButtonControl='button', ListItemControl='list', TabItemControl='tab',
        GroupControl='group',
    )
    auto = SimpleNamespace(
        CurrentName='', CurrentAutomationId='', CurrentClassName='AutoSuggestBox',
        CurrentControlType='group', CurrentIsOffscreen=False,
        CurrentBoundingRectangle=SimpleNamespace(
            left=-3826, top=-566, right=-3418, bottom=51,
        ),
    )
    adapter._all = lambda window, **kw: [auto] if kw.get('kind') == 'group' else []
    results = adapter._search_entry_candidates(object())
    assert results == [auto]
    state = {'opened': False}
    class Control:
        def GetInvokePattern(self): return self
        def Invoke(self, **kw): state['opened'] = True
    adapter._control = lambda elem: Control()
    box = object()
    adapter._visible_search_field = lambda window: box if state['opened'] else None
    adapter._wait = lambda fn, *args, **kw: fn()
    assert adapter._open_compact_search(object()) is box
    assert state['opened']


def test_exact_query_button_is_invoked_even_when_visual_bounds_hidden():
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    button = SimpleNamespace(CurrentAutomationId='QueryButton')
    adapter._all = lambda window, **kw: (
        [button] if kw.get('aid') == 'QueryButton' else []
    )
    called = []
    adapter._invoke = lambda item: called.append(item)
    adapter._post_music_return = lambda: pytest.fail('No background Enter needed')
    assert adapter._submit_search(object()) == 'query-button'
    assert called == [button]


def test_stale_page_from_previous_search_must_not_queue_a_song():
    from spindle.native_queue import WindowsQueueUIA, NativeQueueError
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter._visible_search_field = lambda window: object()
    adapter._search_page_state = lambda window: (
        (1, 9), 'Previous search result', (('9 to 5', 'Dolly Parton'),),
    )
    adapter._set_search_text = lambda search, query: None
    adapter._submit_search = lambda window: 'query-button'
    adapter._wait = lambda fn, *args, **kwargs: fn()
    adapter.allow_focus_handoff = False
    adapter._focused_return_only_if_explicitly_allowed = lambda search: False
    with pytest.raises(NativeQueueError, match='newly submitted full search-results'):
        adapter._search_results(object(), Track('9 to 5', 'Dolly Parton'))


def test_focus_handoff_refuses_to_send_key_if_music_cannot_take_foreground():
    from spindle.native_queue import WindowsQueueUIA
    from types import SimpleNamespace
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter.allow_focus_handoff = True
    adapter._hwnd = 123
    events = []
    foreground = 99
    adapter.user32 = SimpleNamespace(
        IsWindow=lambda h: True,
        GetForegroundWindow=lambda: foreground,
        SetForegroundWindow=lambda h: events.append(('focus', h)) or False,
        keybd_event=lambda *args: pytest.fail('Must never inject Enter into a game'),
    )
    adapter._wait = lambda fn, *a, **kw: fn()
    assert not adapter._focused_return_only_if_explicitly_allowed(object())
    assert events == [('focus', 123), ('focus', 99)]


def test_focus_handoff_is_opt_in_even_when_apple_music_is_visible():
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter.allow_focus_handoff = False
    assert not adapter._focused_return_only_if_explicitly_allowed(object())


def test_first_song_uses_add_to_queue_if_play_next_is_missing():
    from spindle.native_queue import WindowsQueueUIA
    from types import SimpleNamespace
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter._wait = lambda fn, *args, **kwargs: fn()
    item = SimpleNamespace(CurrentName='Add to Queue')
    adapter._visible_menu_items = lambda window: [item]
    invoked = []
    adapter._invoke = lambda control: invoked.append(control)
    adapter._select_queue_action(object(), first_request=True)
    assert invoked == [item]


def test_offered_play_next_is_preferred_over_add_to_queue():
    from spindle.native_queue import WindowsQueueUIA
    from types import SimpleNamespace
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter._wait = lambda fn, *args, **kwargs: fn()
    add = SimpleNamespace(CurrentName='Add to Queue')
    next_song = SimpleNamespace(CurrentName='Play Next')
    adapter._visible_menu_items = lambda window: [add, next_song]
    invoked = []
    adapter._invoke = lambda control: invoked.append(control)
    adapter._select_queue_action(object(), first_request=True)
    assert invoked == [next_song]


def test_apple_music_search_access_key_sequence_never_hits_foreground_game():
    """Apple documents Alt, N, F; no keys may be sent unless Music has focus."""
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter.allow_focus_handoff = True
    adapter._hwnd = 123
    state = {'focused': 998, 'keys': []}
    def set_focus(hwnd):
        state['focused'] = hwnd
        return True
    adapter.user32 = SimpleNamespace(
        IsWindow=lambda hwnd: True,
        GetForegroundWindow=lambda: state['focused'],
        SetForegroundWindow=set_focus,
        keybd_event=lambda vk, scan, flags, extra: state['keys'].append((vk, flags)),
    )
    adapter._wait = lambda fn, *args, **kw: fn()
    assert adapter._focused_open_search() is True
    assert state['keys'] == [
        (0x12, 0), (0x12, 2), (0x4E, 0),
        (0x4E, 2), (0x46, 0), (0x46, 2),
    ]
    assert state['focused'] == 998


def test_search_access_keys_refuse_to_type_if_foreground_stays_on_game():
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter.allow_focus_handoff = True
    adapter._hwnd = 123
    adapter.user32 = SimpleNamespace(
        IsWindow=lambda h: True, GetForegroundWindow=lambda: 999,
        SetForegroundWindow=lambda h: False,
        keybd_event=lambda *args: pytest.fail('Never inject keys into game'),
    )
    adapter._wait = lambda fn, *args, **kw: fn()
    assert adapter._focused_open_search() is False


def test_retried_song_already_in_native_queue_is_never_added_again():
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter._find_window = lambda: object()
    adapter._queue_snapshot = lambda window: [('needy', 'Ariana Grande')]
    adapter._target_row = lambda *args: pytest.fail('Do not duplicate queued song')
    adapter._finish_window = lambda: None
    delivered = adapter.deliver(Track('needy', 'Ariana Grande'), True)
    assert delivered.verified
    assert 'no duplicate' in delivered.reason


def test_read_only_queue_lookup_uses_serial_native_bridge():
    from spindle.native_queue import NativeQueueBridge, Delivery
    class Backend:
        def deliver(self, track, first_request=False):
            pytest.fail('Verification must not insert a song')
        def verify_present(self, track):
            return Delivery(track.title == 'AMEN', 'present')
    bridge = NativeQueueBridge(lambda: Backend(), timeout=3)
    result = bridge.check_present(SONG)
    assert result.verified
    assert result.reason == 'present'


def test_temporarily_restored_music_is_reminimized_without_activation():
    from spindle.native_queue import WindowsQueueUIA
    from types import SimpleNamespace
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    calls = []
    adapter.user32 = SimpleNamespace(
        IsWindow=lambda hwnd: True,
        ShowWindowAsync=lambda hwnd, state: calls.append((hwnd, state)),
    )
    adapter._temporarily_restored_hwnd = 9001
    adapter._finish_window()
    assert calls == [(9001, 7)]
    assert adapter._temporarily_restored_hwnd is None


def test_album_qualified_full_search_finds_song_omitted_from_first_four():
    """Automatic query refinement must find songs absent from top suggestions."""
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    first = SimpleNamespace(
        CurrentIsOffscreen=False,
        CurrentBoundingRectangle=SimpleNamespace(left=0, top=20, right=250, bottom=60),
    )
    desired = SimpleNamespace(
        CurrentIsOffscreen=False,
        CurrentBoundingRectangle=SimpleNamespace(left=0, top=80, right=250, bottom=120),
    )
    calls = []
    def search(window, track, query_override=None):
        calls.append(query_override)
        return [first] if not query_override else [desired]
    adapter._search_results = search
    adapter._row_texts = lambda row: (
        ['Forever', 'Babyface', 'Greatest Hits'] if row is first
        else ['Forever', 'Mariah Carey', 'Daydream']
    )
    adapter._wait = lambda callback, *args, **kwargs: []
    adapter._open_all_songs = lambda window: False
    track = Track('Forever', 'Mariah Carey', 'Daydream')
    assert adapter._target_row(object(), track) is desired
    assert calls == [None, 'Forever Mariah Carey Daydream']


def test_minimized_apple_music_can_be_restored_without_activating_window():
    import sys
    if sys.platform != 'win32':
        pytest.skip('Windows UI Automation uses ctypes.WINFUNCTYPE')
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace
    from spindle.native_queue import WindowsQueueUIA
    adapter = WindowsQueueUIA.__new__(WindowsQueueUIA)
    adapter.ctypes = ctypes
    adapter.wintypes = wintypes
    state = {'minimized': True, 'show_commands': []}
    class FakeUser32:
        def EnumWindows(self, fn, unused):
            fn(4321, 0)
        def IsWindowVisible(self, hwnd):
            return True
        def GetWindowTextW(self, hwnd, out, capacity):
            out.value = 'Apple Music'
        def GetClassNameW(self, hwnd, out, capacity):
            out.value = 'WinUIDesktopWin32WindowClass'
        def IsIconic(self, hwnd):
            return state['minimized']
        def ShowWindowAsync(self, hwnd, code):
            state['show_commands'].append(code)
            state['minimized'] = code == 7
            return True
        def IsWindow(self, hwnd):
            return True
    adapter.user32 = FakeUser32()
    window = object()
    adapter.api = SimpleNamespace(ElementFromHandle=lambda hwnd: window)
    adapter._wait = lambda cb, *args, **kwargs: cb()
    assert adapter._find_window() is window
    assert state['show_commands'] == [4]
    assert not state['minimized']
    adapter._finish_window()
    assert state['show_commands'] == [4, 7]
    assert state['minimized']
