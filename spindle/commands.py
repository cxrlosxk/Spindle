"""Pure Twitch command logic. All responses distinguish request queue from Apple Music."""
import re
from threading import RLock
from .catalog import Catalog, CatalogError, norm
from .models import Track
from .queue_store import QueueStore, QueueError
from .settings import Settings
from .native_queue import NativeQueueBridge, strict_track_match


class CommandEngine:
    def __init__(self, store: QueueStore, catalog: Catalog, settings: Settings, on_change=None,
                 native_queue: NativeQueueBridge | None = None, on_status=None):
        self.store = store
        self.catalog = catalog
        self.settings = settings
        self.on_change = on_change or (lambda: None)
        self._current: Track | None = None
        self._lock = RLock()
        self.native_queue = native_queue
        self.on_status = on_status or (lambda message: None)
        self._previous_playing_key = None

    def set_current(self, current: Track | None):
        # Clear successfully queued songs only on a *new* playing-track transition.
        with self._lock:
            previous = self._previous_playing_key
            key = (norm(current.title), norm(current.artist)) if current and current.playing else None
            self._current = current
            self._previous_playing_key = key
        if previous and key and previous != key:
            for requested in self.store.queued():
                if strict_track_match(requested.title, requested.artist, current):
                    if self.store.change(requested.id, 'played'):
                        self.on_change()
                    break

    def get_current(self) -> Track | None:
        with self._lock:
            return self._current

    def handle(self, user_id: str, user_name: str, message: str, is_moderator=False) -> str | None:
        message = message.strip()
        match = re.match(r'^(!\S+)(?:\s+(.*))?$', message, re.DOTALL)
        if not match:
            return None
        command = match.group(1).casefold()
        arg = (match.group(2) or '').strip()

        if command in ('!song', '!np'):
            current = self.get_current()
            if not current:
                return 'Apple Music is not connected or no track is available right now.'
            if not current.playing:
                return f'Apple Music is paused. Last track: {current.formatted}.'
            return f'♫ Now playing: {current.title} — {current.artist}'

        if command == '!queue':
            active = self.store.active()
            if not active:
                return 'No song requests in Spindle right now.'
            excerpt = ' | '.join(f'{i+1}. {p.title} — {p.artist}' for i,p in enumerate(active[:3]))
            verified = sum(p.status == 'queued' for p in active)
            return f'♫ Requests: {len(active)} total, {verified} verified in Apple Music. {excerpt}' + (' | more in Spindle' if len(active) > 3 else '')

        if command in ('!sr', '!request'):
            if arg.casefold() in ('on', 'off') and is_moderator:
                self.settings.requests_enabled = arg.casefold() == 'on'
                self.settings.save()
                self.on_change()
                return 'Song requests are now ' + ('enabled.' if self.settings.requests_enabled else 'disabled.')
            if not self.settings.requests_enabled:
                return 'Song requests are currently closed.'
            if not arg:
                return 'Usage: !sr AMEN Beyoncé (or paste a direct Apple Music song link).'
            try:
                track = self.catalog.best(arg)
                if track.duration_ms > self.settings.max_duration_minutes * 60_000 and track.duration_ms > 0:
                    return f'{track.title} is longer than the {self.settings.max_duration_minutes}-minute request limit.'
                request_id, position = self.store.add(track, user_id, user_name,
                    max_queue=self.settings.max_queue, max_per_viewer=self.settings.max_per_viewer,
                    cooldown_seconds=self.settings.cooldown_seconds)
                self.on_change()
                if self.settings.native_queue_enabled and self.native_queue is not None:
                    delivery = self.native_queue.enqueue(track, first_request=not bool(self.store.queued()))
                    if delivery.verified:
                        self.store.mark_queued(request_id)
                        self.on_change()
                        self.on_status(f'Apple Music Up Next confirmed: {track.title} — {track.artist}')
                        return f'♫ Added to Apple Music Up Next: {track.title} — {track.artist} (requested by @{user_name})'
                    self.on_status(f'Native queue could not be verified: {delivery.reason}')
                    return f'♫ Request saved in Spindle: {track.title} — {track.artist}. Could not verify Apple Music Up Next.'
                return f'♫ Request saved in Spindle: {track.title} — {track.artist}. Not yet in Apple Music Up Next.'
            except (CatalogError, QueueError) as exc:
                return str(exc)

        if command == '!unrequest':
            if self.store.remove_own(user_id):
                self.on_change()
                return f'@{user_name}, your latest pending request was removed.'
            return f'@{user_name}, you have no pending requests to remove.'

        if command == '!remove' and is_moderator:
            try:
                position = int(arg)
            except ValueError:
                return 'Usage: !remove 2 (position in the pending queue)'
            queue = self.store.pending()
            if not 1 <= position <= len(queue):
                return 'That request position does not exist.'
            self.store.change(queue[position-1].id, 'removed')
            self.on_change()
            return f'Removed pending request #{position}.'

        return None
