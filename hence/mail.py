import email
import imaplib
import os
import ssl
from email.header import decode_header, make_header


def clean_header(value):
    try:
        text = str(make_header(decode_header(value or '')))
    except (LookupError, UnicodeError, ValueError):
        text = value or ''
    return ' '.join(text.split())[:200] or '（无）'


def require_ok(result, action):
    status, data = result
    if status != 'OK':
        raise RuntimeError(f'IMAP {action} failed')
    return data


def poll_mailbox(box, state):
    """Read only headers. Return baseline or ordered new messages; no database access here."""
    with imaplib.IMAP4_SSL(box['host'], box.get('port', 993), ssl_context=ssl.create_default_context(), timeout=30) as conn:
        require_ok(conn.login(box['username'], os.environ[box['password_env']]), 'login')
        require_ok(conn.select('INBOX', readonly=True), 'select')
        response = conn.response('UIDVALIDITY')[1]
        if not response or not response[0]:
            raise RuntimeError('IMAP server did not provide UIDVALIDITY')
        validity = response[0].decode('ascii')
        if state and state['uidvalidity'] != validity:
            raise RuntimeError('UIDVALIDITY changed; manual review required before resetting mailbox state')
        data = require_ok(conn.uid('search', None, 'ALL' if state is None else f"UID {state['last_uid'] + 1}:*"), 'search')
        uids = sorted(int(uid) for uid in (data[0] or b'').split())
        if state is None:
            return validity, max(uids, default=0), None
        uids = [uid for uid in uids if uid > state['last_uid']]
        result = []
        for uid in uids[:100]:
            fetched = require_ok(conn.uid('fetch', str(uid), '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])'), 'fetch')
            raw = next((part[1] for part in fetched if isinstance(part, tuple) and isinstance(part[1], bytes)), None)
            if raw is None:
                # A message may have been deleted between SEARCH and FETCH. Do not advance past it;
                # the next SEARCH will no longer contain it.
                raise RuntimeError(f'IMAP UID {uid} disappeared during fetch')
            msg = email.message_from_bytes(raw)
            text = f"📬 新邮件（{box.get('name', box['id'])}）\n发件人：{clean_header(msg.get('From'))}\n主题：{clean_header(msg.get('Subject'))}"
            result.append((uid, text))
        return validity, None, result
