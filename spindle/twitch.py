"""Twitch public-client Device Code OAuth, EventSub WebSocket and chat replies.

Spindle authenticates the broadcaster to post as themselves. Twitch public
clients must NOT ship a client secret; Twitch does not support OAuth PKCE.
Tokens are stored in the OS credential vault by keyring, never source control.
"""
from __future__ import annotations
import asyncio
import json
from collections import deque
import time
import webbrowser
from threading import RLock

import keyring
import requests
import websockets

SCOPES = 'user:read:chat user:write:chat'
KEYRING_SERVICE = 'Spindle Twitch'


class TwitchError(RuntimeError):
    pass


class TwitchClient:
    def __init__(self, client_id, engine, on_status, on_message, on_device_code=None):
        self.client_id = client_id.strip()
        self.engine = engine
        self.on_status = on_status
        self.on_message = on_message
        self.on_device_code = on_device_code or (lambda code, url: None)
        self.running = False
        self.token = None
        self.user_id = None
        self.user_name = None
        self._token_lock = RLock()
        self._seen = deque(maxlen=600)
        self._seen_set = set()
        # Do not reject broadcaster commands; filter Spindle's own replies by ID.
        self._sent_ids = deque(maxlen=600)
        self._sent_id_set = set()

    def _save_token(self):
        keyring.set_password(KEYRING_SERVICE, self.client_id, json.dumps(self.token))

    def _load_token(self):
        raw = keyring.get_password(KEYRING_SERVICE, self.client_id)
        return json.loads(raw) if raw else None

    @staticmethod
    def _request(method, url, **kwargs):
        response = requests.request(method, url, timeout=15, **kwargs)
        if response.status_code >= 400:
            try:
                message = response.json().get('message', '')
            except ValueError:
                message = ''
            raise TwitchError(f'Twitch returned HTTP {response.status_code}: {message or response.text[:100]}')
        return response.json()

    @staticmethod
    def _with_expiry(data):
        return {**data, 'expires_at': time.time() + int(data.get('expires_in', 3600))}

    def _refresh(self):
        if not self.token or not self.token.get('refresh_token'):
            raise TwitchError('Twitch login needs to be renewed.')
        data = self._request('POST', 'https://id.twitch.tv/oauth2/token', data={
            'grant_type': 'refresh_token',
            'refresh_token': self.token['refresh_token'],
            'client_id': self.client_id,
        })
        self.token = self._with_expiry(data)
        self._save_token()  # rotating refresh token; must persist immediately

    def authorize(self):
        if not self.client_id:
            raise TwitchError('Enter your Twitch Application Client ID in Settings first.')
        try:
            self.token = self._load_token()
        except Exception as exc:
            raise TwitchError('Could not access your OS credential vault. No token was stored.') from exc
        if self.token:
            try:
                data = self._request('GET', 'https://id.twitch.tv/oauth2/validate',
                                     headers={'Authorization': f'Bearer {self.token["access_token"]}'})
                if data.get('client_id') == self.client_id and set(SCOPES.split()).issubset(set(data.get('scopes', []))):
                    return
            except (TwitchError, KeyError):
                pass
            try:
                self._refresh()
                return
            except TwitchError:
                self.on_status('Stored Twitch login expired. Please authorize again.')

        # Device Code Flow, supported for registered Twitch PUBLIC client apps.
        details = self._request('POST', 'https://id.twitch.tv/oauth2/device', data={
            'client_id': self.client_id, 'scopes': SCOPES,
        })
        code, url = details['user_code'], details['verification_uri']
        self.on_device_code(code, url)
        self.on_status(f'Authorize Twitch in your browser. Your code is {code}.')
        webbrowser.open(url)
        deadline = time.monotonic() + min(int(details.get('expires_in', 1800)), 1800)
        interval = max(5, int(details.get('interval', 5)))
        while self.running and time.monotonic() < deadline:
            time.sleep(interval)
            try:
                data = self._request('POST', 'https://id.twitch.tv/oauth2/token', data={
                    'client_id': self.client_id,
                    'scopes': SCOPES,
                    'device_code': details['device_code'],
                    'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                })
            except TwitchError as exc:
                error = str(exc).lower()
                if 'authorization_pending' in error:
                    continue
                if 'slow_down' in error:
                    interval += 5
                    continue
                if 'access_denied' in error or 'expired' in error:
                    raise TwitchError('Twitch authorization was denied or expired.') from exc
                raise
            self.token = self._with_expiry(data)
            self._save_token()
            return
        raise TwitchError('Twitch authorization timed out or was canceled.')

    def _headers(self):
        with self._token_lock:
            if self.token and self.token.get('expires_at', 0) < time.time() + 90:
                self._refresh()
            if not self.token:
                raise TwitchError('Twitch is not authenticated.')
            return {'Client-Id': self.client_id,
                    'Authorization': f'Bearer {self.token["access_token"]}'}

    def _helix(self, method, endpoint, body=None):
        return self._request(method, f'https://api.twitch.tv/helix/{endpoint}',
                             headers=self._headers(), json=body)

    async def _send(self, message):
        # Twitch Send Chat Message accepts up to 500 chars. Leave room for future prefixes.
        result = await asyncio.to_thread(self._helix, 'POST', 'chat/messages', {
            'broadcaster_id': self.user_id,
            'sender_id': self.user_id,
            'message': message[:480],
        })
        receipts = result.get('data') or []
        if not receipts:
            raise TwitchError('Twitch did not confirm that the chat reply was sent.')
        receipt = receipts[0]
        if not receipt.get('is_sent', False):
            reason = receipt.get('drop_reason') or {}
            raise TwitchError('Twitch rejected the chat reply: ' + str(reason.get('message') or 'chat restrictions'))
        self._remember_sent(receipt.get('message_id'))

    def _first_seen(self, message_id):
        if not message_id or message_id in self._seen_set:
            return False
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen.popleft())
        self._seen.append(message_id)
        self._seen_set.add(message_id)
        return True

    def _remember_sent(self, message_id):
        if not message_id or message_id in self._sent_id_set:
            return
        if len(self._sent_ids) == self._sent_ids.maxlen:
            self._sent_id_set.discard(self._sent_ids.popleft())
        self._sent_ids.append(message_id)
        self._sent_id_set.add(message_id)

    async def _process_chat_event(self, event):
        """Handle broadcaster and viewer commands without echoing bot replies."""
        message = (event.get('message') or {}).get('text', '').strip()
        command = message.split(maxsplit=1)[0].casefold() if message else ''
        if command not in {'!song', '!np', '!sr', '!request', '!queue', '!unrequest', '!remove'}:
            return
        if event.get('message_id') in self._sent_id_set:
            return
        sender_id = str(event.get('chatter_user_id') or '')
        if not sender_id:
            self.on_status('Received a chat command without a sender ID; ignored.')
            return
        sender_name = event.get('chatter_user_name') or 'viewer'
        badges = {badge.get('set_id') for badge in event.get('badges', [])}
        is_moderator = sender_id == self.user_id or bool({'moderator', 'broadcaster'} & badges)
        self.on_message(f'Received from @{sender_name}: {message}')
        try:
            response = await asyncio.to_thread(
                self.engine.handle, sender_id, sender_name, message, is_moderator,
            )
        except Exception as exc:
            self.on_status(f'Could not process {command}: {str(exc)[:160]}')
            return
        if not response:
            return
        try:
            await self._send(response)
        except Exception as exc:
            self.on_status(f'Command processed, but Twitch reply failed: {str(exc)[:160]}')
        else:
            self.on_message(f'Replied to @{sender_name}: {response}')

    async def _listen(self):
        connection_url = 'wss://eventsub.wss.twitch.tv/ws'
        reuse_subscription = False
        while self.running:
            try:
                async with websockets.connect(connection_url, ping_interval=20, ping_timeout=20) as ws:
                    self.on_status('Connecting to Twitch EventSub…')
                    reconnecting = False
                    async for raw in ws:
                        if not self.running:
                            return
                        payload = json.loads(raw)
                        metadata = payload.get('metadata', {})
                        msg_type = metadata.get('message_type')
                        if msg_type == 'session_welcome':
                            session_id = payload['payload']['session']['id']
                            if not reuse_subscription:
                                await asyncio.to_thread(self._helix, 'POST', 'eventsub/subscriptions', {
                                    'type': 'channel.chat.message', 'version': '1',
                                    'condition': {'broadcaster_user_id': self.user_id,
                                                  'user_id': self.user_id},
                                    'transport': {'method': 'websocket', 'session_id': session_id},
                                })
                            reuse_subscription = False
                            self.on_status(f'Connected to Twitch chat as @{self.user_name}')
                        elif msg_type == 'notification' and metadata.get('subscription_type') == 'channel.chat.message':
                            if not self._first_seen(metadata.get('message_id')):
                                continue
                            event = payload.get('payload', {}).get('event', {})
                            await self._process_chat_event(event)
                        elif msg_type == 'session_reconnect':
                            new_url = payload.get('payload', {}).get('session', {}).get('reconnect_url')
                            if new_url and new_url.startswith('wss://eventsub.wss.twitch.tv/'):
                                connection_url = new_url
                                reuse_subscription = True
                                reconnecting = True
                                break
                            raise TwitchError('Twitch sent an invalid reconnect URL.')
                        elif msg_type == 'revocation':
                            raise TwitchError('Twitch chat permission was revoked. Reconnect your account.')
                    if reconnecting:
                        continue
                    connection_url = 'wss://eventsub.wss.twitch.tv/ws'
            except Exception as exc:
                self.on_status(f'Twitch disconnected: {str(exc)[:180]}')
                connection_url = 'wss://eventsub.wss.twitch.tv/ws'
                reuse_subscription = False
            if self.running:
                await asyncio.sleep(5)

    async def run(self):
        self.running = True
        try:
            await asyncio.to_thread(self.authorize)
            identity = await asyncio.to_thread(self._helix, 'GET', 'users')
            user = identity['data'][0]
            self.user_id, self.user_name = user['id'], user['login']
            await self._listen()
        except Exception as exc:
            self.on_status(f'Twitch connection failed: {str(exc)[:180]}')
        finally:
            self.running = False

    def stop(self):
        self.running = False
