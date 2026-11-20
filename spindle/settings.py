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
    native_queue_enabled: bool = False  # Explicit opt-in until tested on Apple Music for Windows.

    @classmethod
    def load(cls, directory: Path | None = None) -> 'Settings':
        path = (directory or data_dir()) / 'settings.json'
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            filtered = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
            return cls(**filtered)
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, directory: Path | None = None):
        path = (directory or data_dir()) / 'settings.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding='utf-8')
        tmp.replace(path)
