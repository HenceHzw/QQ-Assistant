import asyncio
import os
import sqlite3
import ssl
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

# Tests of application behavior do not require a live WebSocket library or QQ.
if 'websockets' not in sys.modules:
    try:
        import websockets  # noqa: F401
    except ImportError:
        sys.modules['websockets'] = types.ModuleType('websockets')

from hence.app import App
from hence.config import Settings
from hence.mail import poll_mailbox
from hence.graph import fetch_page, save_token
from hence.reminders import ZONE
from hence.store import Store


class StoreTests(unittest.TestCase):
    def test_legacy_database_migrates_without_losing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'db')
            db = sqlite3.connect(path)
            db.executescript('''
                CREATE TABLE outbox (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
                    text TEXT NOT NULL, created INTEGER NOT NULL, sent INTEGER, UNIQUE(kind, ref));
                INSERT INTO outbox(kind, ref, text, created) VALUES ('reply', 'old', '旧消息', 1);
                CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO kv VALUES ('pending_input', '旧数据');
            ''')
            db.commit()
            db.close()
            store = Store(path)
            self.assertEqual(store.pending_outbox()['text'], '旧消息')
            self.assertEqual(store.db.execute('SELECT value FROM kv').fetchone()[0], '旧数据')
            self.assertEqual(store.pending_outbox()['attempts'], 0)
            store.db.close()

    def test_restart_due_delivery_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'db')
            store = Store(path)
            with store.db:
                reminder_id = store.new_reminder('作业', 100)
            store.schedule_due(120)
            store.schedule_due(120)
            store.db.close()
            store = Store(path)
            row = store.pending_outbox()
            self.assertIn('作业', row['text'])
            store.mark_sent(row, 130)
            self.assertEqual(store.active_reminders()[0]['status'], 'notified')
            with store.db:
                self.assertTrue(store.finish_reminder(reminder_id, 'completed'))
            self.assertEqual(store.active_reminders(), [])
            self.assertIsNone(store.pending_outbox())
            store.db.close()

    def test_duplicate_events_and_mail_are_atomic(self):
        store = Store(':memory:')
        def operation():
            store.new_reminder('事项', 100)
        self.assertTrue(store.handle_event('event', operation))
        self.assertFalse(store.handle_event('event', operation))
        self.assertEqual(len(store.active_reminders()), 1)
        store.mailbox_baseline('qq', '10', 5)
        store.mailbox_message('qq', '10', 6, 'mail')
        store.mailbox_message('qq', '10', 6, 'mail')
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
        self.assertEqual(store.mailbox_state('qq')['last_uid'], 6)
        with self.assertRaises(ValueError):
            store.mailbox_message('qq', '11', 7, 'mail')

    def test_cancel_unsent_reminder(self):
        store = Store(':memory:')
        with store.db:
            rid = store.new_reminder('事', 100)
        store.schedule_due(120)
        with store.db:
            store.finish_reminder(rid, 'cancelled')
        self.assertIsNone(store.pending_outbox())

    def test_retry_does_not_block_other_messages_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'db')
            store = Store(path)
            with store.db:
                store.enqueue('reply', '1', 'first', now=100)
                store.enqueue('reply', '2', 'second', now=100)
            first = store.pending_outbox(now=100)
            store.defer_message(first, 100)
            store.db.close()
            store = Store(path)
            self.assertEqual(store.pending_outbox(now=101)['text'], 'second')
            self.assertEqual(store.pending_outbox(now=115)['text'], 'first')
            store.db.close()

    def test_mailbox_alert_once_then_recovery(self):
        store = Store(':memory:')
        for _ in range(4):
            store.mailbox_health('school', '学校邮箱', False)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
        self.assertIn('连续 3 次', store.pending_outbox()['text'])
        store.mailbox_health('school', '学校邮箱', True)
        store.mailbox_health('school', '学校邮箱', True)
        rows = store.db.execute('SELECT text FROM outbox ORDER BY id').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIn('已恢复', rows[1]['text'])


class FakeIMAP:
    validity = b'7'
    uids = b'1 2 3'
    fetched = []
    def __init__(self, *args, **kwargs):
        context = kwargs['ssl_context']
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def login(self, user, password): return 'OK', []
    def select(self, mailbox, readonly=False):
        assert readonly and mailbox == 'INBOX'
        return 'OK', []
    def response(self, name): return name, [self.validity]
    def uid(self, command, *args):
        if command == 'search': return 'OK', [self.uids]
        uid, query = args
        assert 'BODY.PEEK' in query
        self.fetched.append(int(uid))
        return 'OK', [(b'header', b'From: person@example.com\r\nSubject: Hello\r\n\r\n')]


class MailTests(unittest.TestCase):
    def test_baseline_and_read_only_incremental(self):
        box = {'id':'qq', 'host':'example.com', 'username':'x', 'password_env':'TEST_MAIL_SECRET'}
        FakeIMAP.fetched = []
        with patch.dict(os.environ, {'TEST_MAIL_SECRET':'secret'}), patch('hence.mail.imaplib.IMAP4_SSL', FakeIMAP):
            self.assertEqual(poll_mailbox(box, None), ('7', 3, None))
            self.assertEqual(FakeIMAP.fetched, [])
            validity, baseline, messages = poll_mailbox(box, {'uidvalidity':'7', 'last_uid':2})
            self.assertEqual((validity, baseline), ('7', None))
            self.assertEqual([m[0] for m in messages], [3])
            self.assertIn('Hello', messages[0][1])
            with self.assertRaises(RuntimeError):
                poll_mailbox(box, {'uidvalidity':'8', 'last_uid':2})


class GraphTests(unittest.TestCase):
    def test_delta_baseline_paging_new_mail_and_replay(self):
        store = Store(':memory:')
        old = {'id': 'old', 'subject': 'old', 'from': {'emailAddress': {'name': 'Alice'}}}
        new = {'id': 'new', 'subject': 'new', 'from': {'emailAddress': {'name': 'Bob'}}}
        store.graph_page('school', '学校邮箱', {'value': [old], '@odata.nextLink': 'page2'}, True)
        self.assertTrue(store.graph_state('school')['baseline'])
        store.graph_page('school', '学校邮箱', {'value': [], '@odata.deltaLink': 'delta'}, True)
        self.assertFalse(store.graph_state('school')['baseline'])
        store.graph_page('school', '学校邮箱', {'value': [old, new], '@odata.deltaLink': 'delta2'}, False)
        store.graph_page('school', '学校邮箱', {'value': [new], '@odata.deltaLink': 'delta3'}, False)
        rows = store.db.execute('SELECT * FROM outbox').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn('Bob', rows[0]['text'])
        self.assertEqual(store.graph_state('school')['cursor'], 'delta3')

    def test_cursor_origin_restricted_and_token_file_private(self):
        with self.assertRaises(ValueError):
            fetch_page({'token_file': 'not-accessed'}, 'https://attacker.example/steal')
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'token.json')
            save_token(path, {'access_token': 'secret'})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_page_validation_is_atomic(self):
        store = Store(':memory:')
        with self.assertRaises(ValueError):
            store.graph_page('school', '学校', {'value': [{'id': 'x'}]}, False)
        self.assertIsNone(store.graph_state('school'))
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM graph_seen').fetchone()[0], 0)


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        settings = Settings(123, 'ws://localhost:3001', 'token', ':memory:', (), '', '', '', 120)
        self.app = App(settings, self.store)

    def event(self, text, sender=123, message_id=1):
        return {'message_type':'private', 'user_id':sender, 'self_id':456, 'message_id':message_id,
                'message':[{'type':'text', 'data':{'text':text}}]}

    async def test_reject_other_user_and_duplicate_event(self):
        await self.app.accept_event(self.event('明天提醒我交作业', sender=111))
        self.assertTrue(self.app.events.empty())
        future = (datetime.now(ZONE) + timedelta(days=2)).strftime('%Y-%m-%d %H:%M')
        event = self.event(f'/add {future} 交作业')
        await self.app.accept_event(event)
        await self.app.handle_event(await self.app.commands.get())
        await self.app.handle_event(event)
        self.assertEqual(len(self.store.active_reminders()), 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)

    async def test_incomplete_rule_input_is_rejected_without_followup(self):
        await self.app.handle_event(self.event('提醒我交作业'))
        self.assertIn('/help', self.store.pending_outbox()['text'])
        await self.app.handle_event(self.event('明天', message_id=2))
        self.assertEqual(self.store.active_reminders(), [])

    async def test_dispatch_continues_after_one_message_is_rejected(self):
        with self.store.db:
            self.store.enqueue('reply', '1', '被拒绝的消息')
            self.store.enqueue('reply', '2', '后续消息')
        delivered = asyncio.Event()
        async def send(_user, text):
            if text == '被拒绝的消息':
                raise RuntimeError('rejected')
            delivered.set()
        self.app.bot.send = send
        task = asyncio.create_task(self.app.dispatch_loop())
        try:
            await asyncio.wait_for(delivered.wait(), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        rows = self.store.db.execute('SELECT text, sent, attempts FROM outbox ORDER BY id').fetchall()
        self.assertEqual(rows[0]['attempts'], 1)
        self.assertIsNone(rows[0]['sent'])
        self.assertIsNotNone(rows[1]['sent'])


class OneBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_waits_for_successful_onebot_ack(self):
        from hence.onebot import OneBot
        import json
        settings = Settings(123, 'ws://localhost:3001', 'token', ':memory:', (), '', '', '', 120)
        bot = OneBot(settings, None)
        class Socket:
            async def send(self, data):
                message = json.loads(data)
                self.message = message
                bot.pending[message['echo']].set_result({'status': 'ok', 'retcode': 0})
        socket = Socket()
        bot.socket = socket
        await bot.send(123, '提醒')
        self.assertEqual(socket.message['action'], 'send_private_msg')
        self.assertEqual(socket.message['params']['user_id'], 123)
        self.assertEqual(bot.pending, {})
        bot.socket = None
        with self.assertRaises(ConnectionError):
            await bot.send(123, '提醒')


if __name__ == '__main__':
    unittest.main()
