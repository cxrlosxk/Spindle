"""Read-only official Apple Music playback detection using Windows GSMTC.

No claim of ability to modify Apple Music Up Next. No Windows focus stealing.
"""
import asyncio
from .models import Track


class AppleMusicMonitor:
    def __init__(self, on_track, on_status):
        self.on_track = on_track
        self.on_status = on_status
        self.running = False

    async def run(self):
        self.running = True
        try:
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager,
                GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus
            )
        except ImportError:
            self.on_status('Windows media dependency unavailable. Install requirements on Windows.')
            return
        try:
            manager = await Manager.request_async()
        except Exception as exc:
            self.on_status(f'Windows media session unavailable: {exc}')
            return
        self.on_status('Watching Apple Music playback')
        previous = object()
        while self.running:
            current = None
            try:
                sessions = list(manager.get_sessions() or [])
                matches = [s for s in sessions if 'apple' in s.source_app_user_model_id.lower()
                           and 'music' in s.source_app_user_model_id.lower()]
                if matches:
                    session = matches[0]
                    props = await session.try_get_media_properties_async()
                    info = session.get_playback_info()
                    timeline = session.get_timeline_properties()
                    current = Track(
                        title=props.title or '', artist=props.artist or '', album=props.album_title or '',
                        duration_ms=(int(timeline.end_time.total_seconds() * 1000) if timeline else 0),
                        playing=bool(info and info.playback_status == PlaybackStatus.PLAYING),
                        source=session.source_app_user_model_id
                    )
                    if not current.title:
                        current = None
                if current != previous:
                    self.on_track(current)
                    previous = current
            except Exception as exc:
                self.on_status(f'Could not read Apple Music playback: {exc}')
            await asyncio.sleep(2)

    def stop(self):
        self.running = False
