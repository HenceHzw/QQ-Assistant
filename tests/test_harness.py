import json
import io
import asyncio
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

try:
    import websockets  # noqa: F401
except ImportError:
    import types
    sys.modules['websockets'] = types.ModuleType('websockets')

from hence.app import App
from hence.config import Settings
from hence.harness import ask_harness
from hence.reminders import ZONE, validate_reminder
from hence.store import Store


class HarnessActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        settings = Settings(123, 'ws://localhost', 'onebot', ':memory:', (), 120,
                            'http://harness:8080/turn', 'shared')
        self.app = App(settings, self.store)
        async def run_mocked_call(func, *args, **kwargs):
            return func(*args, **kwargs)
        thread_patch = patch('hence.app.asyncio.to_thread', new=run_mocked_call)
        thread_patch.start()
        self.addCleanup(thread_patch.stop)

    def event(self, text, message_id):
        return {'message_type':'private', 'user_id':123, 'self_id':456, 'message_id':message_id,
                'message':[{'type':'text','data':{'text':text}}]}

    async def confirm(self, message_id):
        preview = self.store.db.execute("SELECT * FROM outbox WHERE kind='confirm_preview' AND sent IS NULL ORDER BY id DESC").fetchone()
        self.store.mark_sent(preview, int(datetime.now().timestamp()))
        with patch('hence.app.ask_harness', return_value={'action': 'confirm'}):
            await self.app.handle_event(self.event('确认', message_id))

    def deliver_list(self, kind='task_list'):
        row = self.store.db.execute('SELECT * FROM outbox WHERE kind=? AND sent IS NULL ORDER BY id DESC', (kind,)).fetchone()
        self.assertIsNotNone(row)
        self.store.mark_sent(row, int(datetime.now().timestamp()))

    async def cancel_via_model(self, number, message_id):
        with patch('hence.app.ask_harness', return_value={'action': 'cancel', 'ids': [number]}):
            await self.app.handle_event(self.event('取消那项', message_id))

    async def test_chat_and_create_are_saved_once(self):
        result = {'action':'chat','reply':'你好，我会记住你喜欢蓝色。'}
        with patch('hence.app.ask_harness', return_value=result) as ask:
            await self.app.handle_event(self.event('我喜欢蓝色', 1))
            self.assertIn('蓝色', self.store.pending_outbox()['text'])
            self.assertEqual(ask.call_args.args[2], '我喜欢蓝色')
        now = datetime.now(ZONE)
        due = datetime.fromtimestamp(now.timestamp() + 3600, ZONE).isoformat()
        with patch('hence.app.ask_harness', return_value={'action':'create','title':'开会','due':due}):
            event = self.event('一小时后提醒我开会', 2)
            await self.app.handle_event(event)
            await self.app.handle_event(event)
        self.assertEqual(len(self.store.active_reminders()), 1)
        self.assertEqual(self.store.active_reminders()[0]['title'], '开会')

    async def test_model_cannot_complete_missing_task(self):
        with patch('hence.app.ask_harness', return_value={'action':'complete','id':99}) as ask:
            await self.app.handle_event(self.event('我完成了那件事', 1))
            ask.assert_called_once()
        self.assertIn('无法确定', self.store.pending_outbox()['text'])

    async def test_named_cancellation_needs_confirmation_but_no_prior_list(self):
        with self.store.db:
            self.store.new_reminder('体测', 2000000000)
        with patch('hence.app.ask_harness', return_value={'action': 'cancel', 'ids': [1], 'reference': 'description'}):
            await self.app.handle_event(self.event('取消体测', 1))
        self.assertEqual(len(self.store.active_reminders()), 1)
        preview = self.store.pending_outbox()
        self.assertEqual(preview['kind'], 'confirm_preview')
        self.assertIn('体测', preview['text'])
        await self.confirm(2)
        self.assertEqual(self.store.active_reminders(), [])

    async def test_named_batch_keeps_targets_when_unrelated_task_changes_positions(self):
        with self.store.db:
            self.store.new_reminder('数学作业', 2000000000)
            self.store.new_reminder('英语作业', 2000000100)
        def model(*args):
            with self.store.db:
                self.store.new_reminder('体测', 1999999900)
            return {'action': 'complete', 'ids': [1, 2], 'reference': 'description'}
        with patch('hence.app.ask_harness', side_effect=model):
            await self.app.handle_event(self.event('数学和英语作业都做完了', 1))
        pending = self.store.db.execute('SELECT targets FROM pending_confirmation').fetchone()
        self.assertEqual([row['title'] for row in json.loads(pending['targets'])], ['数学作业', '英语作业'])
        await self.confirm(2)
        self.assertEqual([row['title'] for row in self.store.active_reminders()], ['体测'])

    async def test_named_batch_rejected_if_target_changes_during_model_call(self):
        with self.store.db:
            self.store.new_reminder('数学作业', 2000000000)
            rid = self.store.new_reminder('英语作业', 2000000100)
        def model(*args):
            with self.store.db:
                self.store.finish_reminder(rid, 'completed')
            return {'action': 'cancel', 'ids': [1, 2], 'reference': 'description'}
        with patch('hence.app.ask_harness', side_effect=model):
            await self.app.handle_event(self.event('取消数学和英语作业', 1))
        self.assertEqual([row['title'] for row in self.store.active_reminders()], ['数学作业'])
        self.assertIn('相关事项已变化', self.store.pending_outbox()['text'])
        self.assertIsNone(self.store.db.execute('SELECT * FROM pending_confirmation').fetchone())

    async def test_position_reference_still_requires_delivered_list(self):
        with self.store.db:
            self.store.new_reminder('体测', 2000000000)
        with patch('hence.app.ask_harness', return_value={'action': 'cancel', 'ids': [1], 'reference': 'position'}):
            await self.app.handle_event(self.event('取消第一个', 1))
        self.assertIn('先发送 /list', self.store.pending_outbox()['text'])
        self.assertIsNone(self.store.db.execute('SELECT * FROM pending_confirmation').fetchone())

    async def test_unknown_reference_mode_is_rejected(self):
        with self.store.db:
            self.store.new_reminder('体测', 2000000000)
        with patch('hence.app.ask_harness', return_value={'action': 'cancel', 'ids': [1], 'reference': 'unknown'}):
            await self.app.handle_event(self.event('取消体测', 1))
        self.assertEqual(len(self.store.active_reminders()), 1)
        self.assertIn('无法确定', self.store.pending_outbox()['text'])
        self.assertIsNone(self.store.db.execute('SELECT * FROM pending_confirmation').fetchone())

    async def test_batch_cancel_and_dynamic_numbering(self):
        with self.store.db:
            first = self.store.new_reminder('上课', 2000000100)
            second = self.store.new_reminder('作业', 2000000000)
        await self.app.handle_event(self.event('/list', 1))
        self.assertIn('1. 作业', self.store.pending_outbox()['text'])
        self.deliver_list()
        with patch('hence.app.ask_harness', return_value={'action': 'cancel', 'ids': [1, 2]}):
            await self.app.handle_event(self.event('取消这两个待办', 2))
        self.assertEqual(len(self.store.active_reminders()), 2)
        response = self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text']
        self.assertIn('请核对将要取消的 2 项', response)
        self.assertIn('1. 作业', response)
        self.assertIn('2. 上课', response)
        await self.confirm(4)
        self.assertEqual(self.store.active_reminders(), [])
        with self.store.db:
            self.store.new_reminder('吃饭', 2000000200)
        await self.app.handle_event(self.event('/list', 3))
        response = self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text']
        self.assertIn('1. 吃饭', response)

    async def test_stale_list_prevents_wrong_cancellation(self):
        with self.store.db:
            first = self.store.new_reminder('上课', 2000000100)
        await self.app.handle_event(self.event('/list', 1))
        self.deliver_list()
        with self.store.db:
            self.store.new_reminder('作业', 2000000000)
        await self.app.handle_event(self.event('/cancel 1', 2))
        self.assertEqual(len(self.store.active_reminders()), 2)
        response = self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text']
        self.assertIn('先发送 /list', response)

    async def test_fixed_command_requires_delivered_list_then_executes_directly(self):
        with self.store.db:
            self.store.new_reminder('上课', 2000000100)
        await self.app.handle_event(self.event('/list', 1))
        await self.app.handle_event(self.event('/cancel 1', 2))
        self.assertEqual(len(self.store.active_reminders()), 1)
        self.deliver_list()
        await self.app.handle_event(self.event('/cancel 1', 3))
        self.assertEqual(self.store.active_reminders(), [])
        self.assertIn('已取消待办：上课', self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text'])
        self.assertIsNone(self.store.db.execute('SELECT * FROM pending_confirmation').fetchone())

    async def test_old_inflight_list_ack_does_not_authorize_changed_list(self):
        with self.store.db:
            self.store.new_reminder('上课', 2000000100)
        await self.app.handle_event(self.event('/list', 1))
        old = self.store.db.execute("SELECT * FROM outbox WHERE kind='task_list'").fetchone()
        with self.store.db:
            self.store.new_reminder('体测', 2000000000)
        await self.app.handle_event(self.event('/list', 2))
        now = int(datetime.now().timestamp())
        self.store.mark_sent(old, now)
        await self.app.handle_event(self.event('/cancel 1', 3))
        self.assertEqual(len(self.store.active_reminders()), 2)
        self.assertIn('先发送 /list', self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text'])
        self.deliver_list()
        await self.app.handle_event(self.event('/cancel 1', 4))
        self.assertEqual([row['title'] for row in self.store.active_reminders()], ['上课'])

    async def test_old_list_retry_is_suppressed_after_new_list(self):
        with self.store.db:
            self.store.new_reminder('上课', 2000000100)
        await self.app.handle_event(self.event('/list', 1))
        now = int(datetime.now().timestamp())
        old = self.store.pending_outbox(now)
        self.store.defer_message(old, now)
        with self.store.db:
            self.store.new_reminder('体测', 2000000000)
        await self.app.handle_event(self.event('/list', 2))
        new = self.store.pending_outbox(now)
        self.assertIn('1. 体测', new['text'])
        self.store.mark_sent(new, now)
        self.assertIsNone(self.store.pending_outbox(now + 20))
        await self.app.handle_event(self.event('/cancel 1', 3))
        reply = self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text']
        self.assertIn('已取消待办：体测', reply)
        self.assertEqual([r['title'] for r in self.store.active_reminders()], ['上课'])

    async def test_incomplete_model_batch_is_visible_before_any_action(self):
        with self.store.db:
            self.store.new_reminder('上课', 2000000000)
            self.store.new_reminder('体测', 2000000100)
        await self.app.handle_event(self.event('/list', 1))
        self.deliver_list()
        with patch('hence.app.ask_harness', return_value={'action': 'cancel', 'ids': [1]}):
            await self.app.handle_event(self.event('取消这两项', 2))
        preview = self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text']
        self.assertIn('1 项', preview)
        self.assertIn('上课', preview)
        self.assertNotIn('体测', preview)
        self.assertEqual(len(self.store.active_reminders()), 2)

    async def test_natural_confirmation_and_discard(self):
        with self.store.db:
            self.store.new_reminder('上课', 2000000000)
            self.store.new_reminder('体测', 2000000100)
        await self.app.handle_event(self.event('/list', 1))
        self.deliver_list()
        await self.cancel_via_model(1, 2)
        preview = self.store.db.execute("SELECT * FROM outbox WHERE kind='confirm_preview' ORDER BY id DESC").fetchone()
        self.store.mark_sent(preview, int(datetime.now().timestamp()))
        with patch('hence.app.ask_harness', return_value={'action': 'discard_confirmation'}) as ask:
            await self.app.handle_event(self.event('算了，先别取消', 3))
        self.assertEqual(len(self.store.active_reminders()), 2)
        self.assertEqual(ask.call_args.args[6]['targets'][0]['title'], '上课')
        self.assertNotIn('token', ask.call_args.args[6])
        await self.cancel_via_model(1, 4)
        preview = self.store.db.execute("SELECT * FROM outbox WHERE kind='confirm_preview' AND sent IS NULL ORDER BY id DESC").fetchone()
        self.store.mark_sent(preview, int(datetime.now().timestamp()))
        with patch('hence.app.ask_harness', return_value={'action': 'confirm'}):
            await self.app.handle_event(self.event('是的，取消吧', 5))
        self.assertEqual([row['title'] for row in self.store.active_reminders()], ['体测'])

    async def test_no_preview_or_undelivered_preview_cannot_be_confirmed(self):
        with self.store.db:
            self.store.new_reminder('上课', 2000000000)
        with patch('hence.app.ask_harness', return_value={'action': 'confirm'}):
            await self.app.handle_event(self.event('确认', 1))
        self.assertEqual(len(self.store.active_reminders()), 1)
        await self.app.handle_event(self.event('/list', 2))
        self.deliver_list()
        await self.cancel_via_model(1, 3)
        with patch('hence.app.ask_harness', return_value={'action': 'confirm'}):
            await self.app.handle_event(self.event('确认', 4))
        self.assertEqual(len(self.store.active_reminders()), 1)
        self.assertIn('没有已送达', self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text'])
        await self.confirm(5)
        self.assertEqual(self.store.active_reminders(), [])

    async def test_confirmation_is_bound_to_snapshot_during_model_call(self):
        with self.store.db:
            self.store.new_reminder('上课', 2000000000)
            self.store.new_reminder('体测', 2000000100)
        await self.app.handle_event(self.event('/list', 1))
        self.deliver_list()
        await self.cancel_via_model(1, 2)
        old = self.store.db.execute("SELECT * FROM outbox WHERE kind='confirm_preview' AND sent IS NULL").fetchone()
        self.store.mark_sent(old, int(datetime.now().timestamp()))
        async def change_during_model(*args, **kwargs):
            with self.store.db:
                self.app.prepare_confirmation('cancelled', [self.store.active_reminders()[1]])
            new = self.store.db.execute("SELECT * FROM outbox WHERE kind='confirm_preview' AND sent IS NULL").fetchone()
            self.store.mark_sent(new, int(datetime.now().timestamp()))
            return {'action': 'confirm'}
        with patch('hence.app.asyncio.to_thread', side_effect=change_during_model):
            await self.app.handle_event(self.event('确认', 3))
        self.assertEqual(len(self.store.active_reminders()), 2)
        self.assertIn('已变化', self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text'])

    async def test_model_failure_offers_fixed_command_and_invalidates_preview(self):
        with self.store.db:
            self.store.new_reminder('上课', 2000000000)
        await self.app.handle_event(self.event('/list', 1))
        self.deliver_list()
        await self.cancel_via_model(1, 2)
        preview = self.store.db.execute("SELECT * FROM outbox WHERE kind='confirm_preview' AND sent IS NULL").fetchone()
        self.store.mark_sent(preview, int(datetime.now().timestamp()))
        with patch('hence.app.ask_harness', side_effect=TimeoutError()):
            await self.app.handle_event(self.event('确认', 3))
        self.assertIn('/list', self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text'])
        self.assertEqual(len(self.store.active_reminders()), 1)
        await self.app.handle_event(self.event('/cancel 1', 4))
        self.assertEqual(self.store.active_reminders(), [])
        self.assertIsNone(self.store.confirmation_snapshot())
        with patch('hence.app.ask_harness', return_value={'action': 'confirm'}):
            await self.app.handle_event(self.event('确认', 5))
        self.assertIn('没有已送达', self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text'])

    async def test_fixed_command_invalidates_unsent_natural_preview(self):
        with self.store.db:
            self.store.new_reminder('上课', 2000000000)
            self.store.new_reminder('体测', 2000000100)
        await self.app.handle_event(self.event('/list', 1))
        self.deliver_list()
        await self.cancel_via_model(1, 2)
        preview = self.store.db.execute("SELECT * FROM outbox WHERE kind='confirm_preview' AND sent IS NULL").fetchone()
        self.assertIsNotNone(preview)
        await self.app.handle_event(self.event('/cancel 2', 3))
        self.assertEqual([row['title'] for row in self.store.active_reminders()], ['上课'])
        self.assertIsNone(self.store.db.execute("SELECT * FROM outbox WHERE kind='confirm_preview' AND sent IS NULL").fetchone())
        # A preview already in flight can still be acknowledged, but must not be actionable.
        self.store.mark_sent(preview, int(datetime.now().timestamp()))
        with patch('hence.app.ask_harness', return_value={'action': 'confirm'}):
            await self.app.handle_event(self.event('确认', 4))
        self.assertEqual([row['title'] for row in self.store.active_reminders()], ['上课'])

    async def test_replaced_confirmation_and_changed_target_do_not_execute(self):
        with self.store.db:
            first = self.store.new_reminder('上课', 2000000000)
            self.store.new_reminder('体测', 2000000100)
        await self.app.handle_event(self.event('/list', 1))
        self.deliver_list()
        await self.cancel_via_model(1, 2)
        old = self.store.db.execute("SELECT * FROM outbox WHERE kind='confirm_preview' ORDER BY id DESC").fetchone()
        await self.cancel_via_model(2, 3)
        new = self.store.db.execute('SELECT token FROM pending_confirmation').fetchone()['token']
        new_preview = self.store.db.execute("SELECT * FROM outbox WHERE kind='confirm_preview' AND ref=?", (new,)).fetchone()
        now = int(datetime.now().timestamp())
        self.store.mark_sent(new_preview, now)
        self.store.mark_sent(old, now)
        with patch('hence.app.ask_harness', return_value={'action': 'confirm'}):
            await self.app.handle_event(self.event('确认', 4))
        self.assertEqual(len(self.store.active_reminders()), 2)
        self.assertEqual(self.store.db.execute('SELECT token FROM pending_confirmation').fetchone()['token'], new)
        self.store.mark_sent(new_preview, now)
        with self.store.db:
            self.store.db.execute("UPDATE reminders SET title='其他' WHERE id=?", (2,))
        with patch('hence.app.ask_harness', return_value={'action': 'confirm'}):
            await self.app.handle_event(self.event('确认', 5))
        self.assertEqual(len(self.store.active_reminders()), 2)
        self.assertIn('未执行', self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text'])

    async def test_expired_confirmation_and_unknown_truncated_id_are_rejected(self):
        with self.store.db:
            self.store.new_reminder('上课', 2000000000)
            self.store.new_reminder('体测', 2000000100)
        await self.app.handle_event(self.event('/list', 1))
        self.deliver_list()
        await self.cancel_via_model(1, 2)
        preview = self.store.db.execute("SELECT * FROM outbox WHERE kind='confirm_preview'").fetchone()
        self.store.mark_sent(preview, int(datetime.now().timestamp()))
        with self.store.db:
            self.store.db.execute('UPDATE pending_confirmation SET expires=0')
        with patch('hence.app.ask_harness', return_value={'action': 'confirm'}):
            await self.app.handle_event(self.event('确认', 3))
        self.assertIn('没有已送达', self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text'])
        with patch('hence.app.ask_harness', return_value={'action': 'cancel', 'ids': [2], '_tasks_sent': 1}):
            await self.app.handle_event(self.event('取消体测', 4))
        self.assertEqual(len(self.store.active_reminders()), 2)
        self.assertIsNone(self.store.confirmation_snapshot())

    async def test_batch_is_atomic_when_list_changes_during_model_call(self):
        with self.store.db:
            self.store.new_reminder('上课', 2000000000)
            second = self.store.new_reminder('作业', 2000000100)
        await self.app.handle_event(self.event('/list', 1))
        self.deliver_list()
        def model(*args):
            with self.store.db:
                self.store.finish_reminder(second, 'completed')
            return {'action': 'cancel', 'ids': [1, 2]}
        with patch('hence.app.ask_harness', side_effect=model):
            await self.app.handle_event(self.event('取消这两个', 2))
        self.assertEqual([row['title'] for row in self.store.active_reminders()], ['上课'])
        response = self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text']
        self.assertIn('先发送 /list', response)

    async def test_invalid_batch_never_changes_tasks(self):
        with self.store.db:
            self.store.new_reminder('上课', 2000000000)
        await self.app.handle_event(self.event('/list', 1))
        for number, ids in enumerate(([1, 1], [1, True], [1, 2]), 2):
            with patch('hence.app.ask_harness', return_value={'action': 'cancel', 'ids': ids}):
                await self.app.handle_event(self.event('取消待办', number))
            self.assertEqual(len(self.store.active_reminders()), 1)

    async def test_model_error_never_creates_a_reminder(self):
        with patch('hence.app.ask_harness', side_effect=TimeoutError()):
            await self.app.handle_event(self.event('明天提醒我交作业吗？', 1))
        self.assertEqual(self.store.active_reminders(), [])
        self.assertIn('/help', self.store.pending_outbox()['text'])

    async def test_command_bypasses_waiting_model(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_model(*args, **kwargs):
            started.set()
            await release.wait()
            return {'action': 'chat', 'reply': '完成'}
        with patch('hence.app.asyncio.to_thread', side_effect=slow_model):
            workers = [asyncio.create_task(self.app.event_loop(q)) for q in (self.app.events, self.app.commands)]
            try:
                await self.app.accept_event(self.event('你好', 1))
                await asyncio.wait_for(started.wait(), 1)
                await self.app.accept_event(self.event('/help', 2))
                await asyncio.wait_for(self.app.commands.join(), 1)
                self.assertIn('/add', self.store.pending_outbox()['text'])
                self.assertFalse(release.is_set())
                release.set()
                await asyncio.wait_for(self.app.events.join(), 1)
            finally:
                release.set()
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

    async def test_list_pagination_and_invalid_id(self):
        with self.store.db:
            for number in range(6):
                self.store.new_reminder(f'事项{number + 1}', 2000000000 + number)
        await self.app.handle_event(self.event('/list 2', 1))
        self.assertIn('事项6', self.store.pending_outbox()['text'])
        await self.app.handle_event(self.event('/complete 99999999999999999999999999999', 2))
        reply = self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text']
        self.assertIn('格式不正确', reply)

    async def test_flexible_list_requests_go_to_harness(self):
        with patch('hence.app.ask_harness', return_value={'action': 'list'}) as ask:
            for number, text in enumerate(('待办', '代办', '待办列表', '查看待办', '我的待办', '帮助'), start=1):
                await self.app.handle_event(self.event(text, number))
            self.assertEqual([call.args[2] for call in ask.call_args_list],
                             ['待办', '代办', '待办列表', '查看待办', '我的待办', '帮助'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 6)
        self.assertIn('没有待办', self.store.pending_outbox()['text'])

    async def test_canonical_command_is_available_without_harness(self):
        with patch('hence.app.ask_harness', side_effect=AssertionError('should not invoke Harness')) as ask:
            await self.app.handle_event(self.event('/list', 1))
            self.assertIn('没有待办', self.store.pending_outbox()['text'])
            ask.assert_not_called()

    async def test_offline_commands_and_validation(self):
        future = (datetime.now(ZONE) + timedelta(days=2)).strftime('%Y-%m-%d %H:%M')
        past = (datetime.now(ZONE) - timedelta(days=1)).strftime('%Y-%m-%d %H:%M')
        with patch('hence.app.ask_harness', side_effect=AssertionError('should not invoke Harness')) as ask:
            await self.app.handle_event(self.event('/help', 1))
            help_text = self.store.pending_outbox()['text']
            for command in ('/help', '/add', '/list', '/complete', '/cancel'):
                self.assertIn(command, help_text)
            self.assertNotIn('/reset', help_text)
            await self.app.handle_event(self.event(f'/add {future} 交作业', 2))
            self.assertEqual(self.store.active_reminders()[0]['title'], '交作业')
            await self.app.handle_event(self.event(f'/add {past} 过期事项', 3))
            await self.app.handle_event(self.event('/add 明天 09:00 错误格式', 4))
            await self.app.handle_event(self.event('/unknown', 5))
            await self.app.handle_event(self.event('/reset', 7))
            self.assertEqual(len(self.store.active_reminders()), 1)
            await self.app.handle_event(self.event('/list', 8))
            self.deliver_list()
            await self.app.handle_event(self.event('/complete 1', 6))
            self.assertEqual(self.store.active_reminders(), [])
            ask.assert_not_called()

    async def test_unauthorized_user_is_ignored(self):
        with patch('hence.app.ask_harness') as ask:
            event = self.event('你好', 2)
            event['user_id'] = 999
            await self.app.accept_event(event)
            self.assertTrue(self.app.events.empty())
            ask.assert_not_called()

    def test_reject_invalid_and_past_times(self):
        now = datetime(2026,11,15,20,0,tzinfo=ZONE)
        for due in ('2026-11-15T09:00:00+08:00', '2027-01-01T09:00:00', '2027-01-01T09:00:00Z'):
            with self.subTest(due=due), self.assertRaises(ValueError):
                validate_reminder('事项', due, now)


class HarnessPayloadTests(unittest.TestCase):
    def test_model_receives_current_commands_and_capabilities(self):
        with patch('hence.harness.urllib.request.urlopen', return_value=io.BytesIO(b'{"action":"chat","reply":"ok"}')) as urlopen:
            ask_harness('http://harness/turn', 'token', '有哪些指令', datetime.now(ZONE), [])
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertIn('/add', payload['commands'])
        self.assertIn('/help', payload['commands'])
        self.assertIn('不能直接访问邮箱', payload['capabilities'])
        self.assertEqual(payload['tasks_total'], 0)

    def test_model_sees_list_positions_not_database_ids(self):
        store = Store(':memory:')
        with store.db:
            first = store.new_reminder('已取消', 2000000000)
            store.finish_reminder(first, 'cancelled')
            store.new_reminder('上课', 2000000100)
            store.new_reminder('作业', 2000000000)
        with patch('hence.harness.urllib.request.urlopen', return_value=io.BytesIO(b'{"action":"list"}')) as urlopen:
            ask_harness('http://harness/turn', 'token', '待办', datetime.now(ZONE), store.active_reminders())
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual([(task['id'], task['title']) for task in payload['tasks']], [(1, '作业'), (2, '上课')])

    def test_large_payload_is_bounded_and_model_cannot_spoof_snapshot_counts(self):
        now = datetime.now(ZONE)
        timestamp = int(now.timestamp())
        task = {'id': 1, 'title': '测' * 300, 'due': timestamp + 1000000, 'status': 'pending', 'kind': 'task'}
        date = {**task, 'kind': 'date', 'visible_from': timestamp,
                'alerts': [timestamp + 86400 * index for index in range(1, 21)]}
        with patch('hence.harness.urllib.request.urlopen', return_value=io.BytesIO(b'{"action":"list","_dates_sent":30}')) as urlopen:
            result = ask_harness('http://harness/turn', 'token', '查看', now, [task] * 30, [date] * 30)
        raw = urlopen.call_args.args[0].data
        payload = json.loads(raw)
        self.assertLessEqual(len(raw), 60000)
        self.assertEqual(payload['dates_total'], 30)
        self.assertLess(len(payload['dates']), 30)
        self.assertEqual(result['_dates_sent'], len(payload['dates']))


class RealHarnessTests(unittest.TestCase):
    def test_sdk_restores_session_and_has_no_model_facing_tools(self):
        try:
            from deepseek_harness import DeepSeekHarness  # noqa: F401
        except ImportError:
            self.skipTest('DeepSeek Harness SDK is not installed')
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from harness_runtime.server import ChatEngine, create_server

        class Provider(BaseHTTPRequestHandler):
            requests = []
            def log_message(self, *args): pass
            def do_GET(self):
                data = json.dumps({'object':'list','data':[{'id':'deepseek-flash','object':'model','owned_by':'deepseek'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_POST(self):
                self.requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                content = json.dumps({'action':'chat','reply':'收到'}, ensure_ascii=False)
                events = [
                    {'type':'message_start','message':{'id':'test','type':'message','role':'assistant','model':'deepseek-flash','content':[], 'usage':{'input_tokens':10,'output_tokens':0}}},
                    {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
                    {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':content}},
                    {'type':'content_block_stop','index':0},
                    {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':10}},
                    {'type':'message_stop'}]
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.end_headers()
                for event in events:
                    self.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode())
                    self.wfile.flush()

        try:
            server = ThreadingHTTPServer(('127.0.0.1',0),Provider)
        except PermissionError:
            self.skipTest('localhost sockets are unavailable')
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as root:
                path = os.path.abspath('harness_runtime/profile.yml')
                args = {'home':root+'/home','workspace':root+'/work','patch':path,'model':'deepseek-flash',
                        'api_key':'fake-key','base_url':f'http://127.0.0.1:{server.server_port}'}
                def turn(text):
                    engine = ChatEngine(**args)
                    bridge = create_server(engine, 'bridge-secret', host='127.0.0.1', port=0)
                    threading.Thread(target=bridge.serve_forever, daemon=True).start()
                    try:
                        return ask_harness(f'http://127.0.0.1:{bridge.server_port}/turn', 'bridge-secret', text,
                                           datetime(2026,11,15,20,0,tzinfo=ZONE), [])
                    finally:
                        bridge.shutdown()
                        bridge.server_close()
                        engine.close()
                self.assertEqual(turn('我喜欢蓝色')['reply'], '收到')
                self.assertEqual(turn('我喜欢什么颜色')['reply'], '收到')
                self.assertIn('我喜欢蓝色',json.dumps(Provider.requests[-1],ensure_ascii=False))
                self.assertEqual(Provider.requests[-1].get('tools',[]),[])
                self.assertNotIn('dsh_session_log',Provider.requests[-1])
        finally:
            server.shutdown()
            server.server_close()
