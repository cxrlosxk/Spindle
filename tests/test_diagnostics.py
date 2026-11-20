"""Tests of the passive, private UI inspection workflow."""
from types import SimpleNamespace
from spindle.diagnostics import inspect_window, public_name, public_id
from spindle.native_queue import NativeQueueBridge


class FakeElement:
    def __init__(self, name='', aid='', kind=50000, children=(), offscreen=False,
                 left=0, top=0, right=500, bottom=450):
        self.CurrentName = name
        self.CurrentAutomationId = aid
        self.CurrentClassName = 'WinUI'
        self.CurrentControlType = kind
        self.CurrentIsOffscreen = offscreen
        self.CurrentBoundingRectangle = SimpleNamespace(
            left=left, top=top, right=right, bottom=bottom,
        )
        self.children = children

    def GetCurrentPattern(self, pattern_id):
        return object() if pattern_id in (10000, 10010) else None


class Adapter:
    def __init__(self):
        self.unnamed_icon = FakeElement('', 'NavigationSearchButton', left=-1300)
        self.field = FakeElement('', 'TextBox', kind=50004, left=-1290)
        self.track = FakeElement('Mariah Carey — Fantasy', 'track_8427', left=-800)
        self.sidebar = FakeElement('Navigation', 'SidebarRoot', children=[self.unnamed_icon, self.field])
        self.window = FakeElement('Apple Music', 'MainWindow', children=[self.sidebar, self.track])
        self.user32 = SimpleNamespace(GetSystemMetrics=lambda number: 2)

    def _find_window(self):
        return self.window

    def _all(self, element, *, scope):
        assert scope == 2
        return element.children


def test_redaction_keeps_ui_labels_but_not_library_content():
    assert public_name('Search') == 'Search'
    assert public_name('No upcoming songs.') == 'No upcoming songs.'
    assert public_name('Fantasy — Mariah Carey') == '[redacted]'
    assert public_name('Search: my private playlist') == '[redacted]'
    assert public_name('user@example.com') == '[redacted]'
    assert public_id('NavigationSearchButton') == 'NavigationSearchButton'
    assert public_id('alice@example.com') == '[redacted-id]'


def test_passive_inspection_reports_multiple_monitors_and_icon_geometry():
    adapter = Adapter()
    report = inspect_window(adapter)
    assert report['read_only']
    assert report['monitor_count'] == 2
    assert report['window']['bounds']['right'] == 500
    assert report['counts']['elements_visited'] == 5
    search = [row for row in report['controls']
              if row['automation_id'] == 'NavigationSearchButton']
    assert search
    assert search[0]['bounds']['left'] == -1300
    assert search[0]['patterns']['invoke']
    text_repr = repr(report)
    assert 'Mariah Carey' not in text_repr
    assert 'Fantasy' not in text_repr


def test_inspection_never_invokes_controls():
    adapter = Adapter()
    def forbidden(*args, **kwargs):
        raise AssertionError('Diagnostics must be read-only')
    adapter._invoke = forbidden
    adapter._control = forbidden
    assert inspect_window(adapter)['counts']['elements_visited'] == 5


def test_inspection_respects_element_budget():
    report = inspect_window(Adapter(), max_elements=2)
    assert report['counts']['max_elements_reached']
    assert report['counts']['elements_visited'] == 2


def test_bridge_can_run_diagnostics_on_same_serial_com_worker():
    class Backend:
        def deliver(self, track, first_request=False):
            from spindle.native_queue import Delivery
            return Delivery(True, 'ok')
        def inspect_diagnostics(self):
            return {'read_only': True, 'monitor_count': 2}
    bridge = NativeQueueBridge(lambda: Backend(), timeout=3)
    assert bridge.diagnose()['monitor_count'] == 2
    assert bridge.diagnose()['read_only']
