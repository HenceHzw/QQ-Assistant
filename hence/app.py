import asyncio
import logging
import re
import time
from datetime import datetime

from .config import Settings
from .commands import HELP
from .mail import poll_mailbox
from .graph import OAuthError, fetch_page
from .harness import ask_harness, validated_reminder
from .onebot import OneBot
from .reminders import ZONE, parse_with_llm
from .store import Store, format_time

log = logging.getLogger(__name__)
PAGE_SIZE = 5
MAX_ID = 2**63 - 1


def message_text(event):
    message = event.get('message', [])
    if isinstance(message, list):
        return ''.join(str(p.get('data', {}).get('text', '')) for p in message
                       if isinstance(p, dict) and p.get('type') == 'text').strip()
    if isinstance(message, str) and '[CQ:' not in message:
        return message.strip()
    return ''


def format_reminders(rows, page=1):
    if not rows:
        return '当前没有待办。'
    pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    if page > pages:
        return f'没有第 {page} 页，共 {pages} 页。'
    start = (page - 1) * PAGE_SIZE
    response = f'当前待办（第 {page}/{pages} 页，共 {len(rows)} 项）：\n' + '\n'.join(
        f"#{r['id']} {r['title']} — {format_time(r['due'])}" + ('（已提醒）' if r['status'] == 'notified' else '')
        for r in rows[start:start + PAGE_SIZE])
    if page < pages:
        response += f'\n发送 /list {page + 1} 查看下一页。'
    return response


class App:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self.events = asyncio.Queue(maxsize=200)
        self.commands = asyncio.Queue(maxsize=200)
        self.bot = OneBot(settings, self.accept_event)

    async def accept_event(self, event):
        if event.get('message_type') != 'private' or str(event.get('user_id')) != str(self.settings.owner_qq):
            return
        if event.get('message_id') is None:
            return
        try:
            queue = self.commands if message_text(event).startswith('/') else self.events
            queue.put_nowait(event)
        except asyncio.QueueFull:
            log.error('Incoming queue is full; message was not processed')

    async def handle_event(self, event):
        event_id = f"{event.get('self_id')}:{event['message_id']}"
        if self.store.seen(event_id):
            return
        text = message_text(event)
        if not text:
            return
        if len(text) > 2000:
            response = '消息太长，请在 2000 字以内描述待办。'
            operation = lambda: None
        elif text == '/help':
            response, operation = HELP, lambda: None
        elif match := re.fullmatch(r'/list(?:\s+([1-9]\d{0,8}))?', text):
            response = format_reminders(self.store.active_reminders(), int(match[1] or 1))
            operation = lambda: None
        elif (match := re.fullmatch(r'/(complete|cancel)\s+([1-9]\d{0,18})', text)) and int(match[2]) <= MAX_ID:
            action, reminder_id = ('完成' if match[1] == 'complete' else '取消'), int(match[2])
            row = self.store.db.execute("SELECT id FROM reminders WHERE id=? AND status IN ('pending','notified')", (reminder_id,)).fetchone()
            response = f"已{action}待办 #{reminder_id}。" if row else f'找不到未完成的待办 #{reminder_id}。'
            operation = lambda: self.store.finish_reminder(reminder_id, 'completed' if action == '完成' else 'cancelled')
        elif text.startswith('/'):
            match = re.fullmatch(r'/add\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+(.+)', text, re.DOTALL)
            if match:
                title = match[3].strip()
                try:
                    due = datetime.strptime(match[1] + ' ' + match[2], '%Y-%m-%d %H:%M').replace(tzinfo=ZONE)
                    if not title or len(title) > 300 or due <= datetime.now(ZONE):
                        raise ValueError('invalid title or time')
                except ValueError:
                    response = '事项须为 1–300 字，提醒时间须是有效的未来北京时间。发送 /help 查看格式。'
                    operation = lambda: None
                else:
                    def operation():
                        reminder_id = self.store.new_reminder(title, int(due.timestamp()))
                        self.store.enqueue('reply', event_id, f'已创建待办 #{reminder_id}：{title}\n提醒时间：{format_time(int(due.timestamp()))}')
                    response = None
            else:
                response, operation = '指令或格式不正确，发送 /help 查看可用指令。', lambda: None
        elif self.settings.harness_url:
            now = datetime.now(ZONE)
            try:
                result = await asyncio.to_thread(ask_harness, self.settings.harness_url, self.settings.harness_token,
                                                 text, now, self.store.active_reminders())
                action = result.get('action')
                if action == 'create':
                    title, due = validated_reminder(result, datetime.now(ZONE))
                    def operation():
                        reminder_id = self.store.new_reminder(title, due)
                        self.store.enqueue('reply', event_id, f'已创建待办 #{reminder_id}：{title}\n提醒时间：{format_time(due)}')
                    response = None
                elif action == 'list':
                    response = format_reminders(self.store.active_reminders())
                    operation = lambda: None
                elif action in ('complete', 'cancel') and type(result.get('id')) is int and 0 < result['id'] <= MAX_ID:
                    reminder_id = result['id']
                    label = '完成' if action == 'complete' else '取消'
                    row = self.store.db.execute("SELECT id FROM reminders WHERE id=? AND status IN ('pending','notified')", (reminder_id,)).fetchone()
                    response = f'已{label}待办 #{reminder_id}。' if row else f'找不到未完成的待办 #{reminder_id}。'
                    def operation():
                        self.store.finish_reminder(reminder_id, 'completed' if action == 'complete' else 'cancelled')
                elif action == 'chat' and isinstance(result.get('reply'), str) and result['reply'].strip():
                    response = result['reply'].strip()[:4000]
                    operation = lambda: None
                else:
                    raise ValueError('invalid Harness result')
            except Exception as exc:
                log.warning('Harness request failed: %s', type(exc).__name__)
                response = '助手暂时无法连接大模型，请稍后再试。发送 /help 查看离线指令。'
                operation = lambda: None
        else:
            now = datetime.now(ZONE)
            parsed = (None, None, '当前未启用大模型，请发送 /help 查看固定指令。')
            if self.settings.llm_url:
                try:
                    parsed = await asyncio.to_thread(parse_with_llm, text, now, self.settings)
                except Exception as exc:
                    log.warning('Reminder model unavailable or returned invalid output: %s', type(exc).__name__)
                    parsed = (None, None, '助手暂时无法连接大模型，请稍后再试。发送 /help 查看离线指令。')
            title, due, question = parsed
            if title:
                def operation():
                    reminder_id = self.store.new_reminder(title, due)
                    self.store.enqueue('reply', event_id, f'已创建待办 #{reminder_id}：{title}\n提醒时间：{format_time(due)}')
                response = None
            else:
                operation = lambda: None
                response = question
        def commit():
            operation()
            if response is not None:
                self.store.enqueue('reply', event_id, response)
        self.store.handle_event(event_id, commit)

    async def event_loop(self, queue):
        while True:
            event = await queue.get()
            try:
                await self.handle_event(event)
            except Exception:
                log.exception('Failed to process incoming event')
            finally:
                queue.task_done()

    async def scheduler_loop(self):
        while True:
            try:
                self.store.schedule_due(int(time.time()))
            except Exception:
                log.exception('Failed to schedule reminders')
            await asyncio.sleep(5)

    async def dispatch_loop(self):
        while True:
            row = self.store.pending_outbox()
            if row is None:
                await asyncio.sleep(2)
                continue
            try:
                await self.bot.send(self.settings.owner_qq, row['text'])
                self.store.mark_sent(row, int(time.time()))
            except Exception as exc:
                log.warning('Message delivery failed (%s); will retry', type(exc).__name__)
                self.store.defer_message(row, int(time.time()))
                if isinstance(exc, ConnectionError):
                    await asyncio.sleep(2)

    async def mail_loop(self):
        while True:
            for box in self.settings.mailboxes:
                try:
                    if box.get('type', 'imap') == 'graph':
                        # Persist each page and cursor together so a restart resumes safely.
                        for _ in range(20):
                            state = self.store.graph_state(box['id'])
                            page = await asyncio.to_thread(fetch_page, box, state['cursor'] if state else None)
                            baseline = bool(state['baseline']) if state else True
                            self.store.graph_page(box['id'], box.get('name', box['id']), page, baseline)
                            if '@odata.deltaLink' in page:
                                break
                    else:
                        state = self.store.mailbox_state(box['id'])
                        result = await asyncio.to_thread(poll_mailbox, box, dict(state) if state else None)
                        validity, baseline, messages = result
                        if messages is None:
                            self.store.mailbox_baseline(box['id'], validity, baseline)
                            log.info('Mailbox %s initialized', box['id'])
                        else:
                            for uid, text in messages:
                                self.store.mailbox_message(box['id'], validity, uid, text)
                    self.store.mailbox_health(box['id'], box.get('name', box['id']), True)
                except Exception as exc:
                    # Report only known safe error codes, never provider response bodies or credentials.
                    if isinstance(exc, OAuthError) and re.fullmatch(r'[a-z_]+', str(exc)):
                        detail = f'OAuth {exc}'
                    elif getattr(exc, 'code', None) in (401, 403, 410):
                        detail = f'HTTP {exc.code}'
                    elif str(exc).startswith('UIDVALIDITY changed;'):
                        detail = 'UIDVALIDITY changed; manual reset required'
                    else:
                        detail = type(exc).__name__
                    log.warning('Mailbox %s failed: %s; check authorization, network and synchronization state', box['id'], detail)
                    self.store.mailbox_health(box['id'], box.get('name', box['id']), False)
            await asyncio.sleep(self.settings.poll_seconds)

    async def run(self):
        await asyncio.gather(self.bot.run(), self.event_loop(self.events), self.event_loop(self.commands),
                             self.scheduler_loop(), self.dispatch_loop(), self.mail_loop())


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    settings = Settings.load()
    store = Store(settings.db_path)
    asyncio.run(App(settings, store).run())


if __name__ == '__main__':
    main()
