import os
import sqlite3
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from hence.app import format_task
from hence.dates import validate_date_reminder
from hence.mail import poll_mailbox
from hence.reminders import ZONE
from hence.store import Store, outbox_message
from helpers import AssistantTestCase


class StoreTests(unittest.TestCase):
    def test_upgrade_preserves_tasks_and_replies_and_invalidates_old_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'db')
            db = sqlite3.connect(path)
            db.executescript('''
                CREATE TABLE reminders (id INTEGER PRIMARY KEY, title TEXT NOT NULL,
                    due INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
                INSERT INTO reminders(title,due) VALUES ('已有待办', 2000000000);
                CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO kv VALUES ('pending_input', '旧数据');
                CREATE TABLE task_view (id INTEGER PRIMARY KEY, ids TEXT NOT NULL);
                INSERT INTO task_view VALUES (1, '[1]');
                CREATE TABLE outbox (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
                    text TEXT NOT NULL, created INTEGER NOT NULL, sent INTEGER, UNIQUE(kind,ref));
                INSERT INTO outbox(kind,ref,text,created) VALUES ('reply','old-list','当前待办：旧列表',1);
                INSERT INTO outbox(kind,ref,text,created) VALUES ('date_list','old-page','旧日期列表',1);
                INSERT INTO outbox(kind,ref,text,created) VALUES ('confirm_preview','preview','旧确认',1);
                INSERT INTO outbox(kind,ref,text,created) VALUES ('reply','other','其他回复',2);
            ''')
            db.commit()
            db.close()
            store = Store(path)
            self.assertFalse(store.positions_match('task', store.active_reminders(), [1]))
            self.assertEqual(store.pending_outbox()['text'], '其他回复')
            self.assertEqual(store.pending_outbox()['attempts'], 0)
            self.assertEqual(store.db.execute("SELECT value FROM kv WHERE key='pending_input'").fetchone()[0], '旧数据')
            task, = store.active_reminders()
            self.assertEqual((task['title'], task['kind']), ('已有待办', 'task'))
            with store.db:
                store.enqueue_list('task_list', 'new-list', store.active_reminders(), '新列表')
            row = outbox_message(store.db.execute("SELECT * FROM outbox WHERE kind='task_list'").fetchone())
            store.mark_sent(row, 100)
            store.db.close()
            store = Store(path)
            self.assertTrue(store.positions_match('task', store.active_reminders(), [1]))
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

    def test_cancel_during_send_preserves_new_reply_for_ack_and_retry(self):
        for kind in ('task', 'date'):
            for success in (True, False):
                with self.subTest(kind=kind, success=success):
                    store = Store(':memory:')
                    self.addCleanup(store.db.close)
                    with store.db:
                        rid = (store.new_reminder('体测', 100) if kind == 'task' else
                               store.new_date_reminder('体测', 200, 0, 300, [100, 110]))
                    store.schedule_due(120)
                    sending = store.pending_outbox(120)
                    with store.db:
                        store.finish_reminder(rid, 'cancelled')
                        store.enqueue('reply', 'cancel-event', '已取消体测', now=121)
                    reply = store.pending_outbox(121)
                    self.assertNotEqual(reply['id'], sending['id'])
                    if success:
                        store.mark_sent(sending, 122)
                    else:
                        store.defer_message(sending, 122)
                    pending = store.pending_outbox(122)
                    self.assertEqual((pending['text'], pending['attempts'], pending['sent']), ('已取消体测', 0, None))
                    store.mark_sent(pending, 123)
                    store.schedule_due(400)
                    self.assertIsNone(store.pending_outbox(400))
                    self.assertEqual(store.db.execute('SELECT status FROM reminders WHERE id=?', (rid,)).fetchone()[0], 'cancelled')

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


def moment(day, clock='00:00'):
    return datetime.strptime(day + ' ' + clock, '%Y-%m-%d %H:%M').replace(tzinfo=ZONE)


class DateStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.db.close)
        self.now = moment('2026-12-01')

    def create(self, show=2, alerts=None):
        alerts = alerts or [{'days': 2, 'time': '09:00'}, {'days': 0, 'time': '09:00'}]
        values = validate_date_reminder('体测', '2026-12-20', show, alerts, self.now)
        title, due, start, end, future, _ = values
        with self.store.db:
            return self.store.new_date_reminder(title, due, start, end, future)

    def test_visibility_and_retention(self):
        rid = self.create()
        for day, expected in [('2026-12-17', False), ('2026-12-18', True),
                              ('2026-12-19', True), ('2026-12-20', True), ('2026-12-21', False)]:
            with self.subTest(day=day):
                self.assertEqual(bool(self.store.active_reminders(moment(day).timestamp())), expected)
        self.assertEqual(self.store.date_reminders()[0]['id'], rid)
        self.assertIn('日期提醒', format_task(self.store.active_reminders(moment('2026-12-18').timestamp())[0]))

    def test_missed_alerts_are_combined_after_restart_and_sent_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'data.db')
            self.store = Store(path)
            self.create()
            self.store.db.close()
            self.store = Store(path)
            self.addCleanup(self.store.db.close)
            late = int(moment('2026-12-21').timestamp())
            self.store.schedule_due(late)
            self.store.schedule_due(late)
            message = self.store.pending_outbox(late)
            self.assertIn('迟到', message['text'])
            self.assertIn('2026-12-18 09:00', message['text'])
            self.assertIn('2026-12-20 09:00', message['text'])
            self.assertEqual(len(message['batch']), 2)
            self.store.mark_sent(message, late)
            self.assertIsNone(self.store.pending_outbox(late))
            self.assertEqual(self.store.date_reminders()[0]['status'], 'notified')

    def test_retry_keeps_batch_across_restart_and_cancellation_removes_later_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'db')
            self.store = Store(path)
            rid = self.create()
            first = int(moment('2026-12-18', '09:00').timestamp())
            self.store.schedule_due(first)
            pending = self.store.pending_outbox(first)
            self.store.defer_message(pending, first)
            self.store.db.close()
            self.store = Store(path)
            self.addCleanup(self.store.db.close)
            later = int(moment('2026-12-20', '09:00').timestamp())
            self.store.schedule_due(later)
            retry = self.store.pending_outbox(later)
            self.assertEqual((retry['batch'], retry['text']), (pending['batch'], pending['text']))
            self.store.mark_sent(retry, later)
            next_alert = self.store.pending_outbox(later)
            self.assertNotEqual(next_alert['ref'], retry['ref'])
            self.assertNotIn('12-18', next_alert['text'])
            with self.store.db:
                self.store.finish_reminder(rid, 'cancelled')
            self.assertIsNone(self.store.pending_outbox(later))

    def test_each_alert_is_delivered_once_even_when_the_next_one_is_already_queued(self):
        self.create(alerts=[{'days': 2, 'time': '09:00'}, {'days': 2, 'time': '09:01'},
                            {'days': 0, 'time': '09:00'}])
        first = int(moment('2026-12-18', '09:00').timestamp())
        self.store.schedule_due(first)
        sending = self.store.pending_outbox(first)
        self.assertNotIn('迟到', sending['text'])
        self.assertIn('2026-12-18 09:00', sending['text'])

        self.store.schedule_due(first + 60)
        self.store.mark_sent(sending, first + 60)
        second = self.store.pending_outbox(first + 60)
        self.assertIn('2026-12-18 09:01', second['text'])
        self.assertNotIn('2026-12-18 09:00', second['text'])
        self.store.mark_sent(second, first + 60)

        later = int(moment('2026-12-20', '09:00').timestamp())
        self.store.schedule_due(later)
        last = self.store.pending_outbox(later)
        self.assertIn('2026-12-20 09:00', last['text'])
        self.assertNotIn('2026-12-18', last['text'])
        self.store.mark_sent(last, later)
        self.assertIsNone(self.store.pending_outbox(later))

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


class DateAppTests(AssistantTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.day = (datetime.now(ZONE) + timedelta(days=30)).strftime('%Y-%m-%d')

    async def test_offline_create_list_cancel(self):
        await self.say(f'/date-add {self.day} 体测', 1)
        self.assertIn('已创建日期提醒', self.last_reply())
        self.assertEqual(len(self.store.active_reminders()), 0)
        self.assertEqual(len(self.store.date_reminders()), 1)
        await self.say('/date-cancel 1', 2)
        self.assertIn('先发送 /dates', self.last_reply())
        await self.say('/dates', 3)
        self.assertIn('1. 体测', self.last_reply())
        self.deliver('date_list')
        await self.say(f'/date-add {self.day} 考试', 4)
        await self.say('/date-cancel 1', 5)
        self.assertIn('已取消', self.last_reply())
        self.assertEqual([r['title'] for r in self.store.date_reminders()], ['考试'])
        await self.say('/dates', 6)
        self.deliver('date_list')
        await self.say('/date-complete 1', 7)
        self.assertIn('已完成日期提醒：考试', self.last_reply())
        self.assertEqual(self.store.date_reminders(), [])

    async def test_date_positions_require_delivery_of_the_matching_date_page(self):
        for n in range(1, 7):
            await self.say(f'/date-add {self.day} 事项{n}', n)
        await self.say('/dates', 7)
        self.deliver('date_list')
        await self.say('/date-complete 6', 8)
        self.assertIn('先发送 /dates 2', self.last_reply())
        await self.say('/dates 2', 9)
        self.assertIn('6. 事项6', self.last_reply())
        self.deliver('date_list')
        await self.say('/date-complete 6', 10)
        self.assertIn('已完成日期提醒：事项6', self.last_reply())
        self.assertEqual(len(self.store.date_reminders()), 5)

    async def test_custom_command_and_natural_language(self):
        await self.say(f'/date-add {self.day} --show 7 --alerts 3@09:00,0@10:00 体测', 1)
        row = self.store.date_reminders()[0]
        self.assertEqual(len(self.store.date_alert_times(row['id'])), 2)
        result = {'action': 'create_date', 'title': '考试', 'date': self.day, 'show_days': 4,
                  'alerts': [{'days': 4, 'time': '08:30'}, {'days': 0, 'time': '10:00'}]}
        await self.model_turn('创建考试日期提醒', 2, **result)
        self.assertEqual(len(self.store.date_reminders()), 2)
        await self.model_turn('查看日期提醒', 3, action='list_dates')
        self.deliver('date_list')
        await self.model_turn('取消这两个日期提醒', 4, action='date_cancel', ids=[1, 2])
        self.assertEqual(len(self.store.date_reminders()), 2)
        await self.confirm(5)
        self.assertEqual(self.store.date_reminders(), [])

    async def test_hidden_date_can_be_cancelled_by_name_without_viewing_dates(self):
        await self.say(f'/date-add {self.day} 体测', 1)
        self.assertEqual(self.store.active_reminders(), [])
        await self.model_turn('取消体测', 2, action='date_cancel', ids=[1], reference='description')
        self.assertIn('请核对将要取消的 1 项', self.last_reply())
        self.assertEqual(len(self.store.date_reminders()), 1)
        await self.confirm(3)
        self.assertEqual(self.store.date_reminders(), [])

    async def test_visible_date_can_be_completed_from_normal_list(self):
        soon = (datetime.now(ZONE) + timedelta(days=1)).strftime('%Y-%m-%d')
        await self.say(f'/date-add {soon} 体测', 1)
        await self.say('/list', 2)
        self.assertIn('1. 体测', self.last_reply())
        self.assertIn('日期提醒', self.last_reply())
        self.deliver('task_list')
        await self.say('/complete 1', 3)
        self.assertIn('已完成', self.last_reply())
        self.assertEqual(self.store.date_reminders(), [])

    async def test_malformed_custom_command_and_numeric_title(self):
        await self.say(f'/date-add {self.day} 7 3@09:00', 1)
        self.assertIn('格式或时间无效', self.last_reply())
        await self.say(f'/date-add {self.day} --show 7 --alerts 3@09:00', 2)
        self.assertIn('格式或时间无效', self.last_reply())
        self.assertEqual(self.store.date_reminders(), [])
        await self.say(f'/date-add {self.day} 2027 年 体测', 3)
        self.assertEqual(self.store.date_reminders()[0]['title'], '2027 年 体测')


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


class OfflineCommandTests(AssistantTestCase):
    harness_url = ''

    async def test_incomplete_rule_input_is_rejected_without_followup(self):
        await self.say('提醒我交作业', 1)
        self.assertIn('/help', self.last_reply())
        await self.say('明天', 2)
        self.assertEqual(self.store.active_reminders(), [])


if __name__ == '__main__':
    unittest.main()
