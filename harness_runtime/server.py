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
每次输入是 JSON：text 为用户原话，now 为当前北京时间，tasks 为当前未完成的待办快照，tasks_total 为总数，commands 和 capabilities 描述当前实际支持的指令与权限。快照不完整时不能假称知道全部待办；回答自身功能时以这些字段为准，不要编造或否认其中列出的功能。
只输出一个 JSON 对象，不使用 Markdown 或代码围栏。不得假称操作已完成，由调用方真正执行。
回答或提问：{"action":"chat","reply":"你的回答"}。
用户要创建单次提醒：{"action":"create","title":"事项","due":"YYYY-MM-DDTHH:MM:SS+08:00"}。
有日期但没说时间，默认北京时间09:00；无年份的月日选择未来最近一次。信息不清楚、没有日期或时间已过，就用 chat 追问，不要编造。
查看待办：{"action":"list"}。完成或取消已存在的任务：{"action":"complete"或"cancel","id":数字}。
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
            if not isinstance(obj, dict) or obj.get('action') not in ('chat', 'create', 'list', 'complete', 'cancel'):
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
