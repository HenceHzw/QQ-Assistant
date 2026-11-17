import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    owner_qq: int
    onebot_url: str
    onebot_token: str
    db_path: str
    mailboxes: tuple
    poll_seconds: int
    harness_url: str = ""
    harness_token: str = ""

    @classmethod
    def load(cls):
        owner = int(os.environ.get("OWNER_QQ", "0"))
        if owner <= 0:
            raise ValueError("OWNER_QQ must be your personal QQ number")
        url = os.environ.get("ONEBOT_URL", "ws://napcat:3001")
        if not url.startswith(("ws://", "wss://")):
            raise ValueError("ONEBOT_URL must be a ws:// or wss:// URL")
        token = os.environ.get("ONEBOT_TOKEN", "")
        if not token:
            raise ValueError("ONEBOT_TOKEN is required")
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
            if kind == 'imap':
                for field in ('host', 'username', 'password_env'):
                    if not isinstance(box.get(field), str) or not box[field].strip():
                        raise ValueError(f"IMAP mailbox requires {field}")
                if not os.environ.get(box['password_env']):
                    raise ValueError(f"missing mailbox credential environment variable {box['password_env']}")
                if not isinstance(box.get('port', 993), int) or not 1 <= box.get('port', 993) <= 65535:
                    raise ValueError("invalid mailbox port")
            elif kind == 'graph':
                if not isinstance(box.get('token_file'), str) or not box['token_file'].startswith('/data/') or '..' in box['token_file'].split('/'):
                    raise ValueError('Graph token_file must be an absolute path under /data/')
            else:
                raise ValueError(f'unsupported mailbox type: {kind}')
        poll = int(os.environ.get("MAIL_POLL_SECONDS", "120"))
        if poll < 30:
            raise ValueError("MAIL_POLL_SECONDS must be at least 30")
        harness_url = os.environ.get('HARNESS_URL', '')
        harness_token = os.environ.get('HARNESS_AUTH_TOKEN', '')
        if harness_url and (not harness_url.startswith(('http://', 'https://')) or not harness_token):
            raise ValueError('HARNESS_URL requires a valid URL and HARNESS_AUTH_TOKEN')
        return cls(owner, url, token, os.environ.get("DB_PATH", "/data/assistant.sqlite3"), tuple(mailboxes),
                   poll, harness_url, harness_token)
