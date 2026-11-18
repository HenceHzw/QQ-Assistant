"""DeepSeek Harness and official QQ plugin host; mailbox credentials stay in assistant."""
import hmac
import json
import logging
import os
import re
import secrets
import signal
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from deepseek_harness import DeepSeekHarness

LOG = logging.getLogger(__name__)
SYSTEM = '''你是用户的中文个人助手，根据会话理解用户偏好并自然回答。
输入是 JSON：text 为用户原话，now 为当前北京时间，commands 为固定指令，capabilities 为当前业务规则。以本次 capabilities 为准，包括与旧会话不同的设置。
tasks 和 dates 分别为当前待办与未完成日期提醒快照，tasks_total 和 dates_total 为各自总数。dates 包含已过日期；show_from 为显示开始日期，alerts 是计划提醒时间，不代表已经送达。pending_confirmation 为已送达的待确认操作，null 表示没有。
按 capabilities 选择操作，只输出一个 JSON 对象。可用格式：
聊天或追问：{"action":"chat","reply":"回答"}。
普通待办：{"action":"create","title":"事项","due":"YYYY-MM-DDTHH:MM:SS+08:00"}。
日期提醒：{"action":"create_date","title":"事项","date":"YYYY-MM-DD","show_days":2,"alerts":[{"days":2,"time":"09:00"},{"days":0,"time":"09:00"}]}；show_days 和 days 为目标日期之前的自然日数。
查看列表：{"action":"list"} 或 {"action":"list_dates"}。
完成或取消：{"action":"cancel","ids":[1],"reference":"description"}。action 可选 complete、cancel、date_complete、date_cancel；reference 可选 description、position。ids 来自本次对应快照。
确认或放弃：{"action":"confirm"} 或 {"action":"discard_confirmation"}。
操作由助手程序校验执行，不能在聊天回复中声称已经执行。
'''


class InvalidModelReply(ValueError):
    pass


class ModelFailure(RuntimeError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch(r'[A-Z_]{1,50}', code) else 'UNKNOWN'
        super().__init__(self.code)


class ChatEngine:
    def __init__(self, *, home, workspace, patch, model, api_key, base_url=None, harness_class=DeepSeekHarness, extra_patches=()):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        self.marker = self.home / 'active-session.json'
        self.lock = threading.Lock()
        kwargs = dict(provider='deepseek-official', model=model, max_tokens=4096,
                      cwd=workspace, dsh_home=home, profile='sdk-minimal', patches=(patch, *extra_patches), api_key=api_key,
                      request_timeout_seconds=120, env={'DSH_SYSTEM_PROMPT': SYSTEM, 'NODE_TLS_REJECT_UNAUTHORIZED': '1'})
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
            if result.finish_reason not in (None, 'completed'):
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
            if not isinstance(result.final_response, str):
                raise InvalidModelReply()
            raw = result.final_response.strip()
            if raw.startswith('```'):
                raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
            try:
                obj = json.loads(raw)
            except ValueError as exc:
                raise InvalidModelReply() from exc
            if not isinstance(obj, dict):
                raise InvalidModelReply()
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
            except InvalidModelReply:
                LOG.warning('Harness returned an invalid model reply')
                data = b'{"error":"invalid_model_reply"}'
                self.send_response(422)
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
    plugin_root = Path('/app/qq_plugin')
    plugins = [{'id': 'qq-assistant-bridge', 'name': str(plugin_root / 'bridge.mjs')}]
    if os.environ.get('QQBOT_APPID') and os.environ.get('QQBOT_SECRET'):
        plugins.append({'id': 'im-qqbot', 'name': str(plugin_root / 'node_modules/@tencent-connect/dsh-qqbot/lib/index.js'),
                        'config': {'appId': '__FROM_ENV__', 'appSecret': '__FROM_ENV__', 'streaming': False,
                                   'media': {'enabled': False}, 'vision': {'enabled': False},
                                   'access': {'c2cMode': 'disabled', 'groupMode': 'disabled'}}})
    plugin_patch = Path('/data/qq-plugin.yml')
    plugin_patch.write_text(json.dumps([{'insert': plugins}]))
    engine = ChatEngine(home='/data/dsh', workspace='/data/workspace', patch='/app/profile.yml',
                        model=os.environ.get('DSH_MODEL', 'deepseek-flash'), api_key=api_key,
                        base_url=os.environ.get('DEEPSEEK_BASE_URL') or None, extra_patches=(str(plugin_patch),))
    server = None
    try:
        # Start QQ before the first model request, so fixed commands and
        # scheduled notifications work independently of the model API.
        engine.harness.start()
        request = urllib.request.Request('http://127.0.0.1:8081/status', headers={'Authorization': 'Bearer ' + token})
        with urllib.request.urlopen(request, timeout=10) as response:
            status = json.load(response)
        if len(plugins) == 2 and not status.get('configured'):
            raise RuntimeError('Official QQ plugin did not activate')
        LOG.info('Official QQ plugin bridge ready; configured=%s', status['configured'])
        server = create_server(engine, token)
        def stop(_signum, _frame):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever()
    finally:
        if server is not None:
            server.server_close()
        engine.close()



if __name__ == '__main__':
    main()
