import asyncio
import logging
import re
import time
from datetime import datetime

from .config import Settings
from .commands import HELP
from .dates import parse_alerts, validate_date_reminder
from .mail import poll_mailbox
from .graph import OAuthError, fetch_page
from .harness import ask_harness
from .onebot import OneBot
from .reminders import ZONE, validate_reminder
from .store import Store, format_date, format_time

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
        f"{index}. {r['title']} — " + (format_date(r['due']) + '（日期提醒）' if r['kind'] == 'date' else format_time(r['due'])) + ('（已提醒）' if r['status'] == 'notified' else '')
        for index, r in enumerate(rows[start:start + PAGE_SIZE], start + 1))
    if page < pages:
        response += f'\n发送 /list {page + 1} 查看下一页。'
    response += '\n序号随列表变化。'
    return response


def format_dates(rows, store, page=1):
    if not rows:
        return '当前没有未完成的日期提醒。'
    pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    if page > pages:
        return f'没有第 {page} 页，共 {pages} 页。'
    start = (page - 1) * PAGE_SIZE
    lines = [f'日期提醒（第 {page}/{pages} 页，共 {len(rows)} 项）：']
    for number, row in enumerate(rows[start:start + PAGE_SIZE], start + 1):
        alerts = store.date_alert_times(row['id'])
        lines.append(f"{number}. {row['title']} — {format_date(row['due'])}"
                     + ('（已提醒）' if row['status'] == 'notified' else '')
                     + f"\n  显示开始：{format_date(row['visible_from'])}；提醒："
                     + ('、'.join(format_time(ts) for ts in alerts) if alerts else '无未来提醒'))
    if page < pages:
        lines.append(f'发送 /dates {page + 1} 查看下一页。')
    lines.append('日期提醒序号独立于 /list，且会随列表变化。')
    return '\n'.join(lines)


def date_created_text(title, due, visible_from, alerts, skipped):
    reply = (f'已创建日期提醒：{title}\n日期：{format_date(due)}\n显示开始：{format_date(visible_from)}'
             + '\n将提醒：' + ('、'.join(format_time(ts) for ts in alerts) if alerts else '无未来提醒'))
    if skipped:
        reply += '\n已过的提醒时间不会发送：' + '、'.join(format_time(ts) for ts in skipped)
    return reply + '\n发送 /dates 查看和管理。'


def format_confirmation(action, rows):
    label = '完成' if action == 'completed' else '取消'
    details = '\n'.join(f"{index}. {' '.join(row['title'].split())} — "
                        + (format_date(row['due']) if row['kind'] == 'date' else format_time(row['due']))
                        for index, row in enumerate(rows, 1))
    return f'请核对将要{label}的 {len(rows)} 项：\n{details}\n请在 10 分钟内回复“确认”继续，或回复“算了”放弃。'


class App:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self.events = asyncio.Queue(maxsize=200)
        self.commands = asyncio.Queue(maxsize=200)
        self.bot = OneBot(settings, self.accept_event)

    def prepare_confirmation(self, status, rows):
        token = self.store.prepare_confirmation(status, rows)
        self.store.enqueue('confirm_preview', token, format_confirmation(status, rows))

    def execute_confirmation(self, event_id, expected_token):
        result, error = self.store.confirm(expected_token)
        if error:
            reply = error
        else:
            action, targets = result
            label = '完成' if action == 'completed' else '取消'
            details = '\n'.join(f"{index}. {' '.join(row['title'].split())} — "
                                + (format_date(row['due']) if row['kind'] == 'date' else format_time(row['due']))
                                for index, row in enumerate(targets, 1))
            reply = f'已{label} {len(targets)} 项：\n{details}\n发送 /list 或 /dates 查看更新后的列表。'
        self.store.enqueue('reply', event_id, reply)

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
        elif match := re.fullmatch(r'/dates(?:\s+([1-9]\d{0,8}))?', text):
            rows = self.store.date_reminders()
            page = int(match[1] or 1)
            response = format_dates(rows, self.store, page)
            if page <= max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE):
                listing = response
                response = None
                operation = lambda: self.store.enqueue_list('date_list', event_id, rows, listing)
            else:
                operation = lambda: None
        elif (match := re.fullmatch(r'/date-(complete|cancel)\s+([1-9]\d{0,18})', text)) and int(match[2]) <= MAX_ID:
            action, number = match[1], int(match[2])
            response = None
            def operation():
                rows = self.store.date_reminders()
                if not self.store.date_view_matches(rows):
                    reply = '日期提醒列表可能已变化，请先发送 /dates 查看当前序号。'
                elif number > len(rows):
                    reply = f'找不到序号为 {number} 的日期提醒，请发送 /dates 查看。'
                else:
                    row = rows[number - 1]
                    status = 'completed' if action == 'complete' else 'cancelled'
                    self.store.invalidate_confirmation()
                    self.store.finish_reminder(row['id'], status)
                    label = '完成' if action == 'complete' else '取消'
                    reply = f'已{label}日期提醒：{row["title"]} — {format_date(row["due"])}。\n发送 /dates 查看更新后的序号。'
                self.store.enqueue('reply', event_id, reply)
        elif match := re.fullmatch(r'/date-add\s+(\d{4}-\d{2}-\d{2})\s+(.+)', text, re.DOTALL):
            day, rest = match[1], match[2].strip()
            custom = re.fullmatch(r'--show\s+(\d{1,4})\s+--alerts\s+(\S+)\s+(.+)', rest, re.DOTALL)
            try:
                if custom:
                    show_days, alerts, title = int(custom[1]), parse_alerts(custom[2]), custom[3]
                elif rest.startswith(('--show', '--alerts')) or re.match(r'\d+\s+\d+@', rest):
                    raise ValueError('invalid custom date syntax')
                else:
                    show_days, alerts, title = 2, [{'days': 2, 'time': '09:00'}, {'days': 0, 'time': '09:00'}], rest
                title, due, start, end, future, skipped = validate_date_reminder(title, day, show_days, alerts, datetime.now(ZONE))
            except (ValueError, OverflowError):
                response, operation = '日期提醒格式或时间无效，发送 /help 查看格式。', lambda: None
            else:
                response = None
                def operation():
                    self.store.new_date_reminder(title, due, start, end, future)
                    self.store.enqueue('reply', event_id, date_created_text(title, due, start, future, skipped))
        elif match := re.fullmatch(r'/list(?:\s+([1-9]\d{0,8}))?', text):
            rows = self.store.active_reminders()
            page = int(match[1] or 1)
            response = format_reminders(rows, page)
            if page <= max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE):
                listing = response
                response = None
                operation = lambda: self.store.enqueue_list('task_list', event_id, rows, listing)
            else:
                operation = lambda: None
        elif (match := re.fullmatch(r'/(complete|cancel)\s+([1-9]\d{0,18})', text)) and int(match[2]) <= MAX_ID:
            action, number = match[1], int(match[2])
            response = None
            def operation():
                rows = self.store.active_reminders()
                if not self.store.task_view_matches(rows):
                    reply = '待办列表可能已变化，请先发送 /list 查看当前序号。'
                elif number > len(rows):
                    reply = f'找不到序号为 {number} 的待办，请发送 /list 查看。'
                else:
                    row = rows[number - 1]
                    status = 'completed' if action == 'complete' else 'cancelled'
                    self.store.invalidate_confirmation()
                    self.store.finish_reminder(row['id'], status)
                    label = '完成' if action == 'complete' else '取消'
                    reply = f'已{label}待办：{row["title"]} — ' + (format_date(row['due']) if row['kind'] == 'date' else format_time(row['due'])) + '。\n发送 /list 查看更新后的序号。'
                self.store.enqueue('reply', event_id, reply)
        elif text.startswith('/'):
            match = re.fullmatch(r'/add\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+(.+)', text, re.DOTALL)
            if match:
                try:
                    title, due = validate_reminder(match[3], match[1] + 'T' + match[2] + ':00+08:00', datetime.now(ZONE))
                except ValueError:
                    response = '事项须为 1–300 字，提醒时间须是有效的未来北京时间。发送 /help 查看格式。'
                    operation = lambda: None
                else:
                    def operation():
                        self.store.new_reminder(title, due)
                        self.store.enqueue('reply', event_id, f'已创建待办：{title}\n提醒时间：{format_time(due)}\n发送 /list 查看当前序号。')
                    response = None
            else:
                response, operation = '指令或格式不正确，发送 /help 查看可用指令。', lambda: None
        elif self.settings.harness_url:
            now = datetime.now(ZONE)
            try:
                snapshot = self.store.active_reminders()
                dates_snapshot = self.store.date_reminders()
                dates_for_model = [dict(row) for row in dates_snapshot]
                for row in dates_for_model[:30]:
                    row['alerts'] = self.store.date_alert_times(row['id'])
                pending_snapshot = self.store.confirmation_snapshot()
                pending_for_model = None if pending_snapshot is None else {
                    'action': pending_snapshot['action'],
                    'targets': [{'title': item['title'], 'kind': item['kind'],
                                 'due': format_date(item['due']) if item['kind'] == 'date' else format_time(item['due'])}
                                for item in pending_snapshot['targets']]}
                result = await asyncio.to_thread(ask_harness, self.settings.harness_url, self.settings.harness_token,
                                                 text, now, snapshot, dates_for_model, pending_for_model)
                action = result.get('action')
                if action == 'create':
                    title, due = validate_reminder(result.get('title'), result.get('due'), datetime.now(ZONE))
                    def operation():
                        self.store.new_reminder(title, due)
                        self.store.enqueue('reply', event_id, f'已创建待办：{title}\n提醒时间：{format_time(due)}\n发送 /list 查看当前序号。')
                    response = None
                elif action == 'create_date':
                    title, due, start, end, future, skipped = validate_date_reminder(
                        result.get('title'), result.get('date'), result.get('show_days', 2),
                        result.get('alerts', [{'days': 2, 'time': '09:00'}, {'days': 0, 'time': '09:00'}]), datetime.now(ZONE))
                    def operation():
                        self.store.new_date_reminder(title, due, start, end, future)
                        self.store.enqueue('reply', event_id, date_created_text(title, due, start, future, skipped))
                    response = None
                elif action == 'list':
                    rows = self.store.active_reminders()
                    listing = format_reminders(rows)
                    response = None
                    operation = lambda: self.store.enqueue_list('task_list', event_id, rows, listing)
                elif action == 'list_dates':
                    rows = self.store.date_reminders()
                    listing = format_dates(rows, self.store)
                    response = None
                    operation = lambda: self.store.enqueue_list('date_list', event_id, rows, listing)
                elif action in ('confirm', 'discard_confirmation'):
                    response = None
                    def operation():
                        if pending_snapshot is None:
                            self.store.enqueue('reply', event_id, '当前没有已送达且有效的待确认操作。')
                        elif action == 'confirm':
                            self.execute_confirmation(event_id, pending_snapshot['token'])
                        else:
                            self.store.enqueue('reply', event_id, self.store.discard_confirmation(pending_snapshot['token']))
                elif action in ('complete', 'cancel', 'date_complete', 'date_cancel'):
                    date_mode = action.startswith('date_')
                    selected_snapshot = dates_snapshot if date_mode else snapshot
                    numbers = result.get('ids')
                    if numbers is None and 'id' in result:
                        numbers = [result['id']]
                    sent_count = result.get('_dates_sent' if date_mode else '_tasks_sent', min(30, len(selected_snapshot)))
                    reference = result.get('reference', 'position')
                    if (not isinstance(numbers, list) or not 1 <= len(numbers) <= 10
                            or reference not in ('description', 'position')
                            or type(sent_count) is not int or not 0 <= sent_count <= min(30, len(selected_snapshot))
                            or any(type(n) is not int or not 1 <= n <= sent_count for n in numbers)
                            or len(set(numbers)) != len(numbers)):
                        raise ValueError('invalid task positions')
                    label = '完成' if action in ('complete', 'date_complete') else '取消'
                    selected = [selected_snapshot[n - 1] for n in numbers]
                    response = None
                    def operation():
                        command = '/dates' if date_mode else '/list'
                        if reference == 'position':
                            rows = self.store.date_reminders() if date_mode else self.store.active_reminders()
                            view_matches = self.store.date_view_matches(rows) if date_mode else self.store.task_view_matches(rows)
                            if [row['id'] for row in rows] != [row['id'] for row in selected_snapshot] or not view_matches:
                                self.store.enqueue('reply', event_id, f'列表可能已变化，请先发送 {command} 查看当前序号，再重新操作。')
                                return
                        if not self.store.targets_match(selected):
                            self.store.enqueue('reply', event_id, f'相关事项已变化，请发送 {command} 查看后重新操作。')
                            return
                        status = 'completed' if label == '完成' else 'cancelled'
                        self.prepare_confirmation(status, selected)
                elif action == 'chat' and isinstance(result.get('reply'), str) and result['reply'].strip():
                    response = result['reply'].strip()[:4000]
                    operation = lambda: None
                else:
                    raise ValueError('invalid Harness result')
            except (ValueError, OverflowError) as exc:
                log.warning('Harness returned an invalid action: %s', type(exc).__name__)
                response = '无法确定待办操作，请查看 /list 或 /dates 后重新描述；一次最多处理 10 项。'
                operation = lambda: None
            except Exception as exc:
                log.warning('Harness request failed: %s', type(exc).__name__)
                response = ('助手暂时无法连接大模型。可发送 /list 或 /dates 查看，再用固定指令处理。'
                            if self.store.confirmation_snapshot() else
                            '助手暂时无法连接大模型，请稍后再试。发送 /help 查看离线指令。')
                operation = lambda: None
        else:
            response, operation = '当前未启用大模型，请发送 /help 查看固定指令。', lambda: None
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
