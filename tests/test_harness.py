import json
import io
import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from hence.harness import ask_harness
from hence.reminders import ZONE, validate_reminder
from hence.store import Store
from helpers import AssistantTestCase, private_event, running


class HarnessActionTests(AssistantTestCase):
    async def cancel_via_model(self, number, message_id, text='取消那项'):
        await self.model_turn(text, message_id, action='cancel', ids=[number])

    async def test_chat_reply_and_duplicate_creation(self):
        result = {'action': 'chat', 'reply': '你好，我会记住你喜欢蓝色。'}
        with patch('hence.app.ask_harness', return_value=result) as ask:
            await self.say('我喜欢蓝色', 1)
            self.assertIn('蓝色', self.store.pending_outbox()['text'])
            self.assertEqual(ask.call_args.args[2], '我喜欢蓝色')
        due = (datetime.now(ZONE) + timedelta(hours=1)).isoformat()
        for _ in range(2):
            await self.model_turn('一小时后提醒我开会', 2, action='create', title='开会', due=due)
        self.assertEqual(len(self.store.active_reminders()), 1)
        self.assertEqual(self.store.active_reminders()[0]['title'], '开会')

    async def test_named_batch_keeps_targets_when_unrelated_task_changes_positions(self):
        self.add_task('数学作业')
        self.add_task('英语作业', 2000000100)
        def model(*args):
            self.add_task('体测', 1999999900)
            return {'action': 'complete', 'ids': [1, 2], 'reference': 'description'}
        with patch('hence.app.ask_harness', side_effect=model):
            await self.say('数学和英语作业都做完了', 1)
        self.assertEqual(len(self.store.active_reminders()), 3)
        self.assertIn('将要完成的 2 项', self.last_reply())
        pending = self.store.db.execute('SELECT targets FROM pending_confirmation').fetchone()
        self.assertEqual([row['title'] for row in json.loads(pending['targets'])], ['数学作业', '英语作业'])
        await self.confirm(2)
        self.assertEqual([row['title'] for row in self.store.active_reminders()], ['体测'])

    async def test_named_batch_rejected_if_target_changes_during_model_call(self):
        self.add_task('数学作业')
        rid = self.add_task('英语作业', 2000000100)
        def model(*args):
            with self.store.db:
                self.store.finish_reminder(rid, 'completed')
            return {'action': 'cancel', 'ids': [1, 2], 'reference': 'description'}
        with patch('hence.app.ask_harness', side_effect=model):
            await self.say('取消数学和英语作业', 1)
        self.assertEqual([row['title'] for row in self.store.active_reminders()], ['数学作业'])
        self.assertIn('相关事项已变化', self.store.pending_outbox()['text'])
        self.assertIsNone(self.store.db.execute('SELECT * FROM pending_confirmation').fetchone())

    async def test_position_reference_still_requires_delivered_list(self):
        self.add_task('体测')
        await self.model_turn('取消第一个', 1, action='cancel', ids=[1], reference='position')
        self.assertIn('先发送 /list', self.store.pending_outbox()['text'])
        self.assertIsNone(self.store.db.execute('SELECT * FROM pending_confirmation').fetchone())

    async def test_position_check_allows_append_but_rejects_a_shifted_target(self):
        self.add_task('上课', 2000000100)
        await self.say('/list', 1)
        await self.say('/cancel 1', 0)
        self.assertIn('先发送 /list', self.last_reply())
        self.assertEqual(len(self.store.active_reminders()), 1)
        self.deliver('task_list')
        self.add_task('体测', 2000000200)
        await self.say('/cancel 1', 2)
        self.assertIn('已取消待办：上课', self.last_reply())
        self.assertIsNone(self.store.confirmation_snapshot())
        self.assertEqual([r['title'] for r in self.store.active_reminders()], ['体测'])
        await self.view_tasks(3)
        self.add_task('作业')
        await self.say('/cancel 1', 4)
        self.assertEqual(len(self.store.active_reminders()), 2)
        self.assertIn('先发送 /list', self.last_reply())

    async def test_position_batch_survives_unrelated_append_during_model_call(self):
        self.add_task('作业', 2000000100)
        self.add_task('上课')
        await self.view_tasks(1)

        def model(*args):
            self.add_task('体测', 2000000200)
            return {'action': 'cancel', 'ids': [1, 2], 'reference': 'position'}
        with patch('hence.app.ask_harness', side_effect=model):
            await self.say('取消前两项', 2)
        self.assertEqual(len(self.store.active_reminders()), 3)
        self.assertIn('将要取消的 2 项', self.last_reply())
        self.assertIn('1. 上课', self.last_reply())
        self.assertIn('2. 作业', self.last_reply())
        await self.confirm(3)
        self.assertEqual([r['title'] for r in self.store.active_reminders()], ['体测'])
        await self.say('/list', 4)
        self.assertIn('1. 体测', self.last_reply())

    async def test_old_inflight_list_ack_does_not_authorize_changed_list(self):
        self.add_task('上课', 2000000100)
        await self.say('/list', 1)
        old = self.pending_message('task_list')
        self.add_task('体测')
        await self.say('/list', 2)
        now = int(datetime.now().timestamp())
        self.store.mark_sent(old, now)
        await self.say('/cancel 1', 3)
        self.assertEqual(len(self.store.active_reminders()), 2)
        self.assertIn('先发送 /list', self.last_reply())
        self.deliver('task_list')
        await self.say('/cancel 1', 4)
        self.assertEqual([row['title'] for row in self.store.active_reminders()], ['上课'])

    async def test_old_list_retry_is_suppressed_after_new_list(self):
        self.add_task('上课', 2000000100)
        await self.say('/list', 1)
        now = int(datetime.now().timestamp())
        old = self.store.pending_outbox(now)
        self.store.defer_message(old, now)
        self.add_task('体测')
        await self.say('/list', 2)
        new = self.store.pending_outbox(now)
        self.assertIn('1. 体测', new['text'])
        self.store.mark_sent(new, now)
        self.assertIsNone(self.store.pending_outbox(now + 20))
        await self.say('/cancel 1', 3)
        self.assertIn('已取消待办：体测', self.last_reply())
        self.assertEqual([r['title'] for r in self.store.active_reminders()], ['上课'])

    async def test_natural_confirmation_and_discard(self):
        self.add_task('上课')
        self.add_task('体测', 2000000100)
        await self.view_tasks(1)
        await self.cancel_via_model(1, 2, '取消这两项')
        self.assertIn('1 项', self.last_reply())
        self.assertIn('上课', self.last_reply())
        self.assertNotIn('体测', self.last_reply())
        self.assertEqual(len(self.store.active_reminders()), 2)
        self.deliver('confirm_preview')
        with patch('hence.app.ask_harness', return_value={'action': 'discard_confirmation'}) as ask:
            await self.say('算了，先别取消', 3)
        self.assertEqual(len(self.store.active_reminders()), 2)
        self.assertEqual(ask.call_args.args[6]['targets'][0]['title'], '上课')
        self.assertNotIn('token', ask.call_args.args[6])
        await self.cancel_via_model(1, 4)
        self.deliver('confirm_preview')
        await self.model_turn('是的，取消吧', 5, action='confirm')
        self.assertEqual([row['title'] for row in self.store.active_reminders()], ['体测'])

    async def test_no_preview_or_undelivered_preview_cannot_be_confirmed(self):
        self.add_task('上课')
        await self.model_turn('确认', 1, action='confirm')
        self.assertEqual(len(self.store.active_reminders()), 1)
        await self.view_tasks(2)
        await self.cancel_via_model(1, 3)
        await self.model_turn('确认', 4, action='confirm')
        self.assertEqual(len(self.store.active_reminders()), 1)
        self.assertIn('没有已送达', self.last_reply())
        await self.confirm(5)
        self.assertEqual(self.store.active_reminders(), [])

    async def test_confirmation_is_bound_to_snapshot_during_model_call(self):
        self.add_task('上课')
        self.add_task('体测', 2000000100)
        await self.view_tasks(1)
        await self.cancel_via_model(1, 2)
        self.deliver('confirm_preview')
        async def change_during_model(*args, **kwargs):
            with self.store.db:
                self.app.prepare_confirmation('cancelled', [self.store.active_reminders()[1]])
            self.deliver('confirm_preview')
            return {'action': 'confirm'}
        with patch('hence.app.asyncio.to_thread', side_effect=change_during_model):
            await self.say('确认', 3)
        self.assertEqual(len(self.store.active_reminders()), 2)
        self.assertIn('已变化', self.last_reply())

    async def test_model_failure_offers_fixed_command_and_invalidates_preview(self):
        self.add_task('上课')
        await self.view_tasks(1)
        await self.cancel_via_model(1, 2)
        self.deliver('confirm_preview')
        with patch('hence.app.ask_harness', side_effect=TimeoutError()):
            await self.say('确认', 3)
        self.assertIn('/list', self.last_reply())
        self.assertEqual(len(self.store.active_reminders()), 1)
        await self.say('/cancel 1', 4)
        self.assertEqual(self.store.active_reminders(), [])
        self.assertIsNone(self.store.confirmation_snapshot())
        await self.model_turn('确认', 5, action='confirm')
        self.assertIn('没有已送达', self.last_reply())

    async def test_fixed_command_invalidates_unsent_natural_preview(self):
        self.add_task('上课')
        self.add_task('体测', 2000000100)
        await self.view_tasks(1)
        await self.cancel_via_model(1, 2)
        preview = self.pending_message('confirm_preview')
        self.assertIsNotNone(preview)
        await self.say('/cancel 2', 3)
        self.assertEqual([row['title'] for row in self.store.active_reminders()], ['上课'])
        self.assertIsNone(self.pending_message('confirm_preview'))
        # A preview already in flight can still be acknowledged, but must not be actionable.
        self.store.mark_sent(preview, int(datetime.now().timestamp()))
        await self.model_turn('确认', 4, action='confirm')
        self.assertEqual([row['title'] for row in self.store.active_reminders()], ['上课'])

    async def test_replaced_confirmation_and_changed_target_do_not_execute(self):
        self.add_task('上课')
        self.add_task('体测', 2000000100)
        await self.view_tasks(1)
        await self.cancel_via_model(1, 2)
        old = self.pending_message('confirm_preview')
        await self.cancel_via_model(2, 3)
        new_preview = self.pending_message('confirm_preview')
        now = int(datetime.now().timestamp())
        self.store.mark_sent(new_preview, now)
        self.store.mark_sent(old, now)
        await self.model_turn('确认', 4, action='confirm')
        self.assertEqual(len(self.store.active_reminders()), 2)
        self.assertEqual(self.store.db.execute('SELECT token FROM pending_confirmation').fetchone()['token'], new_preview['ref'])
        self.store.mark_sent(new_preview, now)
        with self.store.db:
            self.store.db.execute("UPDATE reminders SET title='其他' WHERE id=?", (2,))
        await self.model_turn('确认', 5, action='confirm')
        self.assertEqual(len(self.store.active_reminders()), 2)
        self.assertIn('未执行', self.last_reply())

    async def test_expired_confirmation_and_unknown_truncated_id_are_rejected(self):
        self.add_task('上课')
        self.add_task('体测', 2000000100)
        await self.view_tasks(1)
        await self.cancel_via_model(1, 2)
        self.deliver('confirm_preview')
        with self.store.db:
            self.store.db.execute('UPDATE pending_confirmation SET expires=0')
        await self.model_turn('确认', 3, action='confirm')
        self.assertIn('没有已送达', self.last_reply())
        await self.model_turn('取消体测', 4, action='cancel', ids=[2], _tasks_sent=1)
        self.assertEqual(len(self.store.active_reminders()), 2)
        self.assertIsNone(self.store.confirmation_snapshot())

    async def test_batch_is_atomic_when_list_changes_during_model_call(self):
        self.add_task('上课')
        second = self.add_task('作业', 2000000100)
        await self.view_tasks(1)
        def model(*args):
            with self.store.db:
                self.store.finish_reminder(second, 'completed')
            return {'action': 'cancel', 'ids': [1, 2]}
        with patch('hence.app.ask_harness', side_effect=model):
            await self.say('取消这两个', 2)
        self.assertEqual([row['title'] for row in self.store.active_reminders()], ['上课'])
        self.assertIn('先发送 /list', self.last_reply())

    async def test_invalid_batch_never_creates_a_confirmation(self):
        self.add_task('上课')
        await self.view_tasks(1)
        invalid = [
            {'ids': [1, 1]}, {'ids': [1, True]}, {'ids': [1, 2]},
            {'action': 'complete', 'ids': [99]}, {'ids': []},
            {'ids': [1], 'reference': 'unknown'},
        ]
        for number, result in enumerate(invalid, 2):
            with self.subTest(result=result), patch('hence.app.ask_harness', return_value={'action': 'cancel', **result}):
                await self.say('处理待办', number)
                self.assertEqual(len(self.store.active_reminders()), 1)
                self.assertIsNone(self.store.db.execute('SELECT * FROM pending_confirmation').fetchone())
                self.assertIn('无法确定', self.last_reply())

    async def test_model_error_never_creates_a_reminder(self):
        with patch('hence.app.ask_harness', side_effect=TimeoutError()):
            await self.say('明天提醒我交作业吗？', 1)
        self.assertEqual(self.store.active_reminders(), [])
        self.assertIn('/help', self.store.pending_outbox()['text'])

    async def test_command_bypasses_waiting_model(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_model(*args, **kwargs):
            started.set()
            await release.wait()
            return {'action': 'chat', 'reply': '完成'}
        with patch('hence.app.asyncio.to_thread', side_effect=slow_model):
            async with running(self.app.event_loop(self.app.events)), running(self.app.event_loop(self.app.commands)):
                await self.app.accept_event(private_event('你好', 1))
                await asyncio.wait_for(started.wait(), 1)
                await self.app.accept_event(private_event('/help', 2))
                await asyncio.wait_for(self.app.commands.join(), 1)
                self.assertIn('/add', self.store.pending_outbox()['text'])
                self.assertFalse(release.is_set())
                release.set()
                await asyncio.wait_for(self.app.events.join(), 1)

    async def test_list_pagination_and_invalid_id(self):
        with self.store.db:
            for number in range(6):
                self.store.new_reminder(f'事项{number + 1}', 2000000000 + number)
        await self.view_tasks(1)
        self.assertNotIn('事项6', self.last_reply())
        await self.say('/cancel 6', 2)
        self.assertIn('/list 2', self.last_reply())
        await self.say('/list 2', 3)
        self.assertIn('6. 事项6', self.last_reply())
        await self.model_turn('取消第六项', 4, action='cancel', ids=[6], reference='position')
        self.assertIn('/list 2', self.last_reply())
        self.assertEqual(len(self.store.active_reminders()), 6)
        self.deliver('task_list')
        self.assertTrue(self.store.positions_match('task', self.store.active_reminders(), [1, 6]))
        await self.say('/cancel 6', 5)
        self.assertIn('已取消待办：事项6', self.last_reply())
        await self.say('/complete 99999999999999999999999999999', 6)
        self.assertIn('格式不正确', self.last_reply())

    async def test_flexible_list_requests_go_to_harness(self):
        with patch('hence.app.ask_harness', return_value={'action': 'list'}) as ask:
            for number, text in enumerate(('待办', '代办', '待办列表', '查看待办', '我的待办', '帮助'), start=1):
                await self.say(text, number)
                self.assertEqual(ask.call_args.args[2], text)
                self.assertIn('没有待办', self.last_reply())

    async def test_offline_commands_and_validation(self):
        future = (datetime.now(ZONE) + timedelta(days=2)).strftime('%Y-%m-%d %H:%M')
        past = (datetime.now(ZONE) - timedelta(days=1)).strftime('%Y-%m-%d %H:%M')
        with patch('hence.app.ask_harness', side_effect=AssertionError('should not invoke Harness')) as ask:
            await self.say('/help', 1)
            help_text = self.store.pending_outbox()['text']
            for command in ('/help', '/add', '/list', '/complete', '/cancel'):
                self.assertIn(command, help_text)
            self.assertNotIn('/reset', help_text)
            await self.say(f'/add {future} 交作业', 2)
            await self.say(f'/add {future} 交作业', 2)  # A replay must not create a second task.
            self.assertEqual(self.store.active_reminders()[0]['title'], '交作业')
            await self.say(f'/add {past} 过期事项', 3)
            await self.say('/add 明天 09:00 错误格式', 4)
            await self.say('/unknown', 5)
            self.assertEqual(len(self.store.active_reminders()), 1)
            await self.view_tasks(6)
            await self.say('/complete 1', 7)
            self.assertEqual(self.store.active_reminders(), [])
            ask.assert_not_called()

    def test_reject_invalid_and_past_times(self):
        now = datetime(2026,11,15,20,0,tzinfo=ZONE)
        for due in ('2026-11-15T09:00:00+08:00', '2027-01-01T09:00:00', '2027-01-01T09:00:00Z'):
            with self.subTest(due=due), self.assertRaises(ValueError):
                validate_reminder('事项', due, now)


class HarnessPayloadTests(unittest.TestCase):
    def test_model_receives_commands_permissions_and_display_positions(self):
        store = Store(':memory:')
        self.addCleanup(store.db.close)
        with store.db:
            first = store.new_reminder('已取消', 2000000000)
            store.finish_reminder(first, 'cancelled')
            store.new_reminder('上课', 2000000100)
            store.new_reminder('作业', 2000000000)
        with patch('hence.harness.urllib.request.urlopen', return_value=io.BytesIO(b'{"action":"list"}')) as urlopen:
            ask_harness('http://harness/turn', 'token', '待办', datetime.now(ZONE), store.active_reminders())
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertIn('/add', payload['commands'])
        self.assertIn('/help', payload['commands'])
        self.assertIn('不能直接访问邮箱', payload['capabilities'])
        self.assertEqual(payload['tasks_total'], 2)
        self.assertEqual([(task['id'], task['title']) for task in payload['tasks']], [(1, '作业'), (2, '上课')])

    def test_large_payload_is_bounded_and_model_cannot_spoof_snapshot_counts(self):
        now = datetime.now(ZONE)
        timestamp = int(now.timestamp())
        task = {'id': 1, 'title': '测' * 300, 'due': timestamp + 1000000, 'status': 'pending', 'kind': 'task'}
        date = {**task, 'kind': 'date', 'visible_from': timestamp,
                'alerts': [timestamp + 86400 * index for index in range(1, 21)]}
        with patch('hence.harness.urllib.request.urlopen', return_value=io.BytesIO(b'{"action":"list","_dates_sent":30}')) as urlopen:
            result = ask_harness('http://harness/turn', 'token', '查看', now, [task] * 30, [date] * 30,
                                 dates_total=42)
        raw = urlopen.call_args.args[0].data
        payload = json.loads(raw)
        self.assertLessEqual(len(raw), 60000)
        self.assertEqual(payload['dates_total'], 42)
        self.assertLess(len(payload['dates']), 30)
        self.assertEqual(result['_dates_sent'], len(payload['dates']))
