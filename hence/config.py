import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def qq_app_id():
    app_id = os.environ.get('QQ_BOT_APP_ID', '').strip()
    if not re.fullmatch(r'[0-9]{5,20}', app_id):
        raise ValueError('QQ_BOT_APP_ID must be the AppID from QQ Open Platform')
    return app_id


@dataclass(frozen=True)
class Settings:
    owner_openid: str
    qq_app_id: str
    db_path: str
    mailboxes: tuple
    poll_seconds: int
    harness_url: str = ""
    harness_token: str = field(default="", repr=False)

    qq_bridge_url: str = "http://harness:8081"

    @classmethod
    def load(cls):
        app_id = qq_app_id()
        owner = os.environ.get('QQ_BOT_OWNER_OPENID', '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', owner) or owner.startswith('replace_'):
            raise ValueError('Set QQ_BOT_OWNER_OPENID after running python -m hence.qqbot --bind; it is not a QQ number')
        mail_path = os.environ.get("MAIL_CONFIG", "/config/mailboxes.json")
        if not Path(mail_path).is_file():
            raise ValueError(f"mailbox configuration missing: {mail_path}; use [] to disable mail")
        with open(mail_path, encoding="utf-8") as f:
            mailboxes = json.load(f)
        if not isinstance(mailboxes, list):
            raise ValueError("mailboxes configuration must be a list")
        seen = set()
        for box in mailboxes:
            if not isinstance(box, dict) or not isinstance(box.get('id'), str) or not box['id'].strip():
                raise ValueError('each mailbox requires a unique id')
            if box['id'] in seen:
                raise ValueError("mailbox ids must be unique")
            seen.add(box['id'])
            kind = box.get('type', 'imap')
            if kind != 'imap':
                raise ValueError(f'unsupported mailbox type: {kind} (expected imap)')
            for field in ('host', 'username', 'password_env'):
                if not isinstance(box.get(field), str) or not box[field].strip():
                    raise ValueError(f"IMAP mailbox requires {field}")
            if not os.environ.get(box['password_env']):
                raise ValueError(f"missing mailbox credential environment variable {box['password_env']}")
            if not isinstance(box.get('port', 993), int) or not 1 <= box.get('port', 993) <= 65535:
                raise ValueError("invalid mailbox port")
        poll = int(os.environ.get("MAIL_POLL_SECONDS", "120"))
        if poll < 30:
            raise ValueError("MAIL_POLL_SECONDS must be at least 30")
        harness_url = os.environ.get('HARNESS_URL', '')
        harness_token = os.environ.get('HARNESS_AUTH_TOKEN', '')
        if not harness_token:
            raise ValueError('HARNESS_AUTH_TOKEN is required for the QQ plugin bridge')
        if harness_url and not harness_url.startswith(('http://', 'https://')):
            raise ValueError('HARNESS_URL must be an HTTP(S) URL')
        return cls(owner, app_id, os.environ.get("DB_PATH", "/data/assistant.sqlite3"), tuple(mailboxes),
                   poll, harness_url, harness_token, os.environ.get('QQ_BRIDGE_URL', 'http://harness:8081'))
