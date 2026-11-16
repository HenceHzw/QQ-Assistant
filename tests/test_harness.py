import json
import io
import asyncio
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

try:
    import websockets  # noqa: F401
except ImportError:
    import types
    sys.modules['websockets'] = types.ModuleType('websockets')

from hence.app import App
from hence.config import Settings
from hence.harness import ask_harness, validated_reminder
from hence.reminders import ZONE
from hence.store import Store


class HarnessActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        settings = Settings(123, 'ws://localhost', 'onebot', ':memory:', (), '', '', '', 120,
                            'http://harness:8080/turn', 'shared')
        self.app = App(settings, self.store)
        async def run_mocked_call(func, *args, **kwargs):
            return func(*args, **kwargs)
        thread_patch = patch('hence.app.asyncio.to_thread', new=run_mocked_call)
        thread_patch.start()
        self.addCleanup(thread_patch.stop)

    def event(self, text, message_id):
        return {'message_type':'private', 'user_id':123, 'self_id':456, 'message_id':message_id,
                'message':[{'type':'text','data':{'text':text}}]}

    async def test_chat_and_create_are_saved_once(self):
        result = {'action':'chat','reply':'你好，我会记住你喜欢蓝色。'}
        with patch('hence.app.ask_harness', return_value=result) as ask:
            await self.app.handle_event(self.event('我喜欢蓝色', 1))
            self.assertIn('蓝色', self.store.pending_outbox()['text'])
            self.assertEqual(ask.call_args.args[2], '我喜欢蓝色')
        now = datetime.now(ZONE)
        due = datetime.fromtimestamp(now.timestamp() + 3600, ZONE).isoformat()
        with patch('hence.app.ask_harness', return_value={'action':'create','title':'开会','due':due}):
            event = self.event('一小时后提醒我开会', 2)
            await self.app.handle_event(event)
            await self.app.handle_event(event)
        self.assertEqual(len(self.store.active_reminders()), 1)
        self.assertEqual(self.store.active_reminders()[0]['title'], '开会')

    async def test_model_cannot_complete_missing_task(self):
        with patch('hence.app.ask_harness', return_value={'action':'complete','id':99}) as ask:
            await self.app.handle_event(self.event('我完成了那件事', 1))
            ask.assert_called_once()
        self.assertIn('找不到', self.store.pending_outbox()['text'])

    async def test_model_error_never_creates_a_reminder(self):
        with patch('hence.app.ask_harness', side_effect=TimeoutError()):
            await self.app.handle_event(self.event('明天提醒我交作业吗？', 1))
        self.assertEqual(self.store.active_reminders(), [])
        self.assertIn('/help', self.store.pending_outbox()['text'])

    async def test_command_bypasses_waiting_model(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_model(*args, **kwargs):
            started.set()
            await release.wait()
            return {'action': 'chat', 'reply': '完成'}
        with patch('hence.app.asyncio.to_thread', side_effect=slow_model):
            workers = [asyncio.create_task(self.app.event_loop(q)) for q in (self.app.events, self.app.commands)]
            try:
                await self.app.accept_event(self.event('你好', 1))
                await asyncio.wait_for(started.wait(), 1)
                await self.app.accept_event(self.event('/help', 2))
                await asyncio.wait_for(self.app.commands.join(), 1)
                self.assertIn('/add', self.store.pending_outbox()['text'])
                self.assertFalse(release.is_set())
                release.set()
                await asyncio.wait_for(self.app.events.join(), 1)
            finally:
                release.set()
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

    async def test_list_pagination_and_invalid_id(self):
        with self.store.db:
            for number in range(6):
                self.store.new_reminder(f'事项{number + 1}', 2000000000 + number)
        await self.app.handle_event(self.event('/list 2', 1))
        self.assertIn('事项6', self.store.pending_outbox()['text'])
        await self.app.handle_event(self.event('/complete 99999999999999999999999999999', 2))
        reply = self.store.db.execute('SELECT text FROM outbox ORDER BY id DESC').fetchone()['text']
        self.assertIn('格式不正确', reply)

    async def test_flexible_list_requests_go_to_harness(self):
        with patch('hence.app.ask_harness', return_value={'action': 'list'}) as ask:
            for number, text in enumerate(('待办', '代办', '待办列表', '查看待办', '我的待办', '帮助'), start=1):
                await self.app.handle_event(self.event(text, number))
            self.assertEqual([call.args[2] for call in ask.call_args_list],
                             ['待办', '代办', '待办列表', '查看待办', '我的待办', '帮助'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 6)
        self.assertIn('没有待办', self.store.pending_outbox()['text'])

    async def test_canonical_command_is_available_without_harness(self):
        with patch('hence.app.ask_harness', side_effect=AssertionError('should not invoke Harness')) as ask:
            await self.app.handle_event(self.event('/list', 1))
            self.assertIn('没有待办', self.store.pending_outbox()['text'])
            ask.assert_not_called()

    async def test_offline_commands_and_validation(self):
        future = (datetime.now(ZONE) + timedelta(days=2)).strftime('%Y-%m-%d %H:%M')
        past = (datetime.now(ZONE) - timedelta(days=1)).strftime('%Y-%m-%d %H:%M')
        with patch('hence.app.ask_harness', side_effect=AssertionError('should not invoke Harness')) as ask:
            await self.app.handle_event(self.event('/help', 1))
            help_text = self.store.pending_outbox()['text']
            for command in ('/help', '/add', '/list', '/complete', '/cancel'):
                self.assertIn(command, help_text)
            self.assertNotIn('/reset', help_text)
            await self.app.handle_event(self.event(f'/add {future} 交作业', 2))
            self.assertEqual(self.store.active_reminders()[0]['title'], '交作业')
            await self.app.handle_event(self.event(f'/add {past} 过期事项', 3))
            await self.app.handle_event(self.event('/add 明天 09:00 错误格式', 4))
            await self.app.handle_event(self.event('/unknown', 5))
            await self.app.handle_event(self.event('/reset', 7))
            self.assertEqual(len(self.store.active_reminders()), 1)
            await self.app.handle_event(self.event('/complete 1', 6))
            self.assertEqual(self.store.active_reminders(), [])
            ask.assert_not_called()

    async def test_unauthorized_user_is_ignored(self):
        with patch('hence.app.ask_harness') as ask:
            event = self.event('你好', 2)
            event['user_id'] = 999
            await self.app.accept_event(event)
            self.assertTrue(self.app.events.empty())
            ask.assert_not_called()

    def test_reject_invalid_and_past_times(self):
        now = datetime(2026,11,15,20,0,tzinfo=ZONE)
        for due in ('2026-11-15T09:00:00+08:00', '2027-01-01T09:00:00', '2027-01-01T09:00:00Z'):
            with self.subTest(due=due), self.assertRaises(ValueError):
                validated_reminder({'title':'事项','due':due}, now)


class HarnessPayloadTests(unittest.TestCase):
    def test_model_receives_current_commands_and_capabilities(self):
        with patch('hence.harness.urllib.request.urlopen', return_value=io.BytesIO(b'{"action":"chat","reply":"ok"}')) as urlopen:
            ask_harness('http://harness/turn', 'token', '有哪些指令', datetime.now(ZONE), [])
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertIn('/add', payload['commands'])
        self.assertIn('/help', payload['commands'])
        self.assertIn('不能直接访问邮箱', payload['capabilities'])
        self.assertEqual(payload['tasks_total'], 0)


class RealHarnessTests(unittest.TestCase):
    def test_sdk_restores_session_and_has_no_model_facing_tools(self):
        try:
            from deepseek_harness import DeepSeekHarness  # noqa: F401
        except ImportError:
            self.skipTest('DeepSeek Harness SDK is not installed')
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from harness_runtime.server import ChatEngine, create_server

        class Provider(BaseHTTPRequestHandler):
            requests = []
            def log_message(self, *args): pass
            def do_GET(self):
                data = json.dumps({'object':'list','data':[{'id':'deepseek-flash','object':'model','owned_by':'deepseek'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_POST(self):
                self.requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                content = json.dumps({'action':'chat','reply':'收到'}, ensure_ascii=False)
                events = [
                    {'type':'message_start','message':{'id':'test','type':'message','role':'assistant','model':'deepseek-flash','content':[], 'usage':{'input_tokens':10,'output_tokens':0}}},
                    {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
                    {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':content}},
                    {'type':'content_block_stop','index':0},
                    {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':10}},
                    {'type':'message_stop'}]
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.end_headers()
                for event in events:
                    self.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode())
                    self.wfile.flush()

        try:
            server = ThreadingHTTPServer(('127.0.0.1',0),Provider)
        except PermissionError:
            self.skipTest('localhost sockets are unavailable')
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as root:
                path = os.path.abspath('harness_runtime/profile.yml')
                args = {'home':root+'/home','workspace':root+'/work','patch':path,'model':'deepseek-flash',
                        'api_key':'fake-key','base_url':f'http://127.0.0.1:{server.server_port}'}
                def turn(text):
                    engine = ChatEngine(**args)
                    bridge = create_server(engine, 'bridge-secret', host='127.0.0.1', port=0)
                    threading.Thread(target=bridge.serve_forever, daemon=True).start()
                    try:
                        return ask_harness(f'http://127.0.0.1:{bridge.server_port}/turn', 'bridge-secret', text,
                                           datetime(2026,11,15,20,0,tzinfo=ZONE), [])
                    finally:
                        bridge.shutdown()
                        bridge.server_close()
                        engine.close()
                self.assertEqual(turn('我喜欢蓝色')['reply'], '收到')
                self.assertEqual(turn('我喜欢什么颜色')['reply'], '收到')
                self.assertIn('我喜欢蓝色',json.dumps(Provider.requests[-1],ensure_ascii=False))
                self.assertEqual(Provider.requests[-1].get('tools',[]),[])
                self.assertNotIn('dsh_session_log',Provider.requests[-1])
        finally:
            server.shutdown()
            server.server_close()
