"""Plain preferences in JSON; tokens are stored in Windows Credential Manager by keyring."""
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


def data_dir() -> Path:
    root = os.environ.get('LOCALAPPDATA')
    path = Path(root) / 'Spindle' if root else Path.home() / '.spindle'
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Settings:
    twitch_client_id: str = ''
    requests_enabled: bool = True
    max_queue: int = 30
    max_per_viewer: int = 2
    cooldown_seconds: int = 90
    max_duration_minutes: int = 12
    allow_explicit: bool = True
    native_queue_enabled: bool = True  # Automatic song requests, switchable in Settings.
    allow_focus_handoff: bool = False  # Legacy compatibility preference.
    protect_foreground: bool = True  # Game-safe: never request focus handoff.
    auto_queue_v2_initialized: bool = True

    @property
    def effective_focus_handoff(self) -> bool:
        """A legacy compatibility opt-in must never override game protection."""
        return bool(self.allow_focus_handoff and not self.protect_foreground)

    @classmethod
    def load(cls, directory: Path | None = None) -> 'Settings':
        path = (directory or data_dir()) / 'settings.json'
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            filtered = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
            # One-time upgrade for the new unattended-request workflow.
            # The previous version initialized native queue sync as off
            # by default. This release's primary feature is automatic
            # delivery; after migration the user's Settings choice persists.
            if 'auto_queue_v2_initialized' not in raw:
                filtered['native_queue_enabled'] = True
                filtered['auto_queue_v2_initialized'] = True
                settings = cls(**filtered)
                settings.save(directory)
                return settings
            return cls(**filtered)
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, directory: Path | None = None):
        path = (directory or data_dir()) / 'settings.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding='utf-8')
        tmp.replace(path)
