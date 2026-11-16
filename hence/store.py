import sqlite3
import time
import uuid
from pathlib import Path


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
        CREATE TABLE IF NOT EXISTS graph_state (id TEXT PRIMARY KEY, cursor TEXT NOT NULL, baseline INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS graph_seen (box_id TEXT NOT NULL, message_id TEXT NOT NULL, PRIMARY KEY(box_id, message_id));
        CREATE TABLE IF NOT EXISTS incoming (
          id TEXT PRIMARY KEY, received INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS mailbox_health (
          id TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0, alerted INTEGER NOT NULL DEFAULT 0);
        ''')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(outbox)')}
        if 'retry_at' not in columns:
            self.db.execute('ALTER TABLE outbox ADD COLUMN retry_at INTEGER NOT NULL DEFAULT 0')
        if 'attempts' not in columns:
            self.db.execute('ALTER TABLE outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0')
        self.db.commit()

    def seen(self, event_id):
        return self.db.execute('SELECT 1 FROM incoming WHERE id=?', (event_id,)).fetchone() is not None

    def handle_event(self, event_id, operation):
        """Record the event and all its effects in one transaction."""
        with self.db:
            if self.seen(event_id):
                return False
            operation()
            self.db.execute('INSERT INTO incoming VALUES (?,?)', (event_id, int(time.time())))
        return True

    def enqueue(self, kind, ref, text, now=None):
        self.db.execute('INSERT OR IGNORE INTO outbox(kind,ref,text,created) VALUES (?,?,?,?)',
                        (kind, str(ref), text, int(time.time() if now is None else now)))

    def new_reminder(self, title, due):
        cur = self.db.execute('INSERT INTO reminders(title,due) VALUES (?,?)', (title, due))
        return cur.lastrowid

    def active_reminders(self):
        return self.db.execute("SELECT * FROM reminders WHERE status IN ('pending','notified') ORDER BY due,id").fetchall()

    def finish_reminder(self, reminder_id, status):
        cur = self.db.execute("UPDATE reminders SET status=? WHERE id=? AND status IN ('pending','notified')", (status, reminder_id))
        if cur.rowcount:
            self.db.execute("DELETE FROM outbox WHERE kind='reminder' AND ref=? AND sent IS NULL", (str(reminder_id),))
        return bool(cur.rowcount)

    def schedule_due(self, now):
        with self.db:
            for row in self.db.execute("SELECT * FROM reminders WHERE status='pending' AND due<=?", (now,)).fetchall():
                self.enqueue('reminder', row['id'], f"待办提醒 #{row['id']}：{row['title']}\n原定时间：{format_time(row['due'])}", now)

    def pending_outbox(self, now=None):
        return self.db.execute('SELECT * FROM outbox WHERE sent IS NULL AND retry_at<=? ORDER BY id LIMIT 1',
                               (int(time.time() if now is None else now),)).fetchone()

    def defer_message(self, row, now):
        delay = min(300, 15 * 2 ** min(row['attempts'], 5))
        with self.db:
            self.db.execute('UPDATE outbox SET attempts=attempts+1, retry_at=? WHERE id=? AND sent IS NULL',
                            (now + delay, row['id']))

    def mark_sent(self, row, now):
        with self.db:
            self.db.execute('UPDATE outbox SET sent=? WHERE id=? AND sent IS NULL', (now, row['id']))
            if row['kind'] == 'reminder':
                self.db.execute("UPDATE reminders SET status='notified' WHERE id=? AND status='pending'", (row['ref'],))

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

    def graph_state(self, box_id):
        return self.db.execute('SELECT * FROM graph_state WHERE id=?', (box_id,)).fetchone()

    def graph_page(self, box_id, name, page, was_baseline):
        import hashlib
        from .mail import clean_header
        next_url = page.get('@odata.nextLink') or page.get('@odata.deltaLink')
        if not next_url or not isinstance(page.get('value'), list):
            raise ValueError('Graph delta response missing cursor or messages')
        baseline = bool(was_baseline and '@odata.deltaLink' not in page)
        with self.db:
            for msg in page['value']:
                msg_id = msg.get('id')
                if not msg_id or '@removed' in msg:
                    continue
                inserted = self.db.execute('INSERT OR IGNORE INTO graph_seen VALUES (?,?)', (box_id, msg_id)).rowcount
                if inserted and not was_baseline:
                    sender = ((msg.get('from') or {}).get('emailAddress') or {})
                    sender_text = sender.get('name') or sender.get('address') or ''
                    text = f"📬 新邮件（{name}）\n发件人：{clean_header(sender_text)}\n主题：{clean_header(msg.get('subject'))}"
                    key = hashlib.sha256((box_id + '\0' + msg_id).encode()).hexdigest()
                    self.enqueue('graph_mail', key, text)
            self.db.execute('INSERT INTO graph_state VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor, baseline=excluded.baseline',
                            (box_id, next_url, int(baseline)))

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
