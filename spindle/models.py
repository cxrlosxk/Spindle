from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    album: str = ''
    duration_ms: int = 0
    track_id: str = ''
    url: str = ''
    playing: bool = False
    source: str = 'Apple Music'

    @property
    def formatted(self) -> str:
        return f'{self.artist} — {self.title}'


@dataclass(frozen=True)
class Request:
    id: int
    title: str
    artist: str
    album: str
    duration_ms: int
    track_id: str
    url: str
    viewer_id: str
    viewer_name: str
    status: str
    created_at: str
