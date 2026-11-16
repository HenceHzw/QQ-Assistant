import json
import urllib.request
from datetime import datetime, timedelta
from .commands import CAPABILITIES, HELP


def ask_harness(url, token, text, now, tasks):
    payload = {'text': text, 'now': now.isoformat(), 'commands': HELP, 'capabilities': CAPABILITIES,
               'tasks_total': len(tasks), 'tasks': [
        {'id': row['id'], 'title': row['title'], 'due': datetime.fromtimestamp(row['due'], now.tzinfo).isoformat(),
         'status': row['status']} for row in tasks[:30]]}
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=180) as response:
        obj = json.load(response)
    if not isinstance(obj, dict):
        raise ValueError('invalid Harness response')
    return obj


def validated_reminder(obj, now):
    title, due = obj.get('title'), obj.get('due')
    if not isinstance(title, str) or not title.strip() or len(title) > 300 or not isinstance(due, str):
        raise ValueError('invalid title or due')
    due = datetime.fromisoformat(due)
    if due.tzinfo is None or due.utcoffset() != timedelta(hours=8) or due <= now:
        raise ValueError('invalid due time')
    return title.strip(), int(due.timestamp())
