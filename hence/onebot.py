import asyncio
import json
import logging
import uuid

import websockets

log = logging.getLogger(__name__)


class OneBot:
    def __init__(self, settings, on_event):
        self.settings = settings
        self.on_event = on_event
        self.socket = None
        self.pending = {}

    async def run(self):
        while True:
            try:
                async with websockets.connect(self.settings.onebot_url,
                    additional_headers={'Authorization': f'Bearer {self.settings.onebot_token}'},
                    ping_interval=20, ping_timeout=20, max_size=1024 * 1024) as ws:
                    self.socket = ws
                    log.info('OneBot connected')
                    async for raw in ws:
                        try:
                            item = json.loads(raw)
                            echo = item.get('echo')
                            if echo and echo in self.pending:
                                future = self.pending.pop(echo)
                                if not future.done():
                                    future.set_result(item)
                            elif item.get('post_type') == 'message':
                                await self.on_event(item)
                        except (ValueError, TypeError):
                            log.warning('Ignoring invalid OneBot message')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('OneBot disconnected: %s', type(exc).__name__)
            finally:
                self.socket = None
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError('OneBot disconnected'))
                self.pending.clear()
            await asyncio.sleep(5)

    async def send(self, user_id, text):
        if not self.socket:
            raise ConnectionError('OneBot disconnected')
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await self.socket.send(json.dumps({'action': 'send_private_msg', 'params': {
                'user_id': user_id, 'message': [{'type': 'text', 'data': {'text': text}}]}, 'echo': echo}, ensure_ascii=False))
            reply = await asyncio.wait_for(future, 15)
            if reply.get('status') != 'ok' or reply.get('retcode') != 0:
                raise RuntimeError('OneBot rejected message')
        finally:
            self.pending.pop(echo, None)
