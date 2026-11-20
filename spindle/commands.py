"""Pure Twitch command logic. All responses distinguish request queue from Apple Music."""
import re
from threading import RLock
from .catalog import Catalog, CatalogError
from .models import Track
from .queue_store import QueueStore, QueueError
from .settings import Settings


class CommandEngine:
    def __init__(self, store: QueueStore, catalog: Catalog, settings: Settings, on_change=None):
        self.store = store
        self.catalog = catalog
        self.settings = settings
        self.on_change = on_change or (lambda: None)
        self._current: Track | None = None
        self._lock = RLock()

    def set_current(self, current: Track | None):
        with self._lock:
            self._current = current

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
            return f'♫ Now playing on Apple Music: {current.formatted}'

        if command == '!queue':
            pending = self.store.pending()
            if not pending:
                return 'There are no viewer requests pending.'
            excerpt = ' | '.join(f'{i+1}. {p.artist} — {p.title}' for i,p in enumerate(pending[:3]))
            return f'Pending requests ({len(pending)}): {excerpt}' + (' | more in Spindle' if len(pending) > 3 else '')

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
                _, position = self.store.add(track, user_id, user_name,
                    max_queue=self.settings.max_queue, max_per_viewer=self.settings.max_per_viewer,
                    cooldown_seconds=self.settings.cooldown_seconds)
                self.on_change()
                return f'♫ Request #{position} pending: {track.formatted}. This is in the Spindle request queue, not Apple Music Up Next yet.'
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
