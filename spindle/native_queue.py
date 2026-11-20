"""Experimental official Apple Music for Windows Up Next insertion.

Uses Windows UI Automation patterns, not simulated mouse/keyboard input. Queue
writes are considered successful *only* after a new matching row is observed
in the native Playing Next list. Apple exposes no supported Windows queue API;
this is an opt-in compatibility bridge and is not guaranteed on every build.

Apple Music Remote for Windows by ahmetberber (MIT) informed the control IDs and
UIA approach: https://github.com/ahmetberber/windows-apple-music-remote.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass
from concurrent.futures import Future, TimeoutError as FutureTimeout
from .catalog import norm
from .models import Track


class NativeQueueError(RuntimeError):
    pass


@dataclass(frozen=True)
class Delivery:
    verified: bool
    reason: str


def strict_track_match(title: str, artist: str, track: Track) -> bool:
    """No substitutions (karaoke, covers, live versions, same-titled songs)."""
    title = norm(title)
    artist = norm(artist)
    expected_title = norm(track.title)
    expected_artist = norm(track.artist)
    return bool(title and artist and title == expected_title and artist == expected_artist)


def observed_increment(before: list[tuple[str, str]],
                       after: list[tuple[str, str]], track: Track) -> bool:
    """Verify new occurrence, not merely that the track was already queued."""
    def count(items):
        return sum(strict_track_match(t, a, track) for t, a in items)
    return count(after) > count(before)


class WindowsQueueUIA:
    """COM-owning adapter. Every call stays on one dedicated worker thread."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        import uiautomation
        from uiautomation import uiautomation as _uia
        self.ctypes = ctypes
        self.wintypes = wintypes
        self.auto = uiautomation
        self.types = uiautomation.ControlType
        self.props = uiautomation.PropertyId
        self.api = _uia._AutomationClient.instance().IUIAutomation
        self.user32 = ctypes.windll.user32
        self._condition_all = self.api.CreateTrueCondition()
        self._hwnd = None

    def _find_window(self):
        ct = self.ctypes
        wt = self.wintypes
        matches = []
        u = self.user32
        cbtype = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def visit(hwnd, unused):
            if not u.IsWindowVisible(hwnd):
                return True
            title = ct.create_unicode_buffer(256)
            klass = ct.create_unicode_buffer(256)
            u.GetWindowTextW(hwnd, title, 256)
            u.GetClassNameW(hwnd, klass, 256)
            if 'Apple Music' in title.value and klass.value == 'WinUIDesktopWin32WindowClass':
                matches.append(hwnd)
            return True
        callback = cbtype(visit)
        u.EnumWindows(callback, 0)
        if not matches:
            raise NativeQueueError('Open the official Apple Music Windows app before enabling native queue sync.')
        hwnd = matches[0]
        if u.IsIconic(hwnd):
            raise NativeQueueError('Apple Music is minimized; restore its window before queueing.')
        return self.api.ElementFromHandle(hwnd)

    def _cond(self, *, aid=None, name=None, kind=None):
        parts = []
        for prop, value in ((self.props.AutomationIdProperty, aid),
                            (self.props.NameProperty, name),
                            (self.props.ControlTypeProperty, kind)):
            if value is not None:
                parts.append(self.api.CreatePropertyCondition(prop, value))
        if not parts:
            return self._condition_all
        result = parts[0]
        for part in parts[1:]:
            result = self.api.CreateAndCondition(result, part)
        return result

    def _find(self, root, *, aid=None, name=None, kind=None, scope=4):
        if root is None:
            return None
        return root.FindFirst(scope, self._cond(aid=aid, name=name, kind=kind))

    def _all(self, root, *, aid=None, name=None, kind=None, scope=4):
        if root is None:
            return []
        arr = root.FindAll(scope, self._cond(aid=aid, name=name, kind=kind))
        return [arr.GetElement(i) for i in range(arr.Length)] if arr else []

    def _control(self, element):
        return self.auto.Control.CreateControlFromElement(element)

    def _invoke(self, element):
        if element is None:
            raise NativeQueueError('Apple Music control not found.')
        pattern = self._control(element).GetInvokePattern()
        if not pattern:
            raise NativeQueueError('Apple Music control does not support accessibility Invoke.')
        pattern.Invoke(waitTime=0)

    @staticmethod
    def _wait(fn, seconds=3.0, every=0.12):
        limit = time.monotonic() + seconds
        while time.monotonic() < limit:
            val = fn()
            if val:
                return val
            time.sleep(every)
        return None

    def _rows(self, root):
        """Visible UIA rows; may not include virtualized entries off screen."""
        return self._all(root, kind=self.types.ListItemControl)

    def _row_texts(self, row):
        items = self._all(row, kind=self.types.TextControl)
        texts = []
        for item in items:
            name = (item.CurrentName or '').strip()
            if name and not all(0xE000 <= ord(c) <= 0xF8FF for c in name):
                texts.append(name)
        return texts

    def _playing_next(self, window):
        containers = self._all(window, kind=self.types.ListControl)
        for lst in containers:
            if (lst.CurrentAutomationId or '').startswith('PlayQueueListView'):
                return lst
        return None

    def _press_queue_toggle(self, element):
        control = self._control(element)
        # Apple Music sometimes exposes TogglePattern without InvokePattern.
        if control.GetPattern(10015):
            control.GetTogglePattern().Toggle(waitTime=0)
        else:
            self._invoke(element)

    def _queue_snapshot(self, window) -> list[tuple[str, str]]:
        toggle = self._find(window, aid='PlayQueueToggleButton', kind=self.types.ButtonControl)
        panel = self._playing_next(window)
        opened = False
        if panel is None:
            if toggle is None:
                raise NativeQueueError('Apple Music Playing Next button was not found.')
            self._press_queue_toggle(toggle)
            opened = True
            panel = self._wait(lambda: self._playing_next(window), 3)
            if panel is None:
                raise NativeQueueError('Apple Music Playing Next did not open.')
        try:
            results = []
            # The list can load asynchronously; take a second snapshot.
            time.sleep(0.18)
            for row in self._rows(panel):
                texts = self._row_texts(row)
                if len(texts) >= 2:
                    results.append((texts[0], texts[1]))
            return results
        finally:
            if opened:
                try:
                    self._press_queue_toggle(toggle)
                except Exception:
                    pass

    def _search_results(self, window, track: Track):
        search = self._find(window, aid='TextBox', kind=self.types.EditControl)
        submit = self._find(window, aid='QueryButton', kind=self.types.ButtonControl)
        if not search or not submit:
            raise NativeQueueError('Apple Music search UI is unavailable or has changed.')
        value = self._control(search).GetValuePattern()
        if not value:
            raise NativeQueueError('Apple Music search does not accept accessibility text input.')
        # SetValue/Invoke do not use the keyboard or mouse.
        value.SetValue(f'{track.title} {track.artist}', waitTime=0)
        self._invoke(submit)
        def find_rows():
            content = self._find(window, name='Content', kind=self.types.GroupControl)
            songs = self._find(content, name='Songs', kind=self.types.ListItemControl)
            if not songs:
                return None
            return self._rows(songs)
        rows = self._wait(find_rows, 8, 0.2)
        if rows is None:
            raise NativeQueueError('Apple Music search produced no Songs section.')
        return rows

    def _target_row(self, window, track):
        # Search may contain multiple recordings/editions. Do not assume first result.
        rows = self._search_results(window, track)
        matched = []
        for row in rows:
            texts = self._row_texts(row)
            if len(texts) >= 2 and strict_track_match(texts[0], texts[1], track):
                matched.append(row)
        if not matched:
            raise NativeQueueError('Apple Music search could not verify the exact title and artist; request kept in Spindle.')
        if len(matched) > 1:
            raise NativeQueueError('Multiple native search results match. Choose an Apple Music song manually to avoid the wrong edition.')
        return matched[0]

    def _open_more(self, row):
        buttons = self._all(row, kind=self.types.ButtonControl)
        if not buttons:
            raise NativeQueueError('Apple Music did not expose a More (…) button for this song.')
        named = [b for b in buttons if 'more' in (b.CurrentName or '').lower()]
        target = named[0] if named else buttons[-1]
        self._invoke(target)

    def _select_queue_action(self, window, first_request=False):
        """First Spindle request plays next; later requests append in order. Never Play now."""
        def locate():
            items = self._all(window, kind=self.types.MenuItemControl)
            matches = []
            for item in items:
                text = (item.CurrentName or '').strip().casefold()
                # English UI variants only; no speculative translated selectors.
                if first_request and text == 'play next':
                    matches.append(item)
                elif not first_request and text in ('play last', 'play later', 'add to queue'):
                    matches.append(item)
            return matches[0] if len(matches) == 1 else None
        target = self._wait(locate, 3)
        if not target:
            action = 'Play Next' if first_request else 'Play Last/Play Later'
            raise NativeQueueError(f'No unambiguous {action} menu item. Native queue left unchanged.')
        self._invoke(target)

    def deliver(self, track: Track, first_request: bool = False) -> Delivery:
        window = self._find_window()
        before = self._queue_snapshot(window)
        row = self._target_row(window, track)
        self._open_more(row)
        self._select_queue_action(window, first_request)
        # Apple Music's Windows queue sometimes ignores Play Last altogether.
        # Never treat a successfully invoked menu command as proof of insertion.
        for _ in range(8):
            time.sleep(0.28)
            current = self._queue_snapshot(window)
            if observed_increment(before, current, track):
                return Delivery(True, 'Verified in Apple Music Playing Next.')
        return Delivery(False, 'Apple Music did not show the song in Playing Next. Request kept in Spindle.')


class NativeQueueBridge:
    """Serializes all native UIA calls to one COM-initialized, daemon thread."""
    def __init__(self, backend_factory=None, timeout=18.0):
        self.backend_factory = backend_factory
        self.timeout = timeout
        self.tasks = queue.Queue()
        self.worker = None
        self.lock = threading.Lock()

    def _run(self):
        try:
            if self.backend_factory:
                context = None
                backend = self.backend_factory()
            else:
                from uiautomation import UIAutomationInitializerInThread
                context = UIAutomationInitializerInThread()
                context.__enter__()
                backend = WindowsQueueUIA()
        except Exception as exc:
            backend = None
            startup_error = f'Windows accessibility initialization failed: {exc}'
            context = None
        else:
            startup_error = ''
        try:
            while True:
                track, first_request, future = self.tasks.get()
                if future.cancelled():
                    continue
                try:
                    if backend is None:
                        raise NativeQueueError(startup_error)
                    result = backend.deliver(track, first_request=first_request)
                    if not future.cancelled():
                        future.set_result(result)
                except Exception as exc:
                    if not future.cancelled():
                        future.set_result(Delivery(False, str(exc)[:260]))
        finally:
            if context:
                context.__exit__(None, None, None)

    def enqueue(self, track: Track, first_request: bool = False) -> Delivery:
        if self.backend_factory is None and sys.platform != 'win32':
            return Delivery(False, 'Native queue sync is only available on Windows.')
        with self.lock:
            if not self.worker:
                self.worker = threading.Thread(target=self._run, daemon=True, name='AppleMusicQueueUIA')
                self.worker.start()
        future = Future()
        self.tasks.put((track, first_request, future))
        try:
            return future.result(timeout=self.timeout)
        except FutureTimeout:
            future.cancel()
            return Delivery(False, 'Native queue verification timed out. It may have been added; check Playing Next before retrying.')
