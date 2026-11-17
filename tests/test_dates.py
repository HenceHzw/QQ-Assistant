import asyncio
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

try:
    import websockets  # noqa: F401
except ImportError:
    sys.modules['websockets'] = types.ModuleType('websockets')

from hence.app import App, format_reminders
from hence.config import Settings
from hence.dates import validate_date_reminder
from hence.reminders import ZONE
from hence.store import Store


def moment(day, clock='00:00'):
    return datetime.strptime(day + ' ' + clock, '%Y-%m-%d %H:%M').replace(tzinfo=ZONE)


class DateStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.now = moment('2026-12-01')

    def create(self, show=2, alerts=None):
        alerts = alerts or [{'days': 2, 'time': '09:00'}, {'days': 0, 'time': '09:00'}]
        values = validate_date_reminder('体测', '2026-12-20', show, alerts, self.now)
        title, due, start, end, future, skipped = values
        with self.store.db:
            rid = self.store.new_date_reminder(title, due, start, end, future)
        return rid, values

    def test_visibility_and_retention(self):
        rid, _ = self.create()
        for day, expected in [('2026-12-17', False), ('2026-12-18', True),
                              ('2026-12-19', True), ('2026-12-20', True), ('2026-12-21', False)]:
            with self.subTest(day=day):
                self.assertEqual(bool(self.store.active_reminders(moment(day).timestamp())), expected)
        self.assertEqual(self.store.date_reminders()[0]['id'], rid)
        self.assertIn('日期提醒', format_reminders(self.store.active_reminders(moment('2026-12-18').timestamp())))

    def test_missed_alerts_are_combined_after_restart_and_sent_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'data.db')
            self.store = Store(path)
            self.create()
            self.store.db.close()
            self.store = Store(path)
            self.store.schedule_due(int(moment('2026-12-21').timestamp()))
            self.store.schedule_due(int(moment('2026-12-21').timestamp()))
            message = self.store.pending_outbox(int(moment('2026-12-21').timestamp()))
            self.assertIn('迟到', message['text'])
            self.assertIn('2026-12-18 09:00', message['text'])
            self.assertIn('2026-12-20 09:00', message['text'])
            self.assertEqual(len(message['batch']), 2)
            self.store.mark_sent(message, int(moment('2026-12-21').timestamp()))
            self.assertIsNone(self.store.pending_outbox(int(moment('2026-12-21').timestamp())))
            self.assertEqual(self.store.date_reminders()[0]['status'], 'notified')

    def test_retry_merges_due_alerts_and_cancel_removes_queued(self):
        rid, _ = self.create()
        first = int(moment('2026-12-18', '09:00').timestamp())
        self.store.schedule_due(first)
        pending = self.store.pending_outbox(first)
        self.store.defer_message(pending, first)
        later = int(moment('2026-12-20', '09:00').timestamp())
        self.store.schedule_due(later)
        self.assertEqual(len(self.store.pending_outbox(later)['batch']), 2)
        with self.store.db:
            self.store.finish_reminder(rid, 'cancelled')
        self.assertIsNone(self.store.pending_outbox(later))
        self.store.schedule_due(later + 10)
        self.assertIsNone(self.store.pending_outbox(later + 10))

    def test_first_delivery_does_not_suppress_later_alert(self):
        self.create()
        first = int(moment('2026-12-18', '09:00').timestamp())
        self.store.schedule_due(first)
        first_message = self.store.pending_outbox(first)
        self.assertNotIn('迟到', first_message['text'])
        self.store.mark_sent(first_message, first)
        later = int(moment('2026-12-20', '09:00').timestamp())
        self.store.schedule_due(later)
        second_message = self.store.pending_outbox(later)
        self.assertIn('2026-12-20 09:00', second_message['text'])
        self.assertNotIn('2026-12-18 09:00', second_message['text'])
        self.store.mark_sent(second_message, later)
        self.assertIsNone(self.store.pending_outbox(later))

    def test_alert_queued_during_send_is_not_marked_as_sent(self):
        self.create(alerts=[{'days': 2, 'time': '09:00'}, {'days': 2, 'time': '09:01'}])
        first = int(moment('2026-12-18', '09:00').timestamp())
        self.store.schedule_due(first)
        selected = self.store.pending_outbox(first)
        self.store.schedule_due(first + 60)
        self.store.mark_sent(selected, first + 60)
        remaining = self.store.pending_outbox(first + 60)
        self.assertIn('09:01', remaining['text'])
        self.assertNotIn('09:00', remaining['text'])

    def test_custom_window_and_past_times(self):
        values = validate_date_reminder('会议', '2026-12-20', 7,
                                        [{'days': 3, 'time': '09:00'}, {'days': 0, 'time': '10:00'}],
                                        moment('2026-12-19'))
        self.assertEqual(datetime.fromtimestamp(values[2], ZONE).date().isoformat(), '2026-12-13')
        self.assertEqual(len(values[4]), 1)
        self.assertEqual(len(values[5]), 1)
        for alerts in ([], [{'days': 0, 'time': '25:00'}],
                       [{'days': 0, 'time': '09:00'}, {'days': 0, 'time': '09:00'}]):
            with self.subTest(alerts=alerts), self.assertRaises(ValueError):
                validate_date_reminder('会议', '2026-12-20', 2, alerts, self.now)

    def test_old_reminder_table_migrates_without_losing_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'old.db')
            db = sqlite3.connect(path)
            db.executescript("CREATE TABLE reminders (id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
                             "due INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending'); "
                             "INSERT INTO reminders(title,due) VALUES ('已有待办', 2000000000);")
            db.close()
            store = Store(path)
            row = store.active_reminders()[0]
            self.assertEqual((row['title'], row['kind']), ('已有待办', 'task'))
            store.db.close()


class DateAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        settings = Settings(123, 'ws://localhost', 'token', ':memory:', (), 120,
                            'http://harness/turn', 'token')
        self.app = App(settings, self.store)
        self.day = (datetime.now(ZONE) + timedelta(days=30)).strftime('%Y-%m-%d')
        async def run_mocked(func, *args):
            return func(*args)
        patcher = patch('hence.app.asyncio.to_thread', new=run_mocked)
        patcher.start()
        self.addCleanup(patcher.stop)

    def event(self, text, number):
        return {'message_type': 'private', 'user_id': 123, 'self_id': 456, 'message_id': number,
                'message': [{'type': 'text', 'data': {'text': text}}]}

    def last_reply(self):
        return self.store.db.execute("SELECT text FROM outbox WHERE kind IN ('reply','confirm_preview','task_list','date_list') ORDER BY id DESC").fetchone()['text']

    def deliver_list(self, kind):
        row = self.store.db.execute('SELECT * FROM outbox WHERE kind=? AND sent IS NULL ORDER BY id DESC', (kind,)).fetchone()
        self.assertIsNotNone(row)
        self.store.mark_sent(row, int(datetime.now().timestamp()))

    async def confirm(self, message_id):
        preview = self.store.db.execute("SELECT * FROM outbox WHERE kind='confirm_preview' AND sent IS NULL ORDER BY id DESC").fetchone()
        self.store.mark_sent(preview, int(datetime.now().timestamp()))
        with patch('hence.app.ask_harness', return_value={'action': 'confirm'}):
            await self.app.handle_event(self.event('确认', message_id))

    async def test_offline_create_list_cancel(self):
        await self.app.handle_event(self.event(f'/date-add {self.day} 体测', 1))
        self.assertIn('已创建日期提醒', self.last_reply())
        self.assertEqual(len(self.store.active_reminders()), 0)
        self.assertEqual(len(self.store.date_reminders()), 1)
        await self.app.handle_event(self.event('/date-cancel 1', 2))
        self.assertIn('先发送 /dates', self.last_reply())
        await self.app.handle_event(self.event('/dates', 3))
        self.assertIn('1. 体测', self.last_reply())
        self.deliver_list('date_list')
        await self.app.handle_event(self.event('/date-cancel 1', 4))
        self.assertIn('已取消', self.last_reply())
        self.assertEqual(self.store.date_reminders(), [])

    async def test_custom_command_and_natural_language(self):
        await self.app.handle_event(self.event(f'/date-add {self.day} --show 7 --alerts 3@09:00,0@10:00 体测', 1))
        row = self.store.date_reminders()[0]
        self.assertEqual(len(self.store.date_alert_times(row['id'])), 2)
        result = {'action': 'create_date', 'title': '考试', 'date': self.day, 'show_days': 4,
                  'alerts': [{'days': 4, 'time': '08:30'}, {'days': 0, 'time': '10:00'}]}
        with patch('hence.app.ask_harness', return_value=result):
            await self.app.handle_event(self.event('创建考试日期提醒', 2))
        self.assertEqual(len(self.store.date_reminders()), 2)
        with patch('hence.app.ask_harness', return_value={'action': 'list_dates'}):
            await self.app.handle_event(self.event('查看日期提醒', 3))
        self.deliver_list('date_list')
        with patch('hence.app.ask_harness', return_value={'action': 'date_cancel', 'ids': [1, 2]}):
            await self.app.handle_event(self.event('取消这两个日期提醒', 4))
        self.assertEqual(len(self.store.date_reminders()), 2)
        await self.confirm(5)
        self.assertEqual(self.store.date_reminders(), [])

    async def test_explicit_date_list_positions_cannot_use_old_snapshot(self):
        await self.app.handle_event(self.event(f'/date-add {self.day} 体测', 1))
        await self.app.handle_event(self.event('/dates', 2))
        self.deliver_list('date_list')
        await self.app.handle_event(self.event(f'/date-add {self.day} 考试', 3))
        await self.app.handle_event(self.event('/date-complete 1', 4))
        self.assertEqual(len(self.store.date_reminders()), 2)
        self.assertIn('先发送 /dates', self.last_reply())

    async def test_hidden_date_can_be_cancelled_by_name_without_viewing_dates(self):
        await self.app.handle_event(self.event(f'/date-add {self.day} 体测', 1))
        self.assertEqual(self.store.active_reminders(), [])
        with patch('hence.app.ask_harness', return_value={'action': 'date_cancel', 'ids': [1], 'reference': 'description'}):
            await self.app.handle_event(self.event('取消体测', 2))
        self.assertIn('请核对将要取消的 1 项', self.last_reply())
        self.assertEqual(len(self.store.date_reminders()), 1)
        await self.confirm(3)
        self.assertEqual(self.store.date_reminders(), [])

    async def test_visible_date_can_be_completed_from_normal_list(self):
        soon = (datetime.now(ZONE) + timedelta(days=1)).strftime('%Y-%m-%d')
        await self.app.handle_event(self.event(f'/date-add {soon} 体测', 1))
        await self.app.handle_event(self.event('/list', 2))
        self.assertIn('1. 体测', self.last_reply())
        self.assertIn('日期提醒', self.last_reply())
        self.deliver_list('task_list')
        await self.app.handle_event(self.event('/complete 1', 3))
        self.assertIn('已完成', self.last_reply())
        self.assertEqual(self.store.date_reminders(), [])

    async def test_malformed_custom_command_and_numeric_title(self):
        await self.app.handle_event(self.event(f'/date-add {self.day} 7 3@09:00', 1))
        self.assertIn('格式或时间无效', self.last_reply())
        await self.app.handle_event(self.event(f'/date-add {self.day} --show 7 --alerts 3@09:00', 2))
        self.assertIn('格式或时间无效', self.last_reply())
        self.assertEqual(self.store.date_reminders(), [])
        await self.app.handle_event(self.event(f'/date-add {self.day} 2027 年 体测', 3))
        self.assertEqual(self.store.date_reminders()[0]['title'], '2027 年 体测')


if __name__ == '__main__':
    unittest.main()
