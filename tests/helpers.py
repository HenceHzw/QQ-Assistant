"""Shared assistant fixtures."""
import asyncio
import time
import unittest
from contextlib import asynccontextmanager, suppress
from unittest.mock import patch

from hence.app import App
from hence.config import Settings
from hence.store import Store, outbox_message


@asynccontextmanager
async def running(coroutine):
    task = asyncio.create_task(coroutine)
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def private_event(text, message_id, sender='test-owner'):
    return {'message_type': 'private', 'user_id': sender, 'message_id': message_id,
            'message': text, 'timestamp': int(time.time())}


class AssistantTestCase(unittest.IsolatedAsyncioTestCase):
    harness_url = 'http://harness:8080/turn'

    async def asyncSetUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.db.close)
        self.app = App(Settings('test-owner', '123456', ':memory:', (), 120,
                                self.harness_url, 'shared'), self.store)
        async def call_inline(func, *args, **kwargs):
            return func(*args, **kwargs)
        self.enterContext(patch('hence.app.asyncio.to_thread', new=call_inline))

    async def say(self, text, message_id):
        await self.app.handle_event(private_event(text, message_id))

    async def model_turn(self, text, message_id, **action):
        with patch('hence.app.ask_harness', return_value=action):
            await self.say(text, message_id)

    def add_task(self, title, due=2000000000):
        with self.store.db:
            return self.store.new_reminder(title, due)

    def last_reply(self):
        return self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text']

    def pending_message(self, kind):
        row = self.store.db.execute(
            'SELECT * FROM outbox WHERE kind=? AND sent IS NULL ORDER BY id DESC', (kind,)
        ).fetchone()
        return outbox_message(row)

    def deliver(self, kind):
        message = self.pending_message(kind)
        self.assertIsNotNone(message)
        self.store.mark_sent(message, int(time.time()))

    async def view_tasks(self, message_id):
        await self.say('/list', message_id)
        self.deliver('task_list')

    async def confirm(self, message_id):
        self.deliver('confirm_preview')
        await self.model_turn('确认', message_id, action='confirm')
