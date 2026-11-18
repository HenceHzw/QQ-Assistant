import json
import hashlib
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

from .commands import PAGE_SIZE


def outbox_message(row):
    """Use one message shape for individual sends and grouped date reminders."""
    return None if row is None else {**dict(row), 'batch': [(row['id'], row['ref'])]}


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS reminders (
          id INTEGER PRIMARY KEY, title TEXT NOT NULL, due INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS outbox (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
          text TEXT NOT NULL, created INTEGER NOT NULL, sent INTEGER,
          UNIQUE(kind, ref));
        CREATE TABLE IF NOT EXISTS mailbox_state (
          id TEXT PRIMARY KEY, uidvalidity TEXT NOT NULL, last_uid INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS incoming (
          id TEXT PRIMARY KEY, received INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS incoming_queue (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS channel_state (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS mailbox_health (
          id TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0, alerted INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS list_pages (
          kind TEXT NOT NULL, page INTEGER NOT NULL, ids TEXT NOT NULL, PRIMARY KEY(kind,page));
        CREATE TABLE IF NOT EXISTS pending_confirmation (
          id INTEGER PRIMARY KEY CHECK (id=1), token TEXT NOT NULL, action TEXT NOT NULL,
          targets TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS delivered_confirmation (
          id INTEGER PRIMARY KEY CHECK (id=1), token TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS date_alerts (
          reminder_id INTEGER NOT NULL REFERENCES reminders(id), due INTEGER NOT NULL,
          queued INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(reminder_id,due));
        ''')
        self.db.execute('BEGIN IMMEDIATE')
        reminder_columns = {row['name'] for row in self.db.execute('PRAGMA table_info(reminders)')}
        for name, definition in (('kind', "TEXT NOT NULL DEFAULT 'task'"),
                                 ('visible_from', 'INTEGER'), ('visible_until', 'INTEGER')):
            if name not in reminder_columns:
                self.db.execute(f'ALTER TABLE reminders ADD COLUMN {name} {definition}')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(outbox)')}
        for name, definition in (('retry_at', 'INTEGER NOT NULL DEFAULT 0'), ('attempts', 'INTEGER NOT NULL DEFAULT 0'),
                                 ('reply_to', 'TEXT'), ('reply_at', 'INTEGER'), ('paused_reason', 'TEXT'),
                                 ('list_page', 'INTEGER'), ('list_ids', 'TEXT'), ('date_batch', 'INTEGER')):
            if name not in columns:
                self.db.execute(f'ALTER TABLE outbox ADD COLUMN {name} {definition}')
        self._reply_context = (None, None)
        self.db.execute('CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(id) WHERE sent IS NULL')
        if 'list_page' not in columns:
            # Old snapshots included undisplayed pages. Neither those snapshots nor their
            # queued previews may authorize an operation after upgrading.
            self.db.execute("UPDATE outbox SET sent=? WHERE sent IS NULL AND (kind IN ('task_list','date_list','confirm_preview') "
                            "OR (kind='reply' AND (text LIKE '当前待办%' OR text LIKE '当前没有待办%' "
                            "OR text LIKE '日期提醒（%' OR text LIKE '当前没有未完成的日期提醒%')))", (int(time.time()),))
            self.db.execute('DELETE FROM pending_confirmation')
            self.db.execute('DELETE FROM delivered_confirmation')
            for old in ('task_view', 'date_view', 'list_messages'):
                self.db.execute(f'DROP TABLE IF EXISTS {old}')
        self.db.commit()

    def seen(self, event_id):
        return self.db.execute('SELECT 1 FROM incoming WHERE id=?', (event_id,)).fetchone() is not None

    def configure_channel(self, app_id, owner):
        identity = hashlib.sha256(f'qqbot:{app_id}:{owner}'.encode()).hexdigest()
        with self.db:
            previous = self.db.execute('SELECT identity FROM channel_state WHERE id=1').fetchone()
            if previous and previous['identity'] == identity:
                return
            # Lists/confirmations seen in an old chat cannot authorize actions
            # in the new bot. Preserve tasks, mail cursors and notifications.
            for table in ('list_pages', 'pending_confirmation', 'delivered_confirmation', 'incoming_queue'):
                self.db.execute(f'DELETE FROM {table}')
            self.db.execute("UPDATE outbox SET kind='superseded_channel', sent=? WHERE sent IS NULL "
                            "AND kind IN ('reply','task_list','date_list','confirm_preview')", (int(time.time()),))
            self.db.execute('INSERT OR REPLACE INTO channel_state VALUES (1,?)', (identity,))

    def queue_incoming(self, event_id, event):
        with self.db:
            if not self.seen(event_id):
                self.db.execute('INSERT OR IGNORE INTO incoming_queue VALUES (?,?)',
                                (event_id, json.dumps(event, ensure_ascii=False)))

    def queued_events(self):
        return [json.loads(row['payload']) for row in self.db.execute('SELECT payload FROM incoming_queue ORDER BY rowid')]

    def fail_incoming(self, event_id, event):
        self.handle_event(event_id, lambda: self.enqueue('reply', event_id, '未能处理这条消息，请重新发送。'),
                          reply_to=event['message_id'], reply_at=event.get('timestamp'))

    def handle_event(self, event_id, operation, *, reply_to=None, reply_at=None):
        """Record the event and all its effects in one transaction."""
        with self.db:
            if self.seen(event_id):
                return False
            self._reply_context = (reply_to, reply_at)
            try:
                operation()
            finally:
                self._reply_context = (None, None)
            self.db.execute('INSERT INTO incoming VALUES (?,?)', (event_id, int(time.time())))
            self.db.execute('DELETE FROM incoming_queue WHERE id=?', (event_id,))
        return True

    def enqueue(self, kind, ref, text, now=None, *, list_page=None, list_ids=None):
        self.db.execute('INSERT OR IGNORE INTO outbox(kind,ref,text,created,reply_to,reply_at,list_page,list_ids) VALUES (?,?,?,?,?,?,?,?)',
                        (kind, str(ref), text, int(time.time() if now is None else now), *self._reply_context, list_page, list_ids))

    def enqueue_list(self, view_kind, ref, rows, text, page=1):
        # A sender already waiting for a receipt holds its own copy of this page.
        self.db.execute("UPDATE outbox SET kind='superseded_list', sent=?, list_ids=NULL "
                        "WHERE kind=? AND list_page=? AND sent IS NULL", (int(time.time()), view_kind, page))
        self.enqueue(view_kind, ref, text, list_page=page, list_ids=json.dumps([row['id'] for row in rows]))

    def new_reminder(self, title, due):
        cur = self.db.execute('INSERT INTO reminders(title,due) VALUES (?,?)', (title, due))
        return cur.lastrowid

    def new_date_reminder(self, title, due, visible_from, visible_until, alerts):
        cur = self.db.execute("INSERT INTO reminders(title,due,kind,visible_from,visible_until) VALUES (?,?,'date',?,?)",
                              (title, due, visible_from, visible_until))
        self.db.executemany('INSERT INTO date_alerts(reminder_id,due) VALUES (?,?)',
                            [(cur.lastrowid, alert) for alert in alerts])
        return cur.lastrowid

    def active_reminders(self, now=None):
        now = int(time.time() if now is None else now)
        return self.db.execute("SELECT * FROM reminders WHERE status IN ('pending','notified') "
                               "AND (kind='task' OR (kind='date' AND visible_from<=? AND visible_until>?)) "
                               'ORDER BY due,id', (now, now)).fetchall()

    def date_reminders(self):
        return self.db.execute("SELECT * FROM reminders WHERE kind='date' AND status IN ('pending','notified') ORDER BY due,id").fetchall()

    def date_alert_times(self, reminder_id):
        return [r['due'] for r in self.db.execute('SELECT due FROM date_alerts WHERE reminder_id=? ORDER BY due', (reminder_id,))]

    def positions_match(self, kind, rows, numbers):
        for number in numbers:
            if not 1 <= number <= len(rows):
                return False
            page, offset = divmod(number - 1, PAGE_SIZE)
            view = self.db.execute('SELECT ids FROM list_pages WHERE kind=? AND page=?', (kind, page + 1)).fetchone()
            ids = json.loads(view['ids']) if view else []
            if offset >= len(ids) or ids[offset] != rows[number - 1]['id']:
                return False
        return True

    def prepare_confirmation(self, action, rows):
        if action not in ('completed', 'cancelled') or not 1 <= len(rows) <= 10:
            raise ValueError('invalid confirmation')
        token = secrets.token_hex(16)
        targets = [{key: row[key] for key in ('id', 'title', 'due', 'kind')} for row in rows]
        # Keep row IDs occupied: a superseded preview can still be in flight.
        self.db.execute("UPDATE outbox SET kind='superseded_preview', sent=? "
                        "WHERE kind='confirm_preview' AND sent IS NULL", (int(time.time()),))
        self.db.execute('INSERT OR REPLACE INTO pending_confirmation(id,token,action,targets,expires) VALUES (1,?,?,?,?)',
                        (token, action, json.dumps(targets, ensure_ascii=False), 0))
        return token

    def confirmation_snapshot(self, now=None):
        now = int(time.time() if now is None else now)
        pending = self.db.execute('SELECT * FROM pending_confirmation WHERE id=1').fetchone()
        delivered = self.db.execute('SELECT token FROM delivered_confirmation WHERE id=1').fetchone()
        if not pending or not delivered or pending['token'] != delivered['token'] or pending['expires'] <= now:
            return None
        return {'token': pending['token'], 'action': pending['action'], 'targets': json.loads(pending['targets'])}

    def confirm(self, expected_token, now=None):
        now = int(time.time() if now is None else now)
        pending = self.db.execute('SELECT * FROM pending_confirmation WHERE id=1').fetchone()
        delivered = self.db.execute('SELECT token FROM delivered_confirmation WHERE id=1').fetchone()
        if pending is None:
            return None, '当前没有待确认的操作。'
        if not secrets.compare_digest(pending['token'], expected_token):
            return None, '待确认的事项已变化，请重新发起操作。'
        if not delivered or not secrets.compare_digest(pending['token'], delivered['token']):
            return None, '需要确认的事项尚未送达或已变化，请等待助手的消息，或重新发起操作。'
        self.db.execute('DELETE FROM pending_confirmation WHERE id=1')
        if now >= pending['expires']:
            return None, '已超过 10 分钟，请重新发起操作。'
        targets = json.loads(pending['targets'])
        if not self.targets_match(targets):
            return None, '事项已发生变化，本次操作未执行，请重新查看列表。'
        for target in targets:
            self.finish_reminder(target['id'], pending['action'])
        return (pending['action'], targets), None

    def targets_match(self, targets):
        for target in targets:
            row = self.db.execute('SELECT * FROM reminders WHERE id=?', (target['id'],)).fetchone()
            if (not row or row['status'] not in ('pending', 'notified')
                    or any(row[key] != target[key] for key in ('title', 'due', 'kind'))):
                return False
        return True

    def discard_confirmation(self, expected_token):
        pending = self.confirmation_snapshot()
        if not pending or not secrets.compare_digest(pending['token'], expected_token):
            return '待确认的操作已变化或过期，请重新查看。'
        self.db.execute('DELETE FROM pending_confirmation WHERE id=1')
        return '已放弃本次操作，待办没有变化。'

    def invalidate_confirmation(self):
        # A direct command supersedes any earlier natural-language preview.
        self.db.execute('DELETE FROM pending_confirmation WHERE id=1')
        self.db.execute("UPDATE outbox SET kind='superseded_preview', sent=? "
                        "WHERE kind='confirm_preview' AND sent IS NULL", (int(time.time()),))

    def finish_reminder(self, reminder_id, status):
        cur = self.db.execute("UPDATE reminders SET status=? WHERE id=? AND status IN ('pending','notified')", (status, reminder_id))
        if cur.rowcount:
            # Keep IDs occupied while a send/ack may still be in flight.
            self.db.execute("UPDATE outbox SET sent=?, text='' WHERE sent IS NULL AND "
                            "((kind='reminder' AND ref=?) OR (kind='date_reminder' AND ref LIKE ?))",
                            (int(time.time()), str(reminder_id), f'{reminder_id}:%'))
        return bool(cur.rowcount)

    def schedule_due(self, now):
        with self.db:
            for row in self.db.execute("SELECT * FROM reminders WHERE kind='task' AND status='pending' AND due<=?", (now,)).fetchall():
                self.enqueue('reminder', row['id'], f"待办提醒：{row['title']}\n原定时间：{format_time(row['due'])}\n发送 /list 查看当前序号。", now)
            alerts = self.db.execute("SELECT a.reminder_id,a.due FROM date_alerts a JOIN reminders r ON r.id=a.reminder_id "
                                     "WHERE a.queued=0 AND a.due<=? AND r.status IN ('pending','notified')", (now,)).fetchall()
            for alert in alerts:
                self.enqueue('date_reminder', f"{alert['reminder_id']}:{alert['due']}", '', now)
                self.db.execute('UPDATE date_alerts SET queued=1 WHERE reminder_id=? AND due=?',
                                (alert['reminder_id'], alert['due']))

    def pending_outbox(self, now=None):
        now = int(time.time() if now is None else now)
        row = outbox_message(self.db.execute(
            'SELECT * FROM outbox WHERE sent IS NULL AND paused_reason IS NULL AND retry_at<=? '
            'AND (date_batch IS NULL OR date_batch=id) ORDER BY id LIMIT 1', (now,)).fetchone())
        if not row or row['kind'] != 'date_reminder':
            return row
        reminder_id = int(row['ref'].split(':', 1)[0])
        with self.db:
            # Freeze membership before the first network request. Later alerts form their own batch.
            claimed = self.db.execute('UPDATE outbox SET date_batch=id WHERE id=? AND date_batch IS NULL '
                                      'AND sent IS NULL AND paused_reason IS NULL', (row['id'],)).rowcount
            if claimed:
                self.db.execute("UPDATE outbox SET date_batch=? WHERE kind='date_reminder' AND ref LIKE ? "
                                "AND date_batch IS NULL AND sent IS NULL AND paused_reason IS NULL AND retry_at<=?",
                                (row['id'], f'{reminder_id}:%', now))
            grouped = self.db.execute('SELECT id,ref,text FROM outbox WHERE date_batch=? AND sent IS NULL '
                                      'AND paused_reason IS NULL ORDER BY id', (row['id'],)).fetchall()
            if not grouped:
                return None
            row['batch'] = [(item['id'], item['ref']) for item in grouped]
            if claimed:
                reminder = self.db.execute('SELECT title,due FROM reminders WHERE id=?', (reminder_id,)).fetchone()
                scheduled = sorted(int(ref.split(':', 1)[1]) for _, ref in row['batch'])
                label = '迟到的日期提醒' if now - scheduled[0] > 60 else '日期提醒'
                row['text'] = (f"{label}：{reminder['title']}\n日期：{format_date(reminder['due'])}"
                               + '\n原定提醒：' + '、'.join(format_time(ts) for ts in scheduled))
                self.db.execute('UPDATE outbox SET text=? WHERE id=?', (row['text'], row['id']))
            else:
                row['text'] = grouped[0]['text']
        return row

    def defer_message(self, row, now, min_delay=0):
        delay = max(min_delay, min(300, 15 * 2 ** min(row['attempts'], 5)))
        with self.db:
            for outbox_id, ref in row['batch']:
                self.db.execute('UPDATE outbox SET attempts=attempts+1, retry_at=? '
                                'WHERE id=? AND kind=? AND ref=? AND sent IS NULL',
                                (now + delay, outbox_id, row['kind'], ref))

    def pause_message(self, row, reason):
        with self.db:
            for outbox_id, ref in row['batch']:
                self.db.execute('UPDATE outbox SET paused_reason=?, attempts=attempts+1 '
                                'WHERE id=? AND kind=? AND ref=? AND sent IS NULL',
                                (reason, outbox_id, row['kind'], ref))

    def resume_deliveries(self):
        """A service restart explicitly retries paused, still-pending messages."""
        with self.db:
            self.db.execute('UPDATE outbox SET paused_reason=NULL, retry_at=0 '
                            'WHERE sent IS NULL AND paused_reason IS NOT NULL')

    def mark_sent(self, row, now):
        with self.db:
            matched = False
            # Even an obsolete list/confirmation may arrive. Record what was
            # delivered so its stale positions cannot authorize a newer action.
            superseded = {'task_list': 'superseded_list', 'date_list': 'superseded_list',
                          'confirm_preview': 'superseded_preview'}.get(row['kind'], row['kind'])
            for outbox_id, ref in row['batch']:
                current = self.db.execute('SELECT kind FROM outbox WHERE id=? AND ref=?', (outbox_id, ref)).fetchone()
                if not current or current['kind'] not in (row['kind'], superseded):
                    continue
                matched = True
                self.db.execute('UPDATE outbox SET sent=? WHERE id=? AND ref=? AND sent IS NULL', (now, outbox_id, ref))
            if not matched:
                return
            if row['kind'] == 'reminder':
                self.db.execute("UPDATE reminders SET status='notified' WHERE id=? AND status='pending'", (row['ref'],))
            elif row['kind'] == 'date_reminder':
                self.db.execute("UPDATE reminders SET status='notified' WHERE id=? AND status='pending'", (row['ref'].split(':', 1)[0],))
            elif row['kind'] == 'confirm_preview':
                self.db.execute('INSERT OR REPLACE INTO delivered_confirmation(id,token) VALUES (1,?)', (row['ref'],))
                self.db.execute('UPDATE pending_confirmation SET expires=? WHERE id=1 AND token=?', (now + 600, row['ref']))
            elif row['kind'] in ('task_list', 'date_list'):
                kind = 'task' if row['kind'] == 'task_list' else 'date'
                self.db.execute('INSERT OR REPLACE INTO list_pages VALUES (?,?,?)',
                                (kind, row['list_page'], row['list_ids']))
                self.db.execute('UPDATE outbox SET list_ids=NULL WHERE id=?', (row['id'],))

    def mailbox_state(self, box_id):
        return self.db.execute('SELECT * FROM mailbox_state WHERE id=?', (box_id,)).fetchone()

    def mailbox_baseline(self, box_id, uidvalidity, max_uid):
        with self.db:
            self.db.execute('INSERT INTO mailbox_state VALUES (?,?,?)', (box_id, uidvalidity, max_uid))

    def mailbox_message(self, box_id, uidvalidity, uid, text):
        with self.db:
            state = self.mailbox_state(box_id)
            if state is None or state['uidvalidity'] != uidvalidity:
                raise ValueError('mailbox state changed')
            if uid <= state['last_uid']:
                return
            self.enqueue('mail', f'{box_id}:{uidvalidity}:{uid}', text)
            self.db.execute('UPDATE mailbox_state SET last_uid=? WHERE id=?', (uid, box_id))

    def mailbox_health(self, box_id, name, success):
        from .mail import clean_header
        with self.db:
            row = self.db.execute('SELECT failures, alerted FROM mailbox_health WHERE id=?', (box_id,)).fetchone()
            failures, alerted = (row['failures'], bool(row['alerted'])) if row else (0, False)
            if success:
                if alerted:
                    self.enqueue('mail_health', uuid.uuid4().hex, f'✅ 邮箱（{clean_header(name)}）检查已恢复。')
                failures, alerted = 0, False
            else:
                failures += 1
                if failures >= 3 and not alerted:
                    self.enqueue('mail_health', uuid.uuid4().hex,
                                 f'⚠️ 邮箱（{clean_header(name)}）连续 3 次检查失败，可能无法收到新邮件通知，请检查服务日志。')
                    alerted = True
            self.db.execute('INSERT INTO mailbox_health VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET failures=excluded.failures, alerted=excluded.alerted',
                            (box_id, failures, int(alerted)))


def format_time(timestamp):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(timestamp, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M（北京时间）')


def format_date(timestamp):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(timestamp, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
