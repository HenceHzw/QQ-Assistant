import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createBridge } from '../harness_runtime/qq_plugin/bridge.mjs';

async function setup(t, directory, owner='test-owner') {
  const ownsDirectory = !directory;
  directory ||= mkdtempSync(join(tmpdir(), 'qq-plugin-test-'));
  const bridge = createBridge({ token: 'shared', appId: '123456', owner, directory });
  const handlers = {}, sent = [];
  let middleware, refreshes = 0;
  const bot = { on(event, handler) { handlers[event] = handler; }, use(handler) { middleware = handler; },
    async send(options) { sent.push(options); return { id: 'sent-' + sent.length }; },
    tokenManager: { clearCache() { refreshes++; } } };
  bridge.attach(bot);
  await new Promise(resolve => bridge.server.listen(0, '127.0.0.1', resolve));
  async function close() {
    if (bridge.server.listening) await new Promise(resolve => bridge.server.close(resolve));
  }
  t.after(async () => {
    await close();
    if (ownsDirectory) rmSync(directory, {recursive:true,force:true});
  });
  async function request(path, value, token='shared') {
    if (path === '/send') value = { app_id: '123456', owner_openid: owner, ...value };
    const res = await fetch(`http://127.0.0.1:${bridge.server.address().port}${path}`, {
      method: value === undefined ? 'GET' : 'POST', headers: { Authorization: 'Bearer ' + token },
      body: value === undefined ? undefined : JSON.stringify(value) });
    return { status: res.status, data: await res.json() };
  }
  async function receive(content='/help', id='one', senderId='test-owner', kind='c2c') {
    await middleware({ message: { kind, senderId, messageId:id, content, timestamp:'2026-11-17T12:00:00Z' } });
  }
  return { bot, request, receive, sent, handlers, directory, close, refreshes: () => refreshes };
}

test('only the bound owner reaches the durable inbox, and reception order is preserved', async t => {
  const s=await setup(t);
  await s.receive('private stranger text','stranger','stranger');
  await s.receive('group text','group','test-owner','group');
  await s.receive('/add 2027-12-01 09:00 作业','z');
  await s.receive('确认','a');
  await s.receive('duplicate','z');
  const result=await s.request('/events');
  assert.deepEqual(result.data.events.map(x=>x.event.message_id),['z','a']);
  assert.equal((await s.request('/events',undefined,'wrong')).status,403);
  assert.equal((await s.request('/ack',{key:'../../private'})).status,400);
  await s.request('/ack',{key:result.data.events[0].key});
  assert.equal((await s.request('/events')).data.events.length,1);
});

test('restart restores pending input and send receipts for the bound owner', async t => {
  const first = await setup(t);
  await first.receive('保存这条消息');
  const mail = {text: '邮件通知', delivery_id: 'mail:1'};
  assert.equal((await first.request('/send', mail)).status, 200);
  assert.equal((await first.request('/send', mail)).status, 200);
  assert.equal(first.sent.length, 1);
  assert.equal(first.sent[0].target.msgId, undefined);
  await first.close();

  const restarted = await setup(t, first.directory);
  const {data} = await restarted.request('/events');
  assert.equal(data.events[0].event.message, '保存这条消息');
  assert.equal((await restarted.request('/send', mail)).status, 200);
  assert.equal(restarted.sent.length, 0);
  await restarted.close();

  const other = await setup(t, first.directory, 'new-owner');
  assert.deepEqual((await other.request('/events')).data.events, []);
});

test('oversized input stays an explicit error in Python rather than a truncated command', async t=>{
  const s=await setup(t);
  await s.receive('😀'.repeat(3000));
  const text=(await s.request('/events')).data.events[0].event.message;
  assert.equal(Array.from(text).length,2001);
});

test('date retries use the batch ID and reuse receipts from older versions', async t => {
  const s = await setup(t);
  const payload = {text:'日期提醒：体测', delivery_id:'date_reminder:1:100'};
  const first = await s.request('/send', payload);
  const second = await s.request('/send', {...payload, text:'迟到的' + payload.text});
  assert.deepEqual(second.data, first.data);
  assert.equal(s.sent.length, 1);

  const hash = text => createHash('sha256').update(text).digest('hex');
  const oldId = 'date_reminder:2:200';
  const oldPath = join(s.directory, hash('123456:test-owner'), 'receipts', hash(oldId + '\n' + payload.text) + '.json');
  writeFileSync(oldPath, JSON.stringify({id:'previous-receipt'}));
  const retry = await s.request('/send', {delivery_id:oldId, text:'迟到的' + payload.text});
  assert.equal(retry.data.id, 'previous-receipt');
  assert.equal(s.sent.length, 1);
});

test('a different app or owner cannot send or reuse an old receipt', async t => {
  const s = await setup(t);
  const payload = { text: '通知', delivery_id: 'mail:1' };
  assert.equal((await s.request('/send', payload)).status, 200);
  for (const identity of [{app_id:'other-app'}, {owner_openid:'other-owner'}, {app_id:undefined}, {owner_openid:undefined}]) {
    const result = await s.request('/send', {...payload, ...identity});
    assert.equal(result.data.error.status, 409);
  }
  assert.equal(s.sent.length, 1);
});

test('only recent replies carry a QQ message id and retries use a stable sequence', async t=>{
  const s=await setup(t);
  await s.request('/send',{text:'回复',delivery_id:'reply:1',reply_to:'incoming',reply_at:Date.now()/1000});
  await s.request('/send',{text:'迟到回复',delivery_id:'reply:2',reply_to:'old',reply_at:Date.now()/1000-600});
  assert.equal(s.sent[0].target.msgId,'incoming');
  assert.equal(s.sent[0].extra.msg_seq,1);
  assert.equal(s.sent[1].target.msgId,undefined);
});

test('concurrent HTTP retries share one upstream send',async t=>{
  const s=await setup(t); let count=0;
  s.bot.send=async()=>{ count++; await new Promise(r=>setTimeout(r,30)); return {id:'sent'}; };
  const payload={text:'提醒',delivery_id:'reminder:1'};
  const results=await Promise.all([s.request('/send',payload),s.request('/send',payload)]);
  assert.equal(count,1);
  assert.ok(results.every(x=>x.status===200));
});

test('401 refreshes through the official SDK once; errors omit private response bodies',async t=>{
  const s=await setup(t);let count=0;
  s.bot.send=async()=>{ count++; if(count===1)throw {httpStatus:401,bizMessage:'secret'}; return{id:'sent'}; };
  assert.equal((await s.request('/send',{text:'回复',delivery_id:'reply:1'})).status,200);
  assert.equal(s.refreshes(),1);
  s.bot.send=async()=>{throw {httpStatus:403,bizCode:40034024,bizMessage:'private mail'};};
  const error=await s.request('/send',{text:'邮件',delivery_id:'mail:1'});
  assert.deepEqual(error.data,{error:{status:403,code:40034024}});
  assert.equal(error.status,502);
});

test('failed or malformed deliveries never become successful receipts',async t=>{
  const s=await setup(t);let count=0;
  s.bot.send=async()=>{count++;return {code:40034024,message:'private data'};};
  const payload={text:'回复',delivery_id:'reply:1'};
  assert.equal((await s.request('/send',payload)).status,502);
  assert.equal((await s.request('/send',payload)).status,502);
  assert.equal(count,2);
});

test('binding requires a fresh secret; unmatched users never reach the inbox',async t=>{
  const s=await setup(t,undefined,'');
  const first=await s.request('/bind',{}), second=await s.request('/bind',{});
  assert.notEqual(first.data.code,second.data.code);
  await s.receive('/bind '+first.data.code);
  assert.equal((await s.request('/bind')).data.owner,null);
  await s.receive('/bind '+second.data.code);
  assert.equal((await s.request('/bind')).data.owner,'test-owner');
  assert.deepEqual((await s.request('/events')).data.events,[]);
});

test('platform blocks stop delivery and leave notifications pending',async t=>{
  const s=await setup(t);
  s.handlers.disconnected({willReconnect:false});
  assert.equal((await s.request('/status')).data.blocked,true);
  const res=await s.request('/send',{text:'提醒',delivery_id:'reminder:1'});
  assert.equal(res.data.error.status,403);
  assert.equal(s.sent.length,0);
});
