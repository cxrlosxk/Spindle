"""Regression tests for broadcaster commands and Twitch response receipts."""
import asyncio
import sys
import types

# The full keyring is installed in the Windows CI job. Avoid requiring a host
# OS credential backend just to test command routing on Linux.
try:
    import keyring
except ImportError:
    sys.modules['keyring'] = types.SimpleNamespace(
        get_password=lambda *a: None, set_password=lambda *a: None,
    )

from spindle.twitch import TwitchClient, TwitchError
from spindle.commands import CommandEngine
from spindle.queue_store import QueueStore
from spindle.models import Track
from spindle.settings import Settings


def test_broadcaster_and_viewer_commands_are_processed(tmp_path):
    events = []
    responses = []
    queue = QueueStore(tmp_path / 'queue.db')
    catalog = type('CatalogStub', (), {'best': lambda self, query: Track(
        'AMEN', 'Beyoncé', 'COWBOY CARTER', 180000, '123',
        'https://music.apple.com/us/song/amen/123')})()
    engine = CommandEngine(queue, catalog, Settings(cooldown_seconds=0))
    engine.set_current(Track('Fourth of July', 'Sufjan Stevens', playing=True))
    client = TwitchClient('test-client-id', engine, events.append, events.append)
    client.user_id = 'broadcaster-id'
    client.user_name = 'vatariix'

    async def fake_send(message):
        responses.append(message)

    client._send = fake_send
    own_song = {'message_id': 'own-msg-1', 'chatter_user_id': 'broadcaster-id',
                'chatter_user_name': 'VatariiX', 'message': {'text': '!song'}, 'badges': []}
    own_request = {**own_song, 'message_id': 'own-msg-2', 'message': {'text': '!sr AMEN beyonce'}}
    viewer = {**own_song, 'message_id': 'viewer-msg', 'chatter_user_id': 'fan-id',
              'chatter_user_name': 'fan', 'message': {'text': '!queue'}}

    async def run():
        await client._process_chat_event(own_song)
        await client._process_chat_event(own_request)
        await client._process_chat_event(viewer)

    asyncio.run(run())
    assert any('Sufjan Stevens' in r for r in responses)
    assert any('Request saved in Spindle' in r for r in responses)
    assert any('Beyoncé' in r for r in responses)
    assert len(queue.pending()) == 1
    queue.close()


def test_broadcaster_moderator_actions_work(tmp_path):
    queue = QueueStore(tmp_path / 'queue.db')
    engine = CommandEngine(queue, object(), Settings())
    client = TwitchClient('test-client-id', engine, lambda *_:None, lambda *_:None)
    client.user_id = 'broadcaster-id'
    responses = []
    async def fake_send(msg): responses.append(msg)
    client._send = fake_send
    asyncio.run(client._process_chat_event({
        'message_id': 'mod-1', 'chatter_user_id': 'broadcaster-id',
        'chatter_user_name': 'vatariix', 'message': {'text': '!sr off'},
        'badges': [],
    }))
    assert engine.settings.requests_enabled is False
    assert 'disabled' in responses[0]
    queue.close()


def test_own_bot_echo_ignored_and_failed_sends_visible():
    logs = []
    engine = type('Engine', (), {'handle': lambda self, *a: '♫ Now playing'})()
    client = TwitchClient('test-client-id', engine, logs.append, logs.append)
    client.user_id = 'broadcaster-id'
    client._remember_sent('bot-msg')

    async def rejected_send(msg):
        raise TwitchError('Twitch rejected the chat reply')

    client._send = rejected_send
    async def run():
        await client._process_chat_event({
            'message_id': 'bot-msg', 'chatter_user_id': 'broadcaster-id',
            'message': {'text': '!song'},
        })
        await client._process_chat_event({
            'message_id': 'user-msg', 'chatter_user_id': 'viewer-1',
            'chatter_user_name': 'fan', 'message': {'text': '!song'},
        })
    asyncio.run(run())
    assert sum('Received from' in msg for msg in logs) == 1
    assert any('reply failed' in msg for msg in logs)


def test_send_api_receipt_marks_bot_output_and_handles_rejection():
    client = TwitchClient('test-client-id', object(), lambda *_:None, lambda *_:None)
    client.user_id = 'broadcaster-id'
    client._helix = lambda *args: {'data': [{'is_sent': True, 'message_id': 'sent-1'}]}
    asyncio.run(client._send('hello'))
    assert 'sent-1' in client._sent_id_set
    client._helix = lambda *args: {'data': [{'is_sent': False, 'drop_reason': {'message': 'slow mode'}}]}
    import pytest
    with pytest.raises(TwitchError, match='slow mode'):
        asyncio.run(client._send('hello'))
