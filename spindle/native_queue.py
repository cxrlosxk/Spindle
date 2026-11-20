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
        self._temporarily_restored_hwnd = None
        self.allow_focus_handoff = False

    def _finish_window(self):
        """Return Apple Music to its minimized state without activation."""
        hwnd = getattr(self, '_temporarily_restored_hwnd', None)
        self._temporarily_restored_hwnd = None
        if hwnd is None:
            return
        try:
            if self.user32.IsWindow(hwnd):
                # SW_SHOWMINNOACTIVE: keep the streamer's foreground game alone.
                self.user32.ShowWindowAsync(hwnd, 7)
        except Exception:
            pass

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
        self._hwnd = hwnd
        if u.IsIconic(hwnd):
            # UIA has no usable visual tree while Apple Music is minimized.
            # SW_SHOWNOACTIVATE shows it at its existing location but should
            # not take the foreground focus away from the user's game.
            if not u.ShowWindowAsync(hwnd, 4):
                # A false return is not always an error; still poll the state.
                pass
            self._temporarily_restored_hwnd = hwnd
            if not self._wait(lambda: not u.IsIconic(hwnd), 3.0, 0.12):
                self._finish_window()
                raise NativeQueueError(
                    'Apple Music could not be restored in the background for '
                    'queue insertion. No keyboard input was sent.'
                )
            time.sleep(0.2)
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
        """Wrap a live UIA element, including unknown/custom WinUI control types.

        Control.CreateControlFromElement() returns None for unknown types.
        Never turn that into an opaque NoneType.GetValuePattern exception.
        """
        if element is None:
            raise NativeQueueError('Apple Music accessibility element was not found.')
        try:
            control = self.auto.Control.CreateControlFromElement(element)
            if control is None:
                # The generic Control accepts a raw IUIAutomationElement and can
                # still expose Value/Invoke/Selection patterns for custom WinUI types.
                control = self.auto.Control(element=element)
            return control
        except Exception as exc:
            raise NativeQueueError(
                f'Apple Music accessibility element changed while reading it '
                f'({type(exc).__name__}); retry the request.'
            ) from exc

    def _invoke(self, element):
        """Activate an explicitly identified UIA action, never an arbitrary click.

        WinUI buttons can expose LegacyIAccessible instead of InvokePattern.
        This fallback does not change keyboard focus or move the mouse.
        """
        if element is None:
            raise NativeQueueError('Apple Music action control was not found.')
        control = self._control(element)
        failures = []
        try:
            pattern = control.GetInvokePattern()
            if pattern is not None:
                pattern.Invoke(waitTime=0)
                return
        except Exception as exc:
            failures.append('Invoke:' + type(exc).__name__)
        try:
            legacy = control.GetLegacyIAccessiblePattern()
            if legacy is not None:
                legacy.DoDefaultAction(waitTime=0)
                return
        except Exception as exc:
            failures.append('Legacy:' + type(exc).__name__)
        raise NativeQueueError(
            'Apple Music action has no usable Invoke or Legacy default pattern'
            + (' (' + ', '.join(failures) + ')' if failures else '')
            + '. Inspect Apple Music to see its available patterns.'
        )

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

    def _queue_toggle_is_on(self, toggle) -> bool | None:
        """Read Apple Music's queue toggle even if its list has no rows."""
        if toggle is None:
            return None
        try:
            control = self._control(toggle)
            if control.GetPattern(10015):  # UIA TogglePattern
                return control.GetTogglePattern().ToggleState == 1
        except Exception:
            return None
        return None

    def _empty_queue_visible(self, window) -> bool:
        """WinUI may omit PlayQueueListView until a song is actually queued."""
        # Match the Windows English empty-state displayed in Playing Next.
        for label in ('No upcoming songs.', 'No upcoming songs'):
            element = self._find(window, name=label, kind=self.types.TextControl)
            if element is not None:
                try:
                    if element.CurrentIsOffscreen:
                        continue
                except Exception:
                    pass
                return True
        return False

    def _queue_snapshot(self, window) -> list[tuple[str, str]]:
        toggle = self._find(window, aid='PlayQueueToggleButton', kind=self.types.ButtonControl)

        def ready():
            queue_list = self._playing_next(window)
            if queue_list is not None:
                return ('list', queue_list)
            if self._empty_queue_visible(window):
                return ('empty', None)
            return None

        # An *empty* Playing Next panel can be open without an accessible list.
        # The old implementation would press its toggle and CLOSE it, then
        # incorrectly report that the panel failed to open.
        existing = ready()
        open_state = self._queue_toggle_is_on(toggle)
        already_open = open_state is True or existing is not None
        opened_here = False
        if not already_open:
            if toggle is None:
                raise NativeQueueError('Apple Music Playing Next button was not found.')
            self._press_queue_toggle(toggle)
            opened_here = True

        try:
            # Never assume that ToggleState=on means the list has loaded.
            # Wait for either an actual list or an explicit empty-state label.
            status = ready() or self._wait(ready, 4.0, 0.15)
            if status is None:
                if self._queue_toggle_is_on(toggle) is True:
                    raise NativeQueueError(
                        'Playing Next opened, but Apple Music did not expose '
                        'its queue list or empty-state text to accessibility.'
                    )
                raise NativeQueueError('Apple Music Playing Next did not open.')
            state, panel = status
            if state == 'empty':
                return []
            time.sleep(0.18)
            results = []
            for row in self._rows(panel):
                texts = self._row_texts(row)
                if len(texts) >= 2:
                    results.append((texts[0], texts[1]))
            return results
        finally:
            if opened_here:
                # Preserve the user's previous panel state.
                try:
                    self._press_queue_toggle(toggle)
                except Exception:
                    pass

    def _visible_action_button(self, window, automation_id):
        """Never invoke a stale WinUI button left behind after a layout change."""
        for button in self._all(
            window, aid=automation_id, kind=self.types.ButtonControl
        ):
            try:
                if button.CurrentIsOffscreen:
                    continue
                rect = button.CurrentBoundingRectangle
                if rect and (rect.right <= rect.left or rect.bottom <= rect.top):
                    continue
            except AttributeError:
                pass
            except Exception:
                continue
            return button
        return None

    def _visible_search_field(self, window):
        """Reject an EditControl left in the UIA tree after Search is closed."""
        for element in self._all(window, aid='TextBox', kind=self.types.EditControl):
            try:
                if element.CurrentIsOffscreen:
                    continue
                rect = element.CurrentBoundingRectangle
                if rect and (rect.right <= rect.left or rect.bottom <= rect.top):
                    continue
            except AttributeError:
                # Lightweight test doubles do not always expose geometry.
                pass
            except Exception:
                continue
            return element
        return None

    def _search_entry_candidates(self, window):
        """Locate the collapsed sidebar's Search item by accessible identity.

        Narrow Apple Music windows replace the search box with a magnifier
        icon. We only operate on a named search navigation control, never on
        an arbitrary icon or an unrelated search result.
        """
        candidates = []
        kinds = [self.types.ButtonControl, self.types.ListItemControl,
                 self.types.TabItemControl]
        for optional in ('TreeItemControl', 'HyperlinkControl', 'CustomControl'):
            kind = getattr(self.types, optional, None)
            if kind is not None:
                kinds.append(kind)
        for kind in kinds:
            for element in self._all(window, kind=kind):
                try:
                    name = (element.CurrentName or '').strip().casefold()
                    aid = (element.CurrentAutomationId or '').strip().casefold()
                    if element.CurrentIsOffscreen:
                        continue
                except Exception:
                    continue
                if (name == 'search' or name in (
                        'search apple music', 'search music', 'search in apple music'
                    ) or aid in ('searchbutton', 'searchnavitem',
                                   'searchnavigationitem', 'navviewsearchbutton')
                        or aid.endswith('searchnavigationviewitem')):
                    candidates.append(element)
        if not candidates:
            # The actual multi-monitor UIA capture supplied for this app
            # identifies the sidebar search container as a GroupControl named
            # AutoSuggestBox with an InvokePattern. Some compact layouts do
            # not expose a separately named magnifying-glass Button.
            for element in self._all(window, kind=self.types.GroupControl):
                try:
                    if (element.CurrentClassName == 'AutoSuggestBox'
                            and self._usable_result(element)):
                        candidates.append(element)
                except Exception:
                    continue
        return candidates

    def _focused_open_search(self):
        """Opt-in Apple Music access key Alt, N, F (documented by Apple).

        Only uses system keys after verifying Apple Music is the foreground
        process. Restores the previous foreground window immediately after.
        This may briefly interrupt a full-screen game: default is disabled.
        """
        if not getattr(self, 'allow_focus_handoff', False):
            return False
        hwnd = getattr(self, '_hwnd', None)
        if hwnd is None or not self.user32.IsWindow(hwnd):
            return False
        u = self.user32
        previous = u.GetForegroundWindow()
        try:
            if previous != hwnd:
                u.SetForegroundWindow(hwnd)
                if not self._wait(lambda: u.GetForegroundWindow() == hwnd, 0.7, 0.07):
                    return False
            for vk in (0x12, 0x4E, 0x46):  # Alt, N, F in sequence.
                if u.GetForegroundWindow() != hwnd:
                    return False
                u.keybd_event(vk, 0, 0, 0)
                u.keybd_event(vk, 0, 0x0002, 0)
                time.sleep(0.06)
            return True
        finally:
            if previous and previous != hwnd and u.IsWindow(previous):
                u.SetForegroundWindow(previous)

    def _open_compact_search(self, window):
        """Reveal the hidden search box using UIA, without typing or clicking."""
        candidates = self._search_entry_candidates(window)
        if not candidates:
            if self._focused_open_search():
                box = self._wait(
                    lambda: self._visible_search_field(window), 4.0, 0.15,
                )
                if box is not None:
                    return box
            raise NativeQueueError(
                'Apple Music compact Search is not accessible in the background. '
                'Enable the optional brief-focus fallback in Settings to use '
                'Apple Music’s Alt, N, F search shortcut automatically.'
            )
        # Prefer an explicitly named button to a surrounding navigation item.
        ranked = sorted(candidates, key=lambda e: (
            0 if e.CurrentName.strip().casefold() == 'search' else 1,
            0 if e.CurrentControlType == self.types.ButtonControl else 1,
        ))
        top = ranked[0]
        top_rank = (
            top.CurrentName.strip().casefold() == 'search',
            top.CurrentControlType == self.types.ButtonControl,
        )
        if len(ranked) > 1:
            next_rank = (
                ranked[1].CurrentName.strip().casefold() == 'search',
                ranked[1].CurrentControlType == self.types.ButtonControl,
            )
            if next_rank == top_rank:
                raise NativeQueueError(
                    'Apple Music exposes multiple Search controls; '
                    'select its magnifying-glass icon manually and retry.'
                )
        control = self._control(top)
        errors = []
        for pattern_name, method in (
            ('invoke', 'GetInvokePattern'),
            ('select', 'GetSelectionItemPattern'),
            ('legacy', 'GetLegacyIAccessiblePattern'),
        ):
            try:
                pattern = getattr(control, method)()
                if pattern is None:
                    continue
                if pattern_name == 'invoke':
                    pattern.Invoke(waitTime=0)
                elif pattern_name == 'select':
                    pattern.Select(waitTime=0)
                else:
                    pattern.DoDefaultAction(waitTime=0)
                break
            except Exception as exc:
                errors.append(type(exc).__name__)
        else:
            raise NativeQueueError(
                'Apple Music Search navigation cannot be activated through '
                'Windows accessibility (' + ','.join(errors[:2]) + ').'
            )
        box = self._wait(lambda: self._visible_search_field(window), 4.0, 0.15)
        if box is None:
            if self._focused_open_search():
                box = self._wait(
                    lambda: self._visible_search_field(window), 4.0, 0.15,
                )
        if box is None:
            raise NativeQueueError(
                'Apple Music Search navigation was selected, but no search '
                'field appeared. Enable the optional brief-focus search '
                'fallback in Settings if background opening is unsupported.'
            )
        return box

    def _set_search_text(self, element, query: str):
        """Set target search text through accessibility, never global keystrokes."""
        control = self._control(element)
        failures = []
        for method in ('GetValuePattern', 'GetLegacyIAccessiblePattern'):
            try:
                pattern = getattr(control, method)()
                if pattern is None:
                    continue
                setter = getattr(pattern, 'SetValue', None)
                if setter is None:
                    continue
                setter(query, waitTime=0)
                return
            except Exception as exc:
                failures.append(method + ': ' + type(exc).__name__)
        raise NativeQueueError(
            'Search field has no working accessibility SetValue pattern'
            + (' (' + ', '.join(failures) + ')' if failures else '')
            + '. Inspect Apple Music to identify this control.'
        )


    def _usable_result(self, element):
        """Exclude virtualized Apple Music result rows with zero-size bounds."""
        try:
            if element.CurrentIsOffscreen:
                return False
            bounds = element.CurrentBoundingRectangle
            if bounds is not None:
                return bounds.right > bounds.left and bounds.bottom > bounds.top
        except AttributeError:
            pass  # Test fixtures can omit WinUI geometry.
        except Exception:
            return False
        return True

    def _scroll_song_section(self, songs):
        """Realize Songs through UIA; return whether navigation was requested."""
        try:
            pattern = self._control(songs).GetScrollItemPattern()
            if pattern:
                pattern.ScrollIntoView(waitTime=0)
                return True
        except Exception:
            pass
        return False

    def _matching_song_rows(self, rows, track):
        matches = []
        for row in rows:
            if not self._usable_result(row):
                continue
            texts = self._row_texts(row)
            if len(texts) >= 2 and strict_track_match(texts[0], texts[1], track):
                matches.append(row)
        return matches

    def _focused_return_only_if_explicitly_allowed(self, search):
        """Last-resort real Enter for WinUI when no accessibility submit exists.

        Windows does not offer an accessible QuerySubmitted operation. This
        optional mode briefly activates Apple Music; users should leave it
        disabled when even a momentary game-focus interruption is unacceptable.
        No keys are sent unless Apple Music is verified as the foreground app.
        """
        if not getattr(self, 'allow_focus_handoff', False):
            return False
        hwnd = getattr(self, '_hwnd', None)
        if hwnd is None or not self.user32.IsWindow(hwnd):
            return False
        u = self.user32
        previous = u.GetForegroundWindow()
        try:
            if previous != hwnd:
                u.SetForegroundWindow(hwnd)
                if not self._wait(lambda: u.GetForegroundWindow() == hwnd, 0.6, 0.06):
                    return False
            try:
                search.SetFocus()  # IUIAutomationElement: exact Apple search edit
            except Exception:
                return False
            if u.GetForegroundWindow() != hwnd:
                return False
            u.keybd_event(0x0D, 0, 0, 0)
            u.keybd_event(0x0D, 0, 0x0002, 0)
            return True
        finally:
            if previous and previous != hwnd and u.IsWindow(previous):
                u.SetForegroundWindow(previous)

    def _post_music_return(self):
        """Best-effort Enter delivered only to Apple Music's own window.

        WinUI may render its AutoSuggestBox without a QueryButton. Posting to
        the Music input-site HWND does not send keystrokes to Twitch/the user's
        foreground game. Results are *never* trusted until the results view
        actually changes. Windows or WinUI may ignore synthetic messages.
        """
        hwnd = getattr(self, '_hwnd', None)
        if hwnd is None or not self.user32.IsWindow(hwnd):
            return False
        ct = self.ctypes
        wt = self.wintypes
        cbtype = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        candidates = []
        def visitor(child, unused):
            klass = ct.create_unicode_buffer(128)
            self.user32.GetClassNameW(child, klass, 128)
            if klass.value == 'InputSiteWindowClass':
                candidates.append(child)
            return True
        callback = cbtype(visitor)
        self.user32.EnumChildWindows(hwnd, callback, 0)
        target = candidates[0] if candidates else hwnd
        # WM_KEYDOWN/UP, VK_RETURN. This is *not* global SendInput and should
        # not interfere with the keyboard of the foreground game.
        down = self.user32.PostMessageW(target, 0x0100, 0x0D, 0x001C0001)
        up = self.user32.PostMessageW(target, 0x0101, 0x0D, 0xC01C0001)
        return bool(down and up)

    def _submit_search(self, window):
        """Submit a real catalog search instead of showing 4 suggestions."""
        # In Apple Music, QueryButton is the official AutoSuggestBox query
        # action. It may be offscreen/zero-bounds until text is entered.
        found = self._all(window, aid='QueryButton')
        if len(found) == 1:
            try:
                self._invoke(found[0])
                return 'query-button'
            except Exception:
                pass
        # A separate WinUI search control can sometimes expose Invoke.
        # Do NOT also post Enter immediately: if Invoke already submitted the
        # query, Enter could play a highlighted result and interrupt music.
        for candidate in self._all(window, kind=self.types.GroupControl):
            try:
                if candidate.CurrentClassName == 'AutoSuggestBox':
                    self._invoke(candidate)
                    return 'autosuggest-invoke'
            except Exception:
                continue
        if self._post_music_return():
            return 'targeted-enter'
        raise NativeQueueError(
            'Could not submit the Apple Music search. The search text is '
            'filled, but no full-results query action is available.'
        )

    def _search_page_state(self, window):
        """A stale page MUST NOT count as results for a new query."""
        content = self._find(
            window, name='Content', kind=self.types.GroupControl,
        )
        songs = self._find(
            content, name='Songs', kind=self.types.ListItemControl,
        )
        try:
            runtime = tuple(songs.GetRuntimeId()) if songs is not None else None
        except Exception:
            runtime = None
        try:
            status = self._find(window, aid='SearchPageStatus')
            status_name = (status.CurrentName or '') if status else ''
        except Exception:
            status_name = ''
        try:
            texts = tuple(
                tuple(self._row_texts(row)[:3])
                for row in (self._rows(songs) if songs else [])
                if self._usable_result(row)
            )
        except Exception:
            texts = ()
        return runtime, status_name, texts

    def _search_results(self, window, track: Track, query_override=None):
        search = self._visible_search_field(window)
        if search is None:
            search = self._open_compact_search(window)
        query = query_override or f'{track.title} {track.artist}'
        previous = self._search_page_state(window)
        self._set_search_text(search, query)
        submission = self._submit_search(window)
        start = time.monotonic()
        scrolled = False

        def find_new_page():
            nonlocal scrolled
            try:
                old_runtime, old_status, old_texts = previous
                new_runtime, new_status, new_texts = self._search_page_state(window)
                # Typing a query updates autocomplete and may even change
                # SearchPageStatus. Require the actual Songs results tree or
                # visible row contents to change before trusting the page.
                state_changed = (
                    (new_runtime is not None and new_runtime != old_runtime)
                    or bool(new_texts and new_texts != old_texts)
                )
                if not state_changed:
                    return None
                content = self._find(
                    window, name='Content', kind=self.types.GroupControl,
                )
                songs = self._find(
                    content, name='Songs', kind=self.types.ListItemControl,
                )
                if songs is None:
                    return None
                if not scrolled:
                    scrolled = True
                    if self._scroll_song_section(songs):
                        return None
                rows = [
                    row for row in self._rows(songs)
                    if self._usable_result(row)
                ]
                return rows or None
            except Exception:
                # UIA references commonly go stale as WinUI replaces its page.
                return None

        # Each submission technique is tried separately. Never send two
        # Enter actions in quick succession: a late-loading results page could
        # interpret the second as Play Now.
        wait_first = 3.0 if submission == 'autosuggest-invoke' else 8.0
        rows = self._wait(find_new_page, wait_first, 0.2)
        if rows:
            return rows
        if submission == 'autosuggest-invoke':
            if self._post_music_return():
                submission += '+targeted-enter'
                rows = self._wait(find_new_page, 7.0, 0.2)
                if rows:
                    return rows
        if self._focused_return_only_if_explicitly_allowed(search):
            # This is opt-in because the OS must briefly focus Apple Music.
            rows = self._wait(find_new_page, 8.0, 0.2)
            if rows:
                return rows
            submission += '+focused-enter'
        raise NativeQueueError(
            'Apple Music did not show a newly submitted full search-results '
            'page after ' + submission + '. Spindle will not use old results '
            'or the four-item suggestion dropdown. If background submission '
            'is unsupported on this Apple Music build, Settings offers an '
            'optional brief-focus fallback.'
        )

    def _open_all_songs(self, window):
        """Open the full Songs category when the first page shows few tracks."""
        content = self._find(
            window, name='Content', kind=self.types.GroupControl,
        )
        if content is None:
            return False
        section = self._find(
            content, name='Songs', kind=self.types.ListItemControl,
        )
        candidates = []
        search_roots = [section, content] if section is not None else [content]
        control_types = [
            self.types.ButtonControl, self.types.HyperlinkControl,
            self.types.ListItemControl,
        ]
        for root in search_roots:
            if root is None:
                continue
            for kind in control_types:
                for elem in self._all(root, kind=kind):
                    try:
                        name = (elem.CurrentName or '').strip().casefold()
                        aid = (elem.CurrentAutomationId or '').strip().casefold()
                    except Exception:
                        continue
                    if (name in ('see all songs', 'view all songs',
                                 'show all songs', 'all songs')
                            or aid in ('seeallsongs', 'showallsongs',
                                       'viewallsongs')):
                        candidates.append(elem)
            if candidates:
                break
        if len(candidates) != 1:
            return False
        self._invoke(candidates[0])
        return True

    def _refresh_visible_matches(self, window, track):
        content = self._find(
            window, name='Content', kind=self.types.GroupControl,
        )
        songs = self._find(
            content, name='Songs', kind=self.types.ListItemControl,
        )
        if songs is None:
            return []
        return self._matching_song_rows(self._rows(songs), track)

    def _target_row(self, window, track):
        # Only real, visible WinUI rows can be acted on. Apple Music frequently
        # retains several virtualized copies of a search result at (0, 0).
        self._selection_note = ''
        matched = self._matching_song_rows(self._search_results(window, track), track)
        if not matched:
            matched = self._wait(
                lambda: self._refresh_visible_matches(window, track),
                2.5, 0.2,
            ) or []
        if not matched and self._open_all_songs(window):
            matched = self._wait(
                lambda: self._refresh_visible_matches(window, track),
                5.0, 0.2,
            ) or []
        if not matched and track.album:
            # The initial four Songs results might omit an album cut. Retry
            # the full query with the album to narrow catalog relevance.
            narrowed = f'{track.title} {track.artist} {track.album}'
            matched = self._matching_song_rows(
                self._search_results(window, track, query_override=narrowed),
                track,
            )
        if not matched:
            raise NativeQueueError(
                'No exact title-and-artist match appeared in Apple Music full '
                'Songs results, including its all-songs and album search.'
            )
        if len(matched) == 1:
            return matched[0]

        # Album metadata is not always exposed in the Songs ListView, but when
        # it is available, require an exact album match over a generic hit.
        if track.album:
            album_matches = []
            for row in matched:
                text = self._row_texts(row)
                if any(norm(value) == norm(track.album) for value in text[2:]):
                    album_matches.append(row)
            if len(album_matches) == 1:
                return album_matches[0]

        # Multiple editions often share the exact artist/title, while the
        # accessibility tree omits their album IDs. For a plain song request,
        # use Apple's leading *visible* exact-title-and-artist result. We do
        # not assert that the specific release/album was verified.
        self._selection_note = (
            'The title and artist matched, but Apple Music did not expose '
            'enough metadata to confirm the album or recording edition.'
        )
        return matched[0]

    def _open_more(self, row):
        buttons = self._all(row, kind=self.types.ButtonControl)
        if not buttons:
            raise NativeQueueError('Apple Music did not expose a More (…) button for this song.')
        named = []
        for button in buttons:
            try:
                identity = (button.CurrentName or '').casefold()
                aid = (button.CurrentAutomationId or '').casefold()
            except Exception:
                continue
            if identity == 'more' or aid == 'morebutton':
                named.append(button)
        visible = [b for b in named if self._usable_result(b)]
        # WinUI can expose a valid InvokePattern for a More button even when
        # its visual bounds are zero until pointer hover. It is still scoped
        # to the already-verified exact song row, not a guessed global button.
        candidates = visible if len(visible) == 1 else named
        if len(candidates) != 1:
            raise NativeQueueError(
                'Apple Music did not expose exactly one song-specific More '
                'button. No unrelated button was pressed.'
            )
        self._invoke(candidates[0])

    def _visible_menu_items(self, window):
        """Find context menus that WinUI may host outside the music window.

        A WinUI popup can be a separate top-level accessibility window. Only
        search other top-level windows owned by the *same process* as Apple
        Music. Never invoke a menu belonging to the active game or Spindle.
        """
        def visible(items):
            result = []
            for item in items:
                try:
                    if not item.CurrentIsOffscreen:
                        result.append(item)
                except Exception:
                    pass
            return result

        items = visible(self._all(window, kind=self.types.MenuItemControl))
        if items:
            return items
        try:
            pid = int(window.CurrentProcessId)
            desktop = self.api.GetRootElement()
            top_windows = self._all(desktop, scope=2)
        except Exception:
            return []
        for candidate in top_windows:
            try:
                if candidate.CurrentIsOffscreen or int(candidate.CurrentProcessId) != pid:
                    continue
                items.extend(visible(self._all(candidate, kind=self.types.MenuItemControl)))
            except Exception:
                continue
        return items

    def _select_queue_action(self, window, first_request=False):
        """First request plays next; later requests append. Never use Play now."""
        def locate():
            matches = []
            for item in self._visible_menu_items(window):
                try:
                    text = (item.CurrentName or '').strip().casefold()
                except Exception:
                    continue
                # Only exact Apple Music queue actions. Never select generic
                # Play, since that would interrupt the stream.
                if first_request and text in ('play next', 'add to queue'):
                    matches.append(item)
                elif not first_request and text in ('play last', 'play later', 'add to queue'):
                    matches.append(item)
            if first_request and len(matches) > 1:
                # Always prioritize Play Next; Add to Queue is the supported
                # alternate when Apple's menu exposes no Play Next action.
                play_next = [m for m in matches if
                             (m.CurrentName or '').strip().casefold() == 'play next']
                if len(play_next) == 1:
                    return play_next[0]
            return matches[0] if len(matches) == 1 else None
        target = self._wait(locate, 3)
        if not target:
            action = 'Play Next' if first_request else 'Play Last/Play Later'
            raise NativeQueueError(
                f'No unambiguous {action} menu item; no playback action selected.'
            )
        self._invoke(target)

    def deliver(self, track: Track, first_request: bool = False) -> Delivery:
        """Attempt native insertion with a named failure stage for diagnostics."""
        stage = 'locate Apple Music'
        try:
            window = self._find_window()
            stage = 'read Playing Next before insertion'
            before = self._queue_snapshot(window)
            stage = 'find song via Search'
            row = self._target_row(window, track)
            stage = 'open song More menu'
            self._open_more(row)
            stage = 'choose Play Next or Play Last'
            self._select_queue_action(window, first_request)
            stage = 'verify Playing Next after insertion'
            # Invoking an accessible menu isn't proof. Require a new,
            # correctly identified track in Apple's real queue.
            for _ in range(8):
                time.sleep(0.28)
                current = self._queue_snapshot(window)
                if observed_increment(before, current, track):
                    return Delivery(
                        True,
                        'Verified title and artist in Apple Music Playing Next.'
                        + (' ' + self._selection_note if self._selection_note else ''),
                    )
            return Delivery(
                False, 'Apple Music did not show the new song in Playing Next.'
            )
        except Exception as exc:
            raise NativeQueueError(
                f'{stage}: {type(exc).__name__}: {str(exc)[:170]}'
            ) from exc
        finally:
            self._finish_window()

    def inspect_diagnostics(self):
        """Inspect without sending input, then restore the prior window state."""
        from .diagnostics import inspect_window
        try:
            return inspect_window(self)
        finally:
            self._finish_window()


class NativeQueueBridge:
    """Serializes all native UIA calls to one COM-initialized, daemon thread."""
    def __init__(self, backend_factory=None, timeout=65.0):
        self.backend_factory = backend_factory
        self.allow_focus_handoff = False
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
                is_probe = track is None
                try:
                    if backend is None:
                        raise NativeQueueError(startup_error)
                    if is_probe:
                        result = backend.inspect_diagnostics()
                    else:
                        if self.backend_factory is None:
                            backend.allow_focus_handoff = self.allow_focus_handoff
                        result = backend.deliver(track, first_request=first_request)
                    if not future.cancelled():
                        future.set_result(result)
                except Exception as exc:
                    if not future.cancelled():
                        if is_probe:
                            future.set_result({
                                "schema": 1, "read_only": True,
                                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                            })
                        else:
                            future.set_result(Delivery(False, str(exc)[:260]))
        finally:
            if context:
                context.__exit__(None, None, None)

    def diagnose(self) -> dict:
        """Return diagnostic details without modifying Apple Music or its queue."""
        if self.backend_factory is None and sys.platform != 'win32':
            return {"schema": 1, "read_only": True, "error": "Windows is required."}
        with self.lock:
            if not self.worker:
                self.worker = threading.Thread(
                    target=self._run, daemon=True, name='AppleMusicQueueUIA'
                )
                self.worker.start()
        future = Future()
        self.tasks.put((None, False, future))
        try:
            return future.result(timeout=max(35.0, self.timeout))
        except FutureTimeout:
            future.cancel()
            return {
                "schema": 1, "read_only": True,
                "error": "Apple Music accessibility inspection timed out.",
            }

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
