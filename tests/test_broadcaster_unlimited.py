"""Authenticated streamer privileges must not leak to viewers or moderators."""
import asyncio
import pytest

from spindle.commands import CommandEngine
from spindle.models import Track
from spindle.queue_store import QueueStore, QueueError
from spindle.settings import Settings
from spindle.twitch import TwitchClient


def track(index):
    return Track(
        f'Song {index}', 'Test Artist', f'Album {index}',
        180_000, f'unique-id-{index}',
        f'https://music.apple.com/us/song/{index}',
    )


class Catalog:
    def best(self, query):
        return track(int(query.strip().rsplit(' ', 1)[-1]))


def test_broadcaster_bypasses_cooldown_per_viewer_and_global_cap(tmp_path):
    store = QueueStore(tmp_path / 'broadcaster.db')
    try:
        for number in range(1, 6):
            request_id, position = store.add(
                track(number), 'verified-broadcaster-id', 'vatariix',
                max_queue=1, max_per_viewer=1, cooldown_seconds=3600,
                is_broadcaster=True,
            )
            assert request_id == number
            assert position == number
        assert len(store.pending()) == 5
        # The streamer's exemption does not disable duplicate safety.
        with pytest.raises(QueueError, match='already'):
            store.add(
                track(1), 'verified-broadcaster-id', 'vatariix',
                max_queue=1, max_per_viewer=1, cooldown_seconds=3600,
                is_broadcaster=True,
            )
        with pytest.raises(QueueError, match='full'):
            store.add(
                track(6), 'viewer-id', 'viewer',
                max_queue=1, max_per_viewer=1, cooldown_seconds=3600,
            )
    finally:
        store.close()


def test_moderators_are_not_broadcasters_and_still_have_cooldowns(tmp_path):
    store = QueueStore(tmp_path / 'mods.db')
    try:
        store.add(track(1), 'mod-id', 'moderator',
                  max_queue=30, max_per_viewer=1, cooldown_seconds=3600)
        with pytest.raises(QueueError, match='already have'):
            store.add(track(2), 'mod-id', 'moderator',
                      max_queue=30, max_per_viewer=1, cooldown_seconds=3600)
    finally:
        store.close()


def test_engine_requires_explicit_authenticated_broadcaster_role(tmp_path):
    store = QueueStore(tmp_path / 'engine.db')
    engine = CommandEngine(
        store, Catalog(),
        Settings(native_queue_enabled=False, max_queue=1,
                 max_per_viewer=1, cooldown_seconds=3600),
    )
    try:
        assert 'saved in Spindle' in engine.handle(
            'broadcaster-id', 'vatariix', '!sr song 1',
            is_moderator=True, is_broadcaster=True,
        )
        assert 'saved in Spindle' in engine.handle(
            'broadcaster-id', 'vatariix', '!sr song 2',
            is_moderator=True, is_broadcaster=True,
        )
        assert 'full' in engine.handle(
            'mod-id', 'moderator', '!sr song 3', is_moderator=True,
        )
    finally:
        store.close()


def test_twitch_uses_authenticated_user_id_not_username_for_unlimited(tmp_path):
    store = QueueStore(tmp_path / 'twitch.db')
    engine = CommandEngine(
        store, Catalog(),
        Settings(native_queue_enabled=False, max_queue=1,
                 max_per_viewer=1, cooldown_seconds=3600),
    )
    messages = []
    client = TwitchClient(
        'test-client-id', engine, lambda _: None, lambda _: None,
    )
    client.user_id = 'authenticated-owner-id'
    client.user_name = 'vatariix'
    async def fake_send(message):
        messages.append(message)
    client._send = fake_send

    def event(sender_id, name, request, idx, badges=None):
        return {
            'message_id': f'message-{idx}',
            'chatter_user_id': sender_id,
            'chatter_user_name': name,
            'message': {'text': request},
            'badges': badges or [],
        }

    async def run():
        await client._process_chat_event(event(
            'authenticated-owner-id', 'Vatariix', '!sr song 1', 1,
        ))
        await client._process_chat_event(event(
            'authenticated-owner-id', 'Vatariix', '!sr song 2', 2,
        ))
        # A moderator cannot claim broadcaster privileges via their name.
        await client._process_chat_event(event(
            'other-user', 'Vatariix', '!sr song 3', 3,
            [{'set_id': 'moderator'}],
        ))

    try:
        asyncio.run(run())
        assert len(store.pending()) == 2
        assert len(messages) == 3
        assert 'full' in messages[-1]
    finally:
        store.close()


def test_background_only_settings_override_existing_focus_fallback(tmp_path):
    import json
    legacy = {
        'twitch_client_id': '',
        'allow_focus_handoff': True,
        'native_queue_enabled': True,
        'auto_queue_v2_initialized': True,
    }
    (tmp_path / 'settings.json').write_text(json.dumps(legacy), encoding='utf-8')
    settings = Settings.load(tmp_path)
    assert settings.protect_foreground is True
    assert settings.allow_focus_handoff is True  # legacy preference retained
    assert settings.effective_focus_handoff is False  # safety wins
    settings.protect_foreground = False
    assert settings.effective_focus_handoff is True
    settings.save(tmp_path)
    assert Settings.load(tmp_path).effective_focus_handoff is True
