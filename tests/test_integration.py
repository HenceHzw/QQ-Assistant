"""Integration tests for the Harness runtime and the official QQ plugin using local network stubs."""
import asyncio
import json
import queue
import socket
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hence.app import App
from hence.config import Settings
from hence.harness import ask_harness
from hence.qqbot import BotAPIError, QQBot, request_json
from hence.reminders import ZONE
from hence.store import Store


@contextmanager
def running_http(factory, *args, **kwargs):
    try:
        server = factory(*args, **kwargs)
    except PermissionError as exc:
        raise unittest.SkipTest('localhost sockets are unavailable') from exc
    with server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


class ModelProvider(BaseHTTPRequestHandler):
    # Each test subclass supplies its own requests list.
    def log_message(self, *args):
        pass

    def respond(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.respond({'data': [{'id': 'deepseek-flash', 'object': 'model', 'owned_by': 'deepseek'}]})

    def do_POST(self):
        self.respond_model(json.loads(self.rfile.read(int(self.headers['Content-Length']))))

    def respond_model(self, payload):
        self.requests.append(payload)
        events = [
            {'type': 'message_start', 'message': {'id': 'test', 'type': 'message', 'role': 'assistant', 'model': 'deepseek-flash', 'content': [], 'usage': {'input_tokens': 10, 'output_tokens': 0}}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': json.dumps({'action': 'chat', 'reply': '收到'})}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 10}},
            {'type': 'message_stop'},
        ]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for event in events:
            self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode())
            self.wfile.flush()


class RealHarnessTests(unittest.TestCase):
    def test_model_format_errors_and_service_failures_give_different_qq_replies(self):
        try:
            from harness_runtime.server import ChatEngine, create_server
        except ImportError:
            self.skipTest('DeepSeek Harness SDK is not installed')

        with tempfile.TemporaryDirectory() as directory:
            factory = Mock()
            session = factory.return_value.start_session.return_value
            session.id = 'test'
            session.run.side_effect = [
                SimpleNamespace(finish_reason='completed', final_response=reply)
                for reply in ('{"action":"unknown"}', 'invalid JSON', '')
            ] + [TimeoutError()]
            engine = ChatEngine(home=directory, workspace=directory, patch='', model='test',
                                api_key='test', harness_class=factory)
            self.addCleanup(engine.close)
            server = self.enterContext(running_http(create_server, engine, 'shared', host='127.0.0.1', port=0))
            store = Store(':memory:')
            self.addCleanup(store.db.close)
            app = App(Settings('test-owner', '123456', ':memory:', (), 120,
                               f'http://127.0.0.1:{server.server_port}/turn', 'shared'), store)
            for number, expected in enumerate(['重新描述'] * 3 + ['无法连接']):
                asyncio.run(app.handle_event({'message_id': str(number), 'message': '查看待办'}))
                reply = store.pending_outbox()
                self.assertIn(expected, reply['text'])
                store.mark_sent(reply, 100)
            self.assertEqual(store.active_reminders(), [])

    def test_sdk_restores_session_and_has_no_model_facing_tools(self):
        try:
            from deepseek_harness import DeepSeekHarness  # noqa: F401
        except ImportError:
            self.skipTest('DeepSeek Harness SDK is not installed')
        from harness_runtime.server import ChatEngine, create_server

        class Provider(ModelProvider):
            requests = []

        server = self.enterContext(running_http(ThreadingHTTPServer, ('127.0.0.1', 0), Provider))
        with tempfile.TemporaryDirectory() as root:
            path = str(Path('harness_runtime/profile.yml').resolve())
            args = {'home':root+'/home','workspace':root+'/work','patch':path,'model':'deepseek-flash',
                    'api_key':'fake-key','base_url':f'http://127.0.0.1:{server.server_port}'}
            def turn(text):
                with closing(ChatEngine(**args)) as engine:
                    with running_http(create_server, engine, 'bridge-secret', host='127.0.0.1', port=0) as bridge:
                        return ask_harness(f'http://127.0.0.1:{bridge.server_port}/turn', 'bridge-secret', text,
                                           datetime(2026,11,15,20,0,tzinfo=ZONE), [])

            self.assertEqual(turn('我喜欢蓝色')['reply'], '收到')
            with patch('hence.harness.CAPABILITIES', '本轮新规则：只能聊天。'):
                self.assertEqual(turn('我喜欢什么颜色')['reply'], '收到')
            self.assertIn('我喜欢蓝色',json.dumps(Provider.requests[-1],ensure_ascii=False))
            self.assertIn('本轮新规则：只能聊天。', json.dumps(Provider.requests[-1], ensure_ascii=False))
            self.assertEqual(Provider.requests[-1].get('tools',[]),[])
            self.assertNotIn('dsh_session_log',Provider.requests[-1])


class OfficialPluginIntegrationTests(unittest.TestCase):
    def test_official_gateway_mail_delivery_and_restart_with_existing_harness(self):
        try:
            from deepseek_harness import DeepSeekHarness
            from websockets.sync.server import serve
            from websockets.exceptions import ConnectionClosed
        except ImportError:
            self.skipTest('install requirements-dev.txt for real plugin integration')
        plugin = Path('harness_runtime/qq_plugin').resolve()
        entry = plugin / 'node_modules/@tencent-connect/dsh-qqbot/lib/index.js'
        if not entry.is_file():
            self.skipTest('run npm ci and python patch.py in harness_runtime/qq_plugin')
        incoming = queue.Queue()
        delivered, model_requests = [], []
        def gateway(ws):
            ws.send(json.dumps({'op': 10, 'd': {'heartbeat_interval': 1000}}))
            identify = json.loads(ws.recv())
            self.assertEqual(identify['op'], 2)
            ws.send(json.dumps({'op': 0, 't': 'READY', 's': 1, 'd': {'session_id': 'test-session', 'user': {'id': 'bot'}}}))
            sequence = 1
            try:
                while True:
                    try:
                        event = incoming.get_nowait()
                        sequence += 1
                        ws.send(json.dumps({'op': 0, 't': 'C2C_MESSAGE_CREATE', 's': sequence, 'd': event}))
                    except queue.Empty:
                        pass
                    try:
                        packet = json.loads(ws.recv(timeout=.05))
                        if packet.get('op') == 1: ws.send(json.dumps({'op': 11}))
                    except TimeoutError:
                        pass
            except ConnectionClosed:
                pass
        try:
            ws_server = serve(gateway, '127.0.0.1', 0)
        except PermissionError:
            self.skipTest('localhost sockets are unavailable')
        threading.Thread(target=ws_server.serve_forever, daemon=True).start()
        self.addCleanup(ws_server.shutdown)
        class Provider(ModelProvider):
            requests = model_requests
            def do_GET(self):
                self.respond({'url': f'ws://127.0.0.1:{ws_server.socket.getsockname()[1]}',
                              'data': [{'id': 'deepseek-flash', 'object': 'model', 'owned_by': 'deepseek'}]})
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path.endswith('/getAppAccessToken'):
                    return self.respond({'access_token': 'mock-access', 'expires_in': 7200})
                if self.path.startswith('/v2/users/'):
                    delivered.append(payload)
                    return self.respond({'id': 'sent-' + str(len(delivered))})
                self.respond_model(payload)
        api = self.enterContext(running_http(ThreadingHTTPServer, ('127.0.0.1', 0), Provider))
        base = f'http://127.0.0.1:{api.server_port}'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            bridge_port = sock.getsockname()[1]
        bridge_url = f'http://127.0.0.1:{bridge_port}'
        def wait_for(callback):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                result = callback()
                if result: return result
                time.sleep(.05)
            self.fail('official plugin did not produce the expected result')
        def call(path, data=None):
            return request_json('GET' if data is None else 'POST', bridge_url + path, 'shared', data)
        runtime = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                plugin_config = Path(directory) / 'plugin.yml'
                plugin_config.write_text(json.dumps([{'insert': [
                    {'id':'qq-assistant-bridge','name':str(plugin / 'bridge.mjs')},
                    {'id':'im-qqbot','name':str(entry),'config':{'appId':'__FROM_ENV__','appSecret':'__FROM_ENV__',
                      'streaming':False,'media':{'enabled':False},'vision':{'enabled':False},
                      'access':{'c2cMode':'disabled','groupMode':'disabled'}}}]}]))
                def start():
                    h = DeepSeekHarness(dsh_home=directory+'/dsh', cwd=directory,
                        profile='sdk-minimal', patches=(str(Path('harness_runtime/profile.yml').resolve()),str(plugin_config)),
                        provider='deepseek-official', model='deepseek-flash', api_key='fake-key', base_url=base,
                        env={'QQBOT_BASE_URL':base,'QQBOT_TOKEN_BASE_URL':base,
                             'QQBOT_APPID':'123456','QQBOT_SECRET':'fake-secret','QQ_BOT_OWNER_OPENID':'test-owner',
                             'HARNESS_AUTH_TOKEN':'shared','QQ_BRIDGE_HOST':'127.0.0.1',
                             'QQ_BRIDGE_PORT':str(bridge_port),'QQ_BRIDGE_DATA':directory+'/bridge',
                             'NODE_TLS_REJECT_UNAUTHORIZED':'1'})
                    try:
                        h.start()
                        self.assertTrue(call('/status')['configured'])
                        wait_for(lambda: call('/status')['connected'])
                    except Exception:
                        print(h.client._runtime_diagnostics())
                        h.close()
                        raise
                    return h
                runtime = start()
                def receive(text, mid, owner='test-owner'):
                    incoming.put({'id':mid,'author':{'user_openid':owner},'content':text,
                                  'timestamp':datetime.now(timezone.utc).isoformat()})
                receive('PRIVATE_STRANGER_TEXT', 'stranger', 'other-owner')
                receive('/help', 'help')
                events = wait_for(lambda: call('/events')['events'])
                self.assertEqual([x['event']['message_id'] for x in events], ['help'])
                # A stopped Python worker does not lose QQ input on host restart.
                runtime.close(); runtime = start()
                self.assertEqual(call('/events')['events'][0]['event']['message'], '/help')
                store = Store(':memory:')
                self.addCleanup(store.db.close)
                settings = Settings('test-owner','123456',':memory:',(),120,
                                    harness_token='shared',qq_bridge_url=bridge_url)
                app = App(settings, store)
                asyncio.run(app.accept_event(events[0]['event']))
                call('/ack', {'key': events[0]['key']})
                asyncio.run(app.handle_event(app.commands.get_nowait()))
                row = store.pending_outbox()
                self.assertIn('/help', row['text'])
                asyncio.run(app.bot.send(row['text'],delivery_id='reply:help',reply_to=row['reply_to'],reply_at=row['reply_at']))
                self.assertEqual(len(model_requests),0)  # Fixed commands bypass the model.
                store.mark_sent(row, int(time.time()))
                self.assertEqual(delivered[-1]['msg_id'],'help')
                # Native Harness still provides persistent conversation independently.
                result = runtime.run('Remember that I prefer blue.',session_id='plugin-model-test')
                self.assertEqual(json.loads(result.final_response)['reply'],'收到')
                self.assertFalse(model_requests[-1].get('tools'))
                calls = len(model_requests)
                def send_mail():
                    asyncio.run(app.bot.send('PRIVATE_MAIL_SUBJECT', delivery_id='mail:1'))
                stale = QQBot('old-app', 'old-owner', None, 'shared', bridge_url)
                with self.assertRaises(BotAPIError) as rejected:
                    asyncio.run(stale.send('STALE_PRIVATE_MAIL', delivery_id='mail:stale'))
                self.assertEqual(rejected.exception.status, 409)
                self.assertNotIn('STALE_PRIVATE_MAIL', json.dumps(delivered))
                send_mail()
                send_mail()
                self.assertEqual(sum(x.get('content')=='PRIVATE_MAIL_SUBJECT' for x in delivered),1)
                self.assertNotIn('msg_id',delivered[-1])
                self.assertEqual(len(model_requests),calls)
                self.assertNotIn('PRIVATE_MAIL_SUBJECT',json.dumps(model_requests))
                self.assertNotIn('PRIVATE_STRANGER_TEXT',runtime.client._runtime_diagnostics())
                runtime.close(); runtime = start()
                send_mail()
                self.assertEqual(sum(x.get('content')=='PRIVATE_MAIL_SUBJECT' for x in delivered),1)
                self.assertEqual(call('/events')['events'],[])
        finally:
            if runtime: runtime.close()
