import asyncio
import errno
import io
import json
import os
import sqlite3
import ssl
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import DEFAULT, AsyncMock, Mock, call, patch
from urllib.error import URLError

from hence.app import App
from hence.config import Settings
from hence.qqbot import BotAPIError, NoRedirect, QQBot, bind_owner, request_json
from hence.store import Store, outbox_message
from helpers import private_event, running


class BridgeClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_binding_waits_for_plugin_startup_and_qq_connection(self):
        ready = {'app_id': '123456', 'configured': True, 'connected': True}
        request = AsyncMock(side_effect=[URLError(ConnectionRefusedError()),
            {**ready, 'connected': False}, ready, {'code': 'test-code'}, {'owner': 'test-owner'}])
        output = io.StringIO()
        with patch.object(QQBot, 'request', request), patch('hence.qqbot.asyncio.sleep', new_callable=AsyncMock) as sleep, patch('sys.stdout', output):
            await bind_owner('123456', 'shared', 'http://harness:8081')
        self.assertEqual([call.args[1] for call in request.call_args_list],
                         ['/status', '/status', '/status', '/bind', '/bind'])
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(output.getvalue().count('正在等待'), 1)
        self.assertIn('/bind test-code', output.getvalue())
        self.assertIn('QQ_BOT_OWNER_OPENID=test-owner', output.getvalue())

    async def test_binding_stops_waiting_and_does_not_request_a_code_if_not_ready(self):
        request = AsyncMock(return_value={'app_id': '123456', 'configured': True, 'connected': False})
        clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 31]))
        with patch.object(QQBot, 'request', request), patch('hence.qqbot.time', clock):
            with self.assertRaisesRegex(ValueError, '30 秒'):
                await bind_owner('123456', 'shared', 'http://harness:8081')
        self.assertEqual([call.args[1] for call in request.call_args_list], ['/status'])

    async def test_binding_reports_configuration_and_authentication_errors_without_retry(self):
        for response in ({'app_id': 'other'}, {'app_id': '123456', 'blocked': True}, BotAPIError(403)):
            with self.subTest(response=response):
                request = AsyncMock(side_effect=response) if isinstance(response, Exception) else AsyncMock(return_value=response)
                with patch.object(QQBot, 'request', request), patch('hence.qqbot.asyncio.sleep', new_callable=AsyncMock) as sleep:
                    with self.assertRaises((ValueError, BotAPIError)):
                        await bind_owner('123456', 'shared', 'http://harness:8081')
                self.assertEqual(request.await_count, 1)
                sleep.assert_not_awaited()

    async def test_durable_acceptance_precedes_acknowledgment(self):
        accepted, acked = asyncio.Event(), asyncio.Event()
        event = private_event('/help', 'message-1')
        async def accept(value):
            self.assertEqual(value, event)
            accepted.set()
        bot = QQBot('123456', 'test-owner', accept, 'shared')
        async def request(method, path, payload=None):
            if path == '/status': return {'app_id': '123456', 'owner_openid': 'test-owner', 'connected': True, 'blocked': False}
            if path == '/events': return {'events': [{'key': 'key', 'event': event}]}
            self.assertEqual(path, '/ack')
            self.assertTrue(accepted.is_set())
            acked.set()
            return {'ok': True}
        bot.request = request
        async with running(bot.run()):
            await asyncio.wait_for(acked.wait(), 1)

    async def test_backpressure_does_not_acknowledge_input(self):
        checked = asyncio.Event()
        async def accept(event):
            checked.set()
            return False
        bot = QQBot('123456', 'test-owner', accept, 'shared')
        bot.request = AsyncMock(side_effect=[{'app_id': '123456', 'owner_openid': 'test-owner'},
            {'events': [{'key': 'key', 'event': private_event('/help', 'message-1')}]}])
        async with running(bot.run()):
            await asyncio.wait_for(checked.wait(), 1)
        self.assertEqual([call.args[1] for call in bot.request.call_args_list], ['/status', '/events'])

    def test_upstream_error_metadata_is_preserved_without_private_body(self):
        response = io.BytesIO(json.dumps({'error': {'status': 403, 'code': 40034024,
            'message': 'private text'}}).encode())
        response.code = 502
        with patch('hence.qqbot.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(BotAPIError) as caught:
                request_json('POST', 'http://harness:8081/send', 'shared', {})
        self.assertEqual((caught.exception.status, caught.exception.code), (403, '40034024'))
        self.assertNotIn('private', str(caught.exception))
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example'))


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, path=':memory:', app_id='123456'):
        return Settings('test-owner', app_id, path, (), 120)

    def open_app(self):
        store = Store(':memory:')
        self.addCleanup(store.db.close)
        return App(self.settings(), store)

    async def test_input_failure_rolls_back_and_worker_processes_next_message(self):
        app = self.open_app()
        store = app.store
        original = app.handle_event
        bad = private_event('/add broken', 'bad')
        good = private_event('/help', 'good')
        async def handle(event):
            if event['message_id'] != 'bad':
                return await original(event)
            def fail():
                store.new_reminder('应回滚', 100)
                raise ValueError('private text must not be logged')
            store.handle_event(app.event_id(event), fail)
        app.handle_event = handle
        await app.accept_event(bad)
        await app.accept_event(good)
        with self.assertLogs('hence.app', level='ERROR') as logs:
            async with running(app.event_loop(app.commands)):
                await asyncio.wait_for(app.commands.join(), 1)
        self.assertNotIn('private text', str(logs.output))
        self.assertEqual(store.active_reminders(), [])
        self.assertEqual(store.queued_events(), [])
        rows = store.db.execute('SELECT * FROM outbox ORDER BY id').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIn('请重新发送', rows[0]['text'])
        self.assertEqual(rows[0]['reply_to'], 'bad')
        self.assertIn('/help', rows[1]['text'])
        await app.accept_event(bad)
        self.assertTrue(app.commands.empty())

    async def test_database_lock_retries_the_same_message_and_reports_persistent_failure(self):
        for failures, delays, feedback in [(1, [1], '/help'), (3, [1, 2], '请重新发送')]:
            with self.subTest(failures=failures):
                app = self.open_app()
                event = private_event('/help', 'locked')
                await app.accept_event(event)
                busy = sqlite3.OperationalError('database is locked')
                busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
                # After the simulated lock clears, let the real handler finish the command.
                app.handle_event = AsyncMock(wraps=app.handle_event, side_effect=[busy] * failures + [DEFAULT])
                with patch('hence.app.asyncio.sleep', new_callable=AsyncMock) as sleep:
                    async with running(app.event_loop(app.commands)):
                        await asyncio.wait_for(app.commands.join(), 1)
                self.assertEqual(app.handle_event.await_args_list, [call(event)] * (len(delays) + 1))
                self.assertEqual(sleep.await_args_list, [call(delay) for delay in delays])
                self.assertEqual(app.store.queued_events(), [])
                replies = app.store.db.execute('SELECT text FROM outbox').fetchall()
                self.assertEqual(len(replies), 1)
                self.assertIn(feedback, replies[0]['text'])

    async def test_failure_to_record_response_preserves_input_for_restart(self):
        app = self.open_app()
        store = app.store
        event = private_event('/help', 'message-1')
        await app.accept_event(event)
        app.handle_event = AsyncMock(side_effect=ValueError('broken handler'))
        with patch.object(store, 'fail_incoming', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertLogs('hence.app', level='ERROR'), self.assertRaises(sqlite3.OperationalError):
                await app.event_loop(app.commands)
        self.assertEqual(store.queued_events(), [event])
        self.assertFalse(store.seen(app.event_id(event)))

    async def test_delivery_classifies_failures_without_losing_later_messages(self):
        cases = [
            (BotAPIError(400, 40054005), False), (BotAPIError(403, 40034024), False),
            (RuntimeError('rejected'), False), (BotAPIError(401), False), (ssl.SSLCertVerificationError('invalid cert'), False),
            (BotAPIError(503), True), (TimeoutError(), True),
            (URLError(ConnectionResetError()), True), (URLError(OSError(errno.ENETUNREACH, 'offline')), True),
        ]
        for error, retry in cases:
            with self.subTest(error=repr(error)):
                app = self.open_app()
                store = app.store
                with store.db:
                    store.enqueue('reply', 'first', 'first')
                    store.enqueue('reply', 'second', 'second')
                delivered = asyncio.Event()
                sent = []
                async def send(text, **context):
                    sent.append(text)
                    if text == 'first':
                        raise error
                    delivered.set()
                app.bot.send = send
                with patch('hence.app.SEND_INTERVAL', 0), self.assertLogs('hence.app'):
                    async with running(app.dispatch_loop()):
                        await asyncio.wait_for(delivered.wait(), 1)
                self.assertEqual(sent, ['first', 'second'])
                row = outbox_message(store.db.execute("SELECT * FROM outbox WHERE ref='first'").fetchone())
                self.assertEqual(row['attempts'], 1)
                self.assertIsNone(row['sent'])
                self.assertEqual(row['paused_reason'] is None, retry)
                self.assertIsNone(store.pending_outbox())
                self.assertEqual(store.pending_outbox(int(time.time()) + 86400) is not None, retry)

    async def test_restart_retries_paused_notifications_but_keeps_cancellations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'test.db')
            store = Store(path)
            App(self.settings(path), store)
            with store.db:
                rid = store.new_reminder('作业', 100)
                store.new_date_reminder('体测', 400, 0, 500, [100, 110])
            store.schedule_due(120)
            store.pause_message(store.pending_outbox(120), 'denied')
            date = store.pending_outbox(120)
            self.assertEqual(len(date['batch']), 2)
            store.pause_message(date, 'denied')
            self.assertIsNone(store.pending_outbox(1000))
            with store.db:
                store.finish_reminder(rid, 'cancelled')
            store.db.close()
            store = Store(path)
            self.addCleanup(store.db.close)
            self.assertIsNone(store.pending_outbox(1000))
            App(self.settings(path), store)
            resumed = store.pending_outbox(120)
            self.assertEqual(resumed['batch'], date['batch'])
            store.mark_sent(resumed, 120)
            store.schedule_due(1000)
            self.assertIsNone(store.pending_outbox(1000))

    async def test_queued_command_and_reply_context_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'test.db')
            store = Store(path)
            app = App(self.settings(path), store)
            event = private_event('/help', 'message-1')
            await app.accept_event(event)
            self.assertEqual(len(store.queued_events()), 1)
            store.db.close()
            store = Store(path)
            self.addCleanup(store.db.close)
            app = App(self.settings(path), store)
            await app.recover_incoming()
            await app.handle_event(await app.commands.get())
            await app.accept_event(event)
            self.assertTrue(app.commands.empty())
            self.assertEqual(store.queued_events(), [])
            row = store.pending_outbox()
            self.assertEqual(row['reply_to'], event['message_id'])
            self.assertEqual(row['reply_at'], event['timestamp'])
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)

    async def test_channel_change_preserves_mail_and_reminders_but_discards_old_commands_and_views(self):
        app = self.open_app()
        store = app.store
        await app.accept_event(private_event('/help', 'old-command'))
        self.assertTrue(store.queued_events())
        with store.db:
            rid = store.new_reminder('作业', 100)
            store.enqueue_list('task_list', 'old-list', store.active_reminders(), '列表')
            store.enqueue('reply', 'old-reply', '旧回复')
        store.mark_sent(store.pending_outbox(), 101)
        store.mailbox_baseline('personal', '1', 20)
        store.mailbox_message('personal', '1', 21, '新邮件')
        store.schedule_due(120)
        App(self.settings(app_id='654321'), store)
        self.assertEqual(store.queued_events(), [])
        self.assertEqual(store.active_reminders()[0]['id'], rid)
        self.assertEqual(store.mailbox_state('personal')['last_uid'], 21)
        self.assertFalse(store.positions_match('task', store.active_reminders(), [1]))
        pending = store.db.execute('SELECT * FROM outbox WHERE sent IS NULL ORDER BY id').fetchall()
        self.assertEqual([r['kind'] for r in pending], ['mail', 'reminder'])
        self.assertTrue(all(r['reply_to'] is None for r in pending))

    async def test_accept_event_never_persists_other_users_private_text(self):
        app = self.open_app()
        store = app.store
        for sender in ('stranger', 999):
            await app.accept_event(private_event('/list', 'message-1', sender))
        self.assertEqual(store.queued_events(), [])
        self.assertTrue(app.commands.empty())

    async def test_delivery_waits_for_receipt_and_observes_rate_limit(self):
        app = self.open_app()
        store = app.store
        await app.handle_event(private_event('/help', 'message-1'))
        failed = asyncio.Event()
        async def send(text, **context):
            self.assertEqual(context['reply_to'], 'message-1')
            failed.set()
            raise BotAPIError(429, 22009)
        app.bot.send = send
        async with running(app.dispatch_loop()):
            await asyncio.wait_for(failed.wait(), 1)
        row = outbox_message(store.db.execute('SELECT * FROM outbox').fetchone())
        self.assertIsNone(row['sent'])
        self.assertEqual(row['attempts'], 1)
        self.assertGreater(row['retry_at'], time.time() + 58)
        self.assertGreater(app.next_send, time.monotonic() + 58)

    async def test_cancellation_before_or_during_send_still_delivers_the_reply(self):
        for stage in ('cooldown', 'sending'):
            with self.subTest(stage=stage):
                app = self.open_app()
                store = app.store
                with store.db:
                    rid = store.new_reminder('体测', 100)
                store.schedule_due(120)
                reminder_text = store.pending_outbox()['text']
                waiting, resume, replied = asyncio.Event(), asyncio.Event(), asyncio.Event()
                sent = []

                async def pause():
                    waiting.set()
                    await resume.wait()

                async def send(text, **context):
                    sent.append(text)
                    if text == '已取消体测':
                        replied.set()
                    elif stage == 'sending':
                        await pause()

                if stage == 'cooldown':
                    app.wait_to_send = pause
                app.bot.send = send
                with patch('hence.app.SEND_INTERVAL', 0):
                    async with running(app.dispatch_loop()):
                        await asyncio.wait_for(waiting.wait(), 1)
                        with store.db:
                            store.finish_reminder(rid, 'cancelled')
                            store.enqueue('reply', 'cancel-event', '已取消体测')
                        resume.set()
                        await asyncio.wait_for(replied.wait(), 1)
                expected = [] if stage == 'cooldown' else [reminder_text]
                self.assertEqual(sent, [*expected, '已取消体测'])

    def test_config_requires_bridge_auth_and_owner_but_no_app_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'mailboxes.json'
            config.write_text('[]')
            env = {'QQ_BOT_APP_ID': '123456', 'HARNESS_AUTH_TOKEN': 'test-token',
                   'QQ_BOT_OWNER_OPENID': 'test-owner', 'MAIL_CONFIG': str(config)}
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.load()
                self.assertNotIn('test-token', repr(settings))
            for key in ('QQ_BOT_APP_ID', 'HARNESS_AUTH_TOKEN', 'QQ_BOT_OWNER_OPENID'):
                with self.subTest(key=key), patch.dict(os.environ, {**env, key: ''}, clear=True), self.assertRaises(ValueError):
                    Settings.load()


if __name__ == '__main__':
    unittest.main()
