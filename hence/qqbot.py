"""Authenticated client for the Tencent plugin bridge; no QQ protocol implementation."""
import argparse
import asyncio
import errno
import html
import http.client
import json
import logging
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request

from .config import qq_app_id

log = logging.getLogger(__name__)
MAX_RESPONSE = 1024 * 1024


class BotAPIError(Exception):
    """Only safe metadata is retained; response bodies may contain private data."""

    def __init__(self, status, code=None):
        self.status = status
        self.code = str(code) if re.fullmatch(r'\d{1,12}', str(code)) else None
        super().__init__(f'QQ API HTTP {status}, code={self.code or "unknown"}')

    @property
    def rate_limited(self):
        return self.status == 429 or self.code == '22009'

    @property
    def retryable(self):
        return self.rate_limited or self.status == 408 or 500 <= self.status < 600


def retryable_delivery_error(exc):
    if isinstance(exc, BotAPIError):
        return exc.retryable
    if isinstance(exc, urllib.error.URLError):
        return retryable_delivery_error(exc.reason)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    if isinstance(exc, OSError) and exc.errno in (errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError, socket.gaierror, ssl.SSLError, http.client.HTTPException))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials to a redirected origin.


def request_json(method, url, token, data=None, *, timeout=45):
    request = urllib.request.Request(url, method=method,
        data=None if data is None else json.dumps(data, ensure_ascii=False).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        response = urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(MAX_RESPONSE + 1)
        status = response.code
    if len(raw) > MAX_RESPONSE:
        raise BotAPIError(502)
    try:
        result = json.loads(raw)
    except ValueError:
        raise BotAPIError(status if status >= 400 else 502) from None
    if not isinstance(result, dict):
        raise BotAPIError(502)
    if not 200 <= status < 300 or result.get('error'):
        error = result.get('error')
        if isinstance(error, dict):
            raise BotAPIError(error.get('status', status), error.get('code'))
        raise BotAPIError(status)
    return result


class QQBot:
    def __init__(self, app_id, owner_openid, on_event, token, url='http://harness:8081'):
        self.app_id, self.owner_openid = app_id, owner_openid
        self.on_event, self.token, self.url = on_event, token, url.rstrip('/')

    async def request(self, method, path, data=None, *, timeout=45):
        return await asyncio.to_thread(request_json, method, self.url + path, self.token, data, timeout=timeout)

    async def send(self, text, *, delivery_id, reply_to=None, reply_at=None):
        result = await self.request('POST', '/send', {'text': text, 'delivery_id': delivery_id,
                                                   'app_id': self.app_id, 'owner_openid': self.owner_openid,
                                                   'reply_to': reply_to, 'reply_at': reply_at})
        if not isinstance(result.get('id'), str) or not result['id']:
            raise BotAPIError(502)

    async def run(self):
        last_state = None
        while True:
            try:
                state = await self.request('GET', '/status')
                if state.get('app_id') != self.app_id or state.get('owner_openid') != self.owner_openid:
                    raise BotAPIError(409)
                summary = (state.get('connected'), state.get('blocked'))
                if summary != last_state:
                    log.info('Official QQ plugin: connected=%s blocked=%s', *summary)
                    last_state = summary
                result = await self.request('GET', '/events')
                for item in result['events']:
                    event = item['event']
                    if event.get('user_id') != self.owner_openid:
                        raise BotAPIError(409)
                    event['message'] = html.unescape(event['message']).strip()
                    if await self.on_event(event) is False:
                        break
                    # Only acknowledge after Python has persisted or already processed it.
                    await self.request('POST', '/ack', {'key': item['key']})
                await asyncio.sleep(1)
            except Exception as exc:
                detail = str(exc) if isinstance(exc, BotAPIError) else type(exc).__name__
                log.warning('QQ plugin bridge unavailable: %s', detail)
                await asyncio.sleep(5)


async def bind_owner(app_id, token, url):
    bot = QQBot(app_id, '', None, token, url)
    deadline = time.monotonic() + 30
    waiting = False
    while True:
        try:
            status = await bot.request('GET', '/status', timeout=3)
        except (OSError, urllib.error.URLError, BotAPIError) as exc:
            if not retryable_delivery_error(exc):
                raise
        else:
            if status.get('app_id') != app_id:
                raise ValueError('插件中的 AppID 与当前配置不同，请重新创建 harness 容器后再绑定。')
            if status.get('blocked'):
                raise ValueError('QQ 连接已被停止，请检查开放平台权限和 harness 日志后重试。')
            if status.get('configured') and status.get('connected'):
                break
        if time.monotonic() >= deadline:
            raise ValueError('等待 30 秒后官方插件仍未就绪，请查看 harness 日志后重试绑定。')
        if not waiting:
            print('正在等待官方插件连接 QQ…', flush=True)
            waiting = True
        await asyncio.sleep(1)
    result = await bot.request('POST', '/bind', {})
    print('请在 5 分钟内，用自己的 QQ 私聊官方机器人发送：', flush=True)
    print('/bind ' + result['code'], flush=True)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        result = await bot.request('GET', '/bind')
        if result.get('owner'):
            print('绑定成功。将下面一行写入 .env，再重新创建 assistant 和 harness：', flush=True)
            print('QQ_BOT_OWNER_OPENID=' + result['owner'], flush=True)
            return
        await asyncio.sleep(1)
    raise TimeoutError()


def main():
    parser = argparse.ArgumentParser(description='绑定腾讯官方插件对应的本人 OpenID')
    parser.add_argument('--bind', action='store_true', required=True)
    parser.parse_args()
    try:
        app_id = qq_app_id()
        token = os.environ.get('HARNESS_AUTH_TOKEN', '')
        if not token:
            raise ValueError('HARNESS_AUTH_TOKEN is required')
        asyncio.run(bind_owner(app_id, token, os.environ.get('QQ_BRIDGE_URL', 'http://harness:8081')))
    except TimeoutError:
        raise SystemExit('绑定超时，请重新运行绑定命令。') from None
    except (ValueError, BotAPIError) as exc:
        raise SystemExit(str(exc)) from None
    except (OSError, urllib.error.URLError):
        raise SystemExit('无法连接官方插件，请先启动 harness 并检查日志。') from None
    except KeyboardInterrupt:
        raise SystemExit('绑定已取消。') from None


if __name__ == '__main__':
    main()
