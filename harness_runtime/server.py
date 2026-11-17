"""Isolated DeepSeek Harness chat bridge. This process never accesses mail or QQ credentials."""
import hmac
import json
import logging
import os
import re
import secrets
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from deepseek_harness import DeepSeekHarness

LOG = logging.getLogger(__name__)
SYSTEM = '''你是用户的中文个人助手。根据会话持续理解用户的偏好，并自然地回答问题。
每次输入是 JSON：text 为用户原话，now 为当前北京时间，tasks 为当前列表中的待办，dates 为未完成且未取消的日期提醒（包括已过日期），分别最多提供30项，对应 tasks_total 和 dates_total。dates 中 show_from 为显示开始日期，alerts 为创建时保留的提醒时间，不代表已经成功送达。commands 和 capabilities 描述实际支持的指令与权限。快照不完整时不能假称知道全部项目；回答自身功能时以这些字段为准。
只输出一个 JSON 对象，不使用 Markdown 或代码围栏。不得假称操作已完成，由调用方真正执行。
回答或提问：{"action":"chat","reply":"你的回答"}。
普通待办使用 {"action":"create","title":"事项","due":"YYYY-MM-DDTHH:MM:SS+08:00"}。
日期提醒使用 {"action":"create_date","title":"事项","date":"YYYY-MM-DD","show_days":2,"alerts":[{"days":2,"time":"09:00"},{"days":0,"time":"09:00"}]}。show_days 是提前多少自然日开始显示，alerts 是日期前多少天及当天的北京时间。
根据事项含义选择类型，无需用户说出“日期提醒”等术语：“新增一个提醒”不等于要求普通待办。生日、纪念日、体测、考试日期、报名或缴费截止日期等以某个日期为中心的事项，默认使用 create_date；拿快递、交作业、打电话等具体行动或“半小时后提醒”这类临时事项，默认使用 create。结合整句话判断，不能只凭关键词、有没有具体时刻或距离远近分类；例如“明天提醒我买生日礼物”是行动待办，不能因包含生日就当成日期提醒。用户明确选择普通待办或日期提醒时遵从其选择；要求提前几天通知或临近才显示时使用 create_date。
日期提醒未指定设置时，提前两天开始显示，提前两天和当天09:00各提醒一次。用户指定提醒时间、提前天数、显示范围或只在某时提醒一次时如实设置，不额外添加未要求的通知；例如“生日只在当天18点提醒，不要提前”使用 create_date，alerts 仅为 [{"days":0,"time":"18:00"}]。用户的明确要求优先于默认值。日期与意图清楚时直接创建，不要求用户先选择分类；确实无法判断才用 chat 追问。日期提醒对应一次具体日期，不会每年自动重复；用户明确要求循环提醒时说明当前限制，不假称已创建循环。
用户要求查看全部日期提醒：{"action":"list_dates"}。对日期列表中的一个或多个项目操作：{"action":"date_complete"或"date_cancel","ids":[数字],"reference":"description"或"position"}。日期列表序号与普通待办列表序号不同，不得混用；隐藏事项只能使用 dates 的序号，目标不明时追问。完成或取消每次最多10项，由程序列出目标并等待用户确认，不能声称已执行。
pending_confirmation 是助手已经发给用户、等待确认的操作，包含 action 和 targets；null 表示没有待确认操作。只有当用户明确同意执行该操作时返回 {"action":"confirm"}；用户明确表示放弃时返回 {"action":"discard_confirmation"}。疑问、犹豫、不同目标或无法判断时用 chat 交流，不执行操作。确认与放弃由程序校验并执行。
有日期但没说时间，默认北京时间09:00；无年份的月日选择未来最近一次。信息不清楚、没有日期或时间已过，就用 chat 追问，不要编造。
用户要求查看待办时返回 {"action":"list"}，由程序生成列表，不要自行用 chat 编造或显示列表。完成或取消一个或多个任务：{"action":"complete"或"cancel","ids":[数字,数字],"reference":"description"或"position"}；只处理一个也使用数组。
reference 描述用户如何指定目标：明确说事项名称、内容或日期（如“取消体测”“完成明天的作业”）用 description，直接从本次快照选择匹配目标，无需先让用户查看列表；依赖编号、位置或上一份列表（如“取消第一个”“取消这两项”）用 position，由程序检查列表是否仍对应。无论哪种方式，ids 都必须来自本次对应快照。无法唯一确定目标或有多个同名事项时用 chat 追问，不要猜测。
序号是当前列表位置，会变化，以本次 tasks 为准，不要沿用过期的历史序号。notified 表示已经提醒但未完成。用户要求处理多个任务时必须列出全部目标，不能只处理其中一项；目标不明确、超过10项或快照不完整且无法确定全部目标时，用 chat 追问。
用户说的是疑问或讨论时自然回答，不能凭空创建待办；涉及任务的编号如果不明确，要追问。
不读取邮件。任务快照是辅助数据，无法在此处直接执行操作。
'''


class ModelFailure(RuntimeError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch(r'[A-Z_]{1,50}', code) else 'UNKNOWN'
        super().__init__(self.code)


class ChatEngine:
    def __init__(self, *, home, workspace, patch, model, api_key, base_url=None, harness_class=DeepSeekHarness):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        self.marker = self.home / 'active-session.json'
        self.lock = threading.Lock()
        kwargs = dict(provider='deepseek-official', model=model, max_tokens=4096,
                      cwd=workspace, dsh_home=home, profile='sdk-minimal', patches=(patch,), api_key=api_key,
                      request_timeout_seconds=120, env={'DSH_SYSTEM_PROMPT': SYSTEM})
        if base_url:
            kwargs['base_url'] = base_url
        self.harness = harness_class(**kwargs)
        self.session = None

    def _open(self):
        if self.session is not None:
            return
        if self.marker.exists():
            session_id = json.loads(self.marker.read_text(encoding='utf-8'))['id']
            # Never replace an unreadable or incompatible persisted session automatically.
            self.session = self.harness.resume_session(session_id)
        else:
            self.session = self.harness.start_session('qq-' + secrets.token_hex(16))

    def run(self, payload):
        with self.lock:
            self._open()
            result = self.session.run(json.dumps(payload, ensure_ascii=False))
            if result.finish_reason not in (None, 'completed') or not result.final_response.strip():
                code = 'UNKNOWN'
                for event in reversed(result.events):
                    if event.get('type') == 'turn/end':
                        reason = event.get('data', {}).get('reason', {})
                        code = reason.get('error', {}).get('code', 'UNKNOWN')
                        break
                raise ModelFailure(code)
            if not self.marker.exists():
                temp = self.marker.with_suffix('.tmp')
                temp.write_text(json.dumps({'id': self.session.id}), encoding='utf-8')
                os.chmod(temp, 0o600)
                os.replace(temp, self.marker)
            raw = result.final_response.strip()
            if raw.startswith('```'):
                raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
            obj = json.loads(raw)
            if not isinstance(obj, dict) or obj.get('action') not in ('chat', 'create', 'list', 'complete', 'cancel', 'create_date', 'list_dates', 'date_complete', 'date_cancel', 'confirm', 'discard_confirmation'):
                raise ValueError('Invalid Harness response')
            return obj

    def close(self):
        self.harness.close()


def create_server(engine, token, host='0.0.0.0', port=8080):
    if not token:
        raise ValueError('HARNESS_AUTH_TOKEN is required')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path != '/turn' or not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 1 or size > 65536:
                    raise ValueError('Invalid request size')
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict) or not isinstance(payload.get('text'), str) or len(payload['text']) > 4000:
                    raise ValueError('Invalid request')
                # A stuck native turn would otherwise block the sole persistent session forever.
                # Container restart restores the last durable session on the next request.
                def abort_stuck_turn():
                    LOG.error('Harness turn exceeded 150 seconds; restarting service')
                    os._exit(1)
                watchdog = threading.Timer(150, abort_stuck_turn)
                watchdog.daemon = True
                watchdog.start()
                try:
                    result = engine.run(payload)
                finally:
                    watchdog.cancel()
                data = json.dumps(result, ensure_ascii=False).encode()
                self.send_response(200)
            except Exception as exc:
                detail = f'model error {exc.code}' if isinstance(exc, ModelFailure) else type(exc).__name__
                LOG.warning('Harness turn failed: %s', detail)
                data = b'{"error":"harness_unavailable"}'
                self.send_response(503)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                LOG.warning('Client disconnected before Harness result')

    return ThreadingHTTPServer((host, port), Handler)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    api_key = os.environ['DEEPSEEK_API_KEY']
    token = os.environ['HARNESS_AUTH_TOKEN']
    if not api_key or not token:
        raise ValueError('DEEPSEEK_API_KEY and HARNESS_AUTH_TOKEN are required')
    engine = ChatEngine(home='/data/dsh', workspace='/data/workspace', patch='/app/profile.yml',
                        model=os.environ.get('DSH_MODEL', 'deepseek-flash'), api_key=api_key,
                        base_url=os.environ.get('DEEPSEEK_BASE_URL') or None)
    server = create_server(engine, token)
    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        engine.close()


if __name__ == '__main__':
    main()
