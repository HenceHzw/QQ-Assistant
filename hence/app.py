import asyncio
import logging
import re
import sqlite3
import time
from datetime import datetime
from functools import partial

from .config import Settings
from .commands import HELP, PAGE_SIZE
from .dates import DEFAULT_ALERTS, parse_alerts, validate_date_reminder
from .mail import poll_mailbox
from .harness import SNAPSHOT_LIMIT, ask_harness
from .qqbot import BotAPIError, QQBot, retryable_delivery_error
from .reminders import ZONE, validate_reminder
from .store import Store, format_date, format_time

log = logging.getLogger(__name__)
INVALID_ACTION = "无法确定这次操作，请重新描述，或发送 /help 查看固定指令。"
SEND_INTERVAL = 3.1  # Official C2C limit: 20 messages/minute per recipient.


def message_text(event):
    message = event.get('message', '')
    return message.strip() if isinstance(message, str) else ''


def page_command(kind, number):
    command = '/dates' if kind == 'date' else '/list'
    page = (number - 1) // PAGE_SIZE + 1
    return command if page == 1 else f'{command} {page}'


def format_task(row):
    due = format_date(row['due']) + '（日期提醒）' if row['kind'] == 'date' else format_time(row['due'])
    return f"{row['title']} — {due}" + ('（已提醒）' if row['status'] == 'notified' else '')


def date_created_text(title, due, visible_from, alerts, skipped):
    reply = (f'已创建日期提醒：{title}\n日期：{format_date(due)}\n显示开始：{format_date(visible_from)}'
             + '\n将提醒：' + ('、'.join(format_time(ts) for ts in alerts) if alerts else '无未来提醒'))
    if skipped:
        reply += '\n已过的提醒时间不会发送：' + '、'.join(format_time(ts) for ts in skipped)
    return reply + '\n发送 /dates 查看和管理。'


def describe_reminder(row):
    return ' '.join(row['title'].split()) + ' — ' + (format_date(row['due']) if row['kind'] == 'date' else format_time(row['due']))


def format_targets(rows):
    return '\n'.join(f'{index}. {describe_reminder(row)}' for index, row in enumerate(rows, 1))


class App:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self.events = asyncio.Queue(maxsize=200)
        self.commands = asyncio.Queue(maxsize=200)
        self.store.configure_channel(settings.qq_app_id, settings.owner_openid)
        self.store.resume_deliveries()
        self.next_send = 0
        self.bot = QQBot(settings.qq_app_id, settings.owner_openid, self.accept_event,
                         settings.harness_token, settings.qq_bridge_url)

    def event_id(self, event):
        return f"qqbot:{self.settings.qq_app_id}:{event['message_id']}"

    def list_rows(self, kind):
        return self.store.date_reminders() if kind == 'date' else self.store.active_reminders()

    def show_list(self, event_id, kind, page=1):
        rows = self.list_rows(kind)
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        if page > pages:
            return f'没有第 {page} 页，共 {pages} 页。'
        start = (page - 1) * PAGE_SIZE
        visible = rows[start:start + PAGE_SIZE]
        dates = kind == 'date'
        heading = '日期提醒' if dates else '当前待办'
        if visible:
            lines = [f'{heading}（第 {page}/{pages} 页，共 {len(rows)} 项）：']
            for number, row in enumerate(visible, start + 1):
                if dates:
                    alerts = '、'.join(format_time(ts) for ts in self.store.date_alert_times(row['id']))
                    status = '（已提醒）' if row['status'] == 'notified' else ''
                    lines.append(f"{number}. {row['title']} — {format_date(row['due'])}{status}"
                                 f"\n  显示开始：{format_date(row['visible_from'])}；提醒：{alerts or '无未来提醒'}")
                else:
                    lines.append(f'{number}. {format_task(row)}')
            if page < pages:
                lines.append(f'发送 {page_command(kind, start + PAGE_SIZE + 1)} 查看下一页。')
            lines.append('日期提醒序号独立于 /list，且会随列表变化。' if dates else '序号随列表变化。')
            text = '\n'.join(lines)
        else:
            text = '当前没有未完成的日期提醒。' if dates else '当前没有待办。'
        self.store.enqueue_list(kind + '_list', event_id, visible, text, page)

    def add_task(self, title, due):
        self.store.new_reminder(title, due)
        return f'已创建待办：{title}\n提醒时间：{format_time(due)}\n发送 /list 查看当前序号。'

    def add_date(self, values):
        title, due, start, end, future, skipped = values
        self.store.new_date_reminder(title, due, start, end, future)
        return date_created_text(title, due, start, future, skipped)

    def finish_position(self, kind, action, number):
        rows = self.list_rows(kind)
        command, noun = ('/dates', '日期提醒') if kind == 'date' else ('/list', '待办')
        if number > len(rows):
            return f'找不到序号为 {number} 的{noun}，请发送 {command} 查看。'
        if not self.store.positions_match(kind, rows, [number]):
            return f'请先发送 {page_command(kind, number)} 查看当前序号。'
        row = rows[number - 1]
        status, label = ('completed', '完成') if action == 'complete' else ('cancelled', '取消')
        self.store.invalidate_confirmation()
        self.store.finish_reminder(row['id'], status)
        return f'已{label}{noun}：{describe_reminder(row)}。\n发送 {command} 查看更新后的序号。'

    def prepare_confirmation(self, status, rows):
        token = self.store.prepare_confirmation(status, rows)
        label = '完成' if status == 'completed' else '取消'
        text = f'请核对将要{label}的 {len(rows)} 项：\n{format_targets(rows)}\n请在 10 分钟内回复“确认”继续，或回复“算了”放弃。'
        self.store.enqueue('confirm_preview', token, text)

    def execute_confirmation(self, expected_token):
        result, error = self.store.confirm(expected_token)
        if error:
            return error
        action, targets = result
        label = '完成' if action == 'completed' else '取消'
        return f'已{label} {len(targets)} 项：\n{format_targets(targets)}\n发送 /list 或 /dates 查看更新后的列表。'

    async def accept_event(self, event):
        if event.get('message_type') != 'private' or event.get('user_id') != self.settings.owner_openid:
            return
        if not event.get('message_id') or not message_text(event) or self.store.seen(self.event_id(event)):
            return
        self.store.queue_incoming(self.event_id(event), event)
        try:
            queue = self.commands if message_text(event).startswith('/') else self.events
            queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning('Incoming queue is full; saved message will be replayed')
            return False

    async def recover_incoming(self):
        for event in self.store.queued_events():
            if event.get('user_id') == self.settings.owner_openid:
                queue = self.commands if message_text(event).startswith('/') else self.events
                await queue.put(event)

    def run_command(self, event_id, text):
        if text == '/help':
            return HELP
        if match := re.fullmatch(r'/(list|dates)(?:\s+([1-9]\d{0,8}))?', text):
            kind = 'date' if match[1] == 'dates' else 'task'
            return self.show_list(event_id, kind, int(match[2] or 1))
        if match := re.fullmatch(r'/(date-)?(complete|cancel)\s+([1-9]\d{0,18})', text):
            return self.finish_position('date' if match[1] else 'task', match[2], int(match[3]))
        if match := re.fullmatch(r'/add\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+(.+)', text, re.DOTALL):
            try:
                title, due = validate_reminder(match[3], f'{match[1]}T{match[2]}:00+08:00', datetime.now(ZONE))
            except ValueError:
                return '事项须为 1–300 字，提醒时间须是有效的未来北京时间。发送 /help 查看格式。'
            return self.add_task(title, due)
        if match := re.fullmatch(r'/date-add\s+(\d{4}-\d{2}-\d{2})\s+(.+)', text, re.DOTALL):
            day, rest = match[1], match[2].strip()
            custom = re.fullmatch(r'--show\s+(\d{1,4})\s+--alerts\s+(\S+)\s+(.+)', rest, re.DOTALL)
            try:
                if custom:
                    values = validate_date_reminder(custom[3], day, int(custom[1]), parse_alerts(custom[2]), datetime.now(ZONE))
                elif rest.startswith(('--show', '--alerts')) or re.match(r'\d+\s+\d+@', rest):
                    raise ValueError('invalid custom date syntax')
                else:
                    values = validate_date_reminder(rest, day, 2, DEFAULT_ALERTS, datetime.now(ZONE))
            except (ValueError, OverflowError):
                return '日期提醒格式或时间无效，发送 /help 查看格式。'
            return self.add_date(values)
        return '指令或格式不正确，发送 /help 查看可用指令。'

    async def model_turn(self, event_id, text):
        tasks = self.store.active_reminders()
        dates = self.store.date_reminders()
        date_context = [dict(row, alerts=self.store.date_alert_times(row['id']))
                        for row in dates[:SNAPSHOT_LIMIT]]
        pending = self.store.confirmation_snapshot()
        confirmation = None if pending is None else {
            'action': pending['action'],
            'targets': [{'title': item['title'], 'kind': item['kind'],
                         'due': format_date(item['due']) if item['kind'] == 'date' else format_time(item['due'])}
                        for item in pending['targets']]}
        try:
            result = await asyncio.to_thread(ask_harness, self.settings.harness_url, self.settings.harness_token,
                                             text, datetime.now(ZONE), tasks, date_context, confirmation, len(dates))
        except ValueError:
            return INVALID_ACTION
        except Exception as exc:
            log.warning('Harness request failed: %s', type(exc).__name__)
            if self.store.confirmation_snapshot():
                return '助手暂时无法连接大模型。可发送 /list 或 /dates 查看，再用固定指令处理。'
            return '助手暂时无法连接大模型，请稍后再试。发送 /help 查看离线指令。'
        return partial(self.apply_action, event_id, result, tasks, dates, pending)

    def apply_action(self, event_id, result, tasks, dates, pending):
        action = result.get('action')
        if action in ('create', 'create_date'):
            now = datetime.now(ZONE)
            try:
                if action == 'create':
                    title, due = validate_reminder(result.get('title'), result.get('due'), now)
                else:
                    values = validate_date_reminder(result.get('title'), result.get('date'), result.get('show_days', 2),
                                                    result.get('alerts', DEFAULT_ALERTS), now)
            except (ValueError, OverflowError):
                return INVALID_ACTION
            if action == 'create':
                return self.add_task(title, due)
            return self.add_date(values)
        if action in ('list', 'list_dates'):
            return self.show_list(event_id, 'date' if action == 'list_dates' else 'task')
        if action in ('confirm', 'discard_confirmation'):
            if pending is None:
                return '当前没有已送达且有效的待确认操作。'
            if action == 'confirm':
                return self.execute_confirmation(pending['token'])
            return self.store.discard_confirmation(pending['token'])
        if action in ('complete', 'cancel', 'date_complete', 'date_cancel'):
            date_mode = action.startswith('date_')
            kind = 'date' if date_mode else 'task'
            snapshot = dates if date_mode else tasks
            numbers = result.get('ids')
            if numbers is None and 'id' in result:
                numbers = [result['id']]
            limit = min(SNAPSHOT_LIMIT, len(snapshot))
            count = result.get('_dates_sent' if date_mode else '_tasks_sent', limit)
            reference = result.get('reference', 'position')
            if (not isinstance(numbers, list) or not 1 <= len(numbers) <= 10
                    or reference not in ('description', 'position')
                    or type(count) is not int or not 0 <= count <= limit
                    or any(type(n) is not int or not 1 <= n <= count for n in numbers)
                    or len(set(numbers)) != len(numbers)):
                return INVALID_ACTION
            selected = [snapshot[n - 1] for n in numbers]
            if reference == 'position':
                rows = self.list_rows(kind)
                if (not self.store.positions_match(kind, rows, numbers)
                        or any(rows[n - 1]['id'] != snapshot[n - 1]['id'] for n in numbers)):
                    pages = '、'.join(dict.fromkeys(page_command(kind, n) for n in sorted(numbers)))
                    return f'请先发送 {pages} 查看对应序号，再重新操作。'
            if not self.store.targets_match(selected):
                return f'相关事项已变化，请发送 {page_command(kind, 1)} 查看后重新操作。'
            status = 'completed' if action in ('complete', 'date_complete') else 'cancelled'
            return self.prepare_confirmation(status, selected)
        if action == 'chat' and isinstance(result.get('reply'), str) and result['reply'].strip():
            return result['reply'].strip()[:4000]
        return INVALID_ACTION

    async def handle_event(self, event):
        event_id = self.event_id(event)
        text = message_text(event)
        if not text or self.store.seen(event_id):
            return
        if len(text) > 2000:
            outcome = '消息太长，请在 2000 字以内描述待办。'
        elif text.startswith('/'):
            outcome = partial(self.run_command, event_id, text)
        elif self.settings.harness_url:
            outcome = await self.model_turn(event_id, text)
        else:
            outcome = '当前未启用大模型，请发送 /help 查看固定指令。'

        # Model I/O is complete; commit the validated action and its reply together.
        def commit():
            reply = outcome() if callable(outcome) else outcome
            if reply is not None:
                self.store.enqueue('reply', event_id, reply)

        self.store.handle_event(event_id, commit, reply_to=event['message_id'], reply_at=event.get('timestamp'))

    async def event_loop(self, queue):
        while True:
            event = await queue.get()
            try:
                for attempt in range(3):
                    try:
                        await self.handle_event(event)
                        break
                    except Exception as exc:
                        busy = (isinstance(exc, sqlite3.OperationalError)
                                and (getattr(exc, 'sqlite_errorcode', 0) & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))
                        if busy and attempt < 2:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        log.error('Incoming event failed (%s); notifying owner', type(exc).__name__)
                        # If recording the failure also fails, let the service
                        # restart and recover the original durable input.
                        self.store.fail_incoming(self.event_id(event), event)
                        break
            finally:
                queue.task_done()

    async def scheduler_loop(self):
        while True:
            try:
                self.store.schedule_due(int(time.time()))
            except Exception:
                log.exception('Failed to schedule reminders')
            await asyncio.sleep(5)

    async def wait_to_send(self):
        await asyncio.sleep(max(0, self.next_send - time.monotonic()))

    async def dispatch_loop(self):
        while True:
            # Select after the rate-limit wait: a reminder/list may have been
            # cancelled or superseded while the sender was cooling down.
            await self.wait_to_send()
            row = self.store.pending_outbox()
            if row is None:
                await asyncio.sleep(2)
                continue
            try:
                await self.bot.send(row['text'], delivery_id=f"{row['kind']}:{row['ref']}",
                                    reply_to=row['reply_to'], reply_at=row['reply_at'])
            except Exception as exc:
                detail = str(exc) if isinstance(exc, BotAPIError) else type(exc).__name__
                if retryable_delivery_error(exc):
                    delay = 0
                    if isinstance(exc, BotAPIError) and exc.rate_limited:
                        delay = 60
                        self.next_send = time.monotonic() + delay
                    log.warning('Message delivery failed (%s); will retry', detail)
                    self.store.defer_message(row, int(time.time()), delay)
                else:
                    log.error('Message delivery paused (%s); fix the cause and restart assistant to retry', detail)
                    self.store.pause_message(row, detail)
            else:
                self.store.mark_sent(row, int(time.time()))
            finally:
                self.next_send = max(self.next_send, time.monotonic() + SEND_INTERVAL)

    async def mail_loop(self):
        while True:
            for box in self.settings.mailboxes:
                try:
                    state = self.store.mailbox_state(box['id'])
                    validity, baseline, messages = await asyncio.to_thread(poll_mailbox, box, dict(state) if state else None)
                    if messages is None:
                        self.store.mailbox_baseline(box['id'], validity, baseline)
                        log.info('Mailbox %s initialized', box['id'])
                    else:
                        for uid, text in messages:
                            self.store.mailbox_message(box['id'], validity, uid, text)
                    self.store.mailbox_health(box['id'], box.get('name', box['id']), True)
                except Exception as exc:
                    if str(exc).startswith('UIDVALIDITY changed;'):
                        detail = 'UIDVALIDITY changed; manual reset required'
                    else:
                        detail = type(exc).__name__
                    log.warning('Mailbox %s failed: %s; check authorization, network and synchronization state', box['id'], detail)
                    self.store.mailbox_health(box['id'], box.get('name', box['id']), False)
            await asyncio.sleep(self.settings.poll_seconds)

    async def run(self):
        await asyncio.gather(self.recover_incoming(), self.bot.run(), self.event_loop(self.events), self.event_loop(self.commands),
                             self.scheduler_loop(), self.dispatch_loop(), self.mail_loop())


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    settings = Settings.load()
    store = Store(settings.db_path)
    asyncio.run(App(settings, store).run())


if __name__ == '__main__':
    main()
