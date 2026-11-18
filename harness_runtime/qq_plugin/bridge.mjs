/** Business adapter for the pinned Tencent DSH plugin. QQ protocol stays upstream. */
import http from 'node:http';
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync, renameSync, readdirSync, unlinkSync, existsSync } from 'node:fs';
import { join } from 'node:path';

export const name = 'qq-assistant-bridge';
const hash = value => createHash('sha256').update(value).digest('hex');
function save(path, value) {
  writeFileSync(path + '.tmp', JSON.stringify(value), { mode: 0o600 });
  renameSync(path + '.tmp', path);
}
function equal(a, b) {
  const x = Buffer.from(a || ''), y = Buffer.from(b || '');
  return x.length === y.length && timingSafeEqual(x, y);
}
function response(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) });
  res.end(body);
}
async function body(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 65536) throw new Error('request_too_large');
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString('utf8');
  const value = JSON.parse(text || '{}');
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid_request');
  return value;
}

export function createBridge({ token, appId, owner, directory }) {
  if (!token) throw new Error('HARNESS_AUTH_TOKEN is required');
  // Separate state when the app or owner changes; old inputs never reach a new owner.
  const state = join(directory, hash(appId + ':' + owner));
  const inbox = join(state, 'inbox'), receipts = join(state, 'receipts');
  for (const path of [inbox, receipts]) mkdirSync(path, { recursive: true, mode: 0o700 });
  let sequence = readdirSync(inbox).filter(n => n.endsWith('.json'))
    .reduce((max, n) => Math.max(max, JSON.parse(readFileSync(join(inbox, n), 'utf8')).order), 0);
  let bot, connected = false, blocked = false, pairing;
  const sending = new Map();
  function attach(instance) {
    if (bot) throw new Error('Only one QQ connection is allowed');
    bot = instance;
    bot.on('ready', () => { connected = true; blocked = false; });
    bot.on('disconnected', event => { connected = false; blocked = !event.willReconnect; });
    // Runs before the upstream conversational middleware. Commands, reminder
    // actions and chat keep the existing transactional Python/Harness path.
    bot.use(async context => {
      const msg = context.message;
      if (msg.kind !== 'c2c' || typeof msg.content !== 'string') return;
      const text = msg.content.trim();
      if (pairing && Date.now() < pairing.expires && equal(text, '/bind ' + pairing.code)) {
        if (!pairing.owner) pairing.owner = msg.senderId;
        return;
      }
      if (!owner || msg.senderId !== owner || !text || !msg.messageId) return;
      const key = hash(String(msg.messageId));
      const path = join(inbox, key + '.json');
      if (!existsSync(path)) {
        const timestamp = Date.parse(msg.timestamp);
        save(path, { order: ++sequence, event: { message_type: 'private', user_id: owner, message_id: String(msg.messageId),
          message: Array.from(text).slice(0, 2001).join(''),
          timestamp: Number.isFinite(timestamp) ? Math.floor(timestamp / 1000) : null } });
      }
    });
  }
  async function send(value) {
    // Validate on the send itself, before receipt lookup or the asynchronous
    // QQ request: the two containers may have different configuration during updates.
    if (value.app_id !== appId || value.owner_openid !== owner) return { error: { status: 409 } };
    if (!bot || !owner || blocked) return { error: { status: blocked ? 403 : 503 } };
    if (typeof value.text !== 'string' || !value.text.trim() || value.text.length > 12000
        || typeof value.delivery_id !== 'string' || !value.delivery_id) return { error: { status: 400 } };
    const isDate = value.delivery_id.startsWith('date_reminder:');
    const key = hash(isDate ? value.delivery_id : value.delivery_id + '\n' + value.text);
    const path = join(receipts, key + '.json');
    if (existsSync(path)) return JSON.parse(readFileSync(path, 'utf8'));
    if (isDate) {
      // Older versions keyed date receipts by the rendered text, including its late prefix.
      const onTime = value.text.replace(/^迟到的日期提醒：/, '日期提醒：');
      for (const text of [onTime, '迟到的' + onTime]) {
        const old = join(receipts, hash(value.delivery_id + '\n' + text) + '.json');
        if (existsSync(old)) {
          renameSync(old, path);
          return JSON.parse(readFileSync(path, 'utf8'));
        }
      }
    }
    if (sending.has(key)) return sending.get(key);
    const pending = (async () => {
      const target = { scope: 'c2c', targetId: owner };
      const age = Date.now() / 1000 - value.reply_at;
      if (typeof value.reply_to === 'string' && typeof value.reply_at === 'number' && age >= 0 && age < 240) {
        target.msgId = value.reply_to;
      }
      try {
        const deliver = () => bot.send({ target, content: value.text, msgType: 0, extra: { msg_seq: 1 } });
        let result;
        try { result = await deliver(); } catch (error) {
          if (error.httpStatus !== 401) throw error;
          bot.tokenManager.clearCache(appId);
          result = await deliver();
        }
        const code = result?.code ?? result?.err_code;
        if ((code !== undefined && code !== 0 && code !== '0') || typeof result?.id !== 'string' || !result.id) {
          return { error: { status: 200, code: Number(code) || undefined } };
        }
        const receipt = { id: result.id };
        save(path, receipt);
        return receipt;
      } catch (error) {
        // Never return upstream response bodies: they can contain message text.
        return { error: { status: Number.isInteger(error.httpStatus) ? error.httpStatus || 503 : 503,
          code: Number.isInteger(error.bizCode) ? error.bizCode : undefined } };
      }
    })();
    sending.set(key, pending);
    try { return await pending; } finally { sending.delete(key); }
  }
  const server = http.createServer(async (req, res) => {
    if (!equal(req.headers.authorization, 'Bearer ' + token)) return response(res, 403, { error: 'forbidden' });
    try {
      if (req.method === 'GET' && req.url === '/status') {
        return response(res, 200, { plugin: '@tencent-connect/dsh-qqbot@0.5.5', configured: !!bot,
          connected, blocked, owner_bound: !!owner, owner_openid: owner, app_id: appId });
      }
      if (req.method === 'GET' && req.url === '/events') {
        const events = readdirSync(inbox).filter(n => n.endsWith('.json')).map(n => ({ key: n.slice(0, -5),
          ...JSON.parse(readFileSync(join(inbox, n), 'utf8')) }));
        events.sort((a, b) => a.order - b.order);
        return response(res, 200, { events: events.slice(0, 100) });
      }
      if (req.method === 'GET' && req.url === '/bind') {
        return response(res, 200, { owner: pairing && Date.now() < pairing.expires ? pairing.owner || null : null });
      }
      if (req.method !== 'POST') return response(res, 404, { error: 'not_found' });
      const value = await body(req);
      if (req.url === '/ack' && /^[a-f0-9]{64}$/.test(value.key || '')) {
        const path = join(inbox, value.key + '.json');
        if (existsSync(path)) unlinkSync(path);
        return response(res, 200, { ok: true });
      }
      if (req.url === '/send') {
        const result = await send(value);
        return response(res, result.error ? 502 : 200, result);
      }
      if (req.url === '/bind') {
        if (!bot || blocked) return response(res, 503, { error: 'configure_bot_first' });
        pairing = { code: randomBytes(18).toString('base64url'), expires: Date.now() + 300000 };
        return response(res, 200, { code: pairing.code });
      }
      response(res, 400, { error: 'invalid_request' });
    } catch (error) {
      response(res, error instanceof SyntaxError || error.message === 'invalid_request'
        || error.message === 'request_too_large' ? 400 : 500, { error: 'bridge_request_failed' });
    }
  });
  server.requestTimeout = 10000;
  server.headersTimeout = 10000;
  return { attach, server };
}

export async function apply(ctx) {
  const bridge = createBridge({ token: process.env.HARNESS_AUTH_TOKEN,
    appId: process.env.QQBOT_APPID || '', owner: process.env.QQ_BOT_OWNER_OPENID || '',
    directory: process.env.QQ_BRIDGE_DATA || '/data/qq-bridge' });
  await new Promise((resolve, reject) => {
    bridge.server.once('error', reject);
    bridge.server.listen(Number(process.env.QQ_BRIDGE_PORT || 8081), process.env.QQ_BRIDGE_HOST || '0.0.0.0', resolve);
  });
  ctx.reflect.provide('qqAssistantBridge', bridge);
  ctx.on('dispose', () => new Promise(resolve => bridge.server.close(resolve)));
}
