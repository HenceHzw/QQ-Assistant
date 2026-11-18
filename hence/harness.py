import json
import urllib.error
import urllib.request
from datetime import datetime
from .commands import CAPABILITIES, HELP

SNAPSHOT_LIMIT = 30


def ask_harness(url, token, text, now, tasks, dates=(), pending_confirmation=None, dates_total=None):
    payload = {'text': text, 'now': now.isoformat(), 'commands': HELP, 'capabilities': CAPABILITIES,
               'tasks_total': len(tasks), 'tasks': [],
               'dates_total': len(dates) if dates_total is None else dates_total, 'dates': [],
               'pending_confirmation': pending_confirmation}
    def encode():
        return json.dumps(payload, ensure_ascii=False).encode()
    if len(encode()) > 60000:
        raise ValueError('message exceeds Harness size budget')
    for index, row in enumerate(tasks[:SNAPSHOT_LIMIT], 1):
        item = {'id': index, 'title': row['title'], 'due': datetime.fromtimestamp(row['due'], now.tzinfo).isoformat(),
                'status': row['status'], 'kind': row['kind']}
        payload['tasks'].append(item)
        if len(encode()) > 60000:
            payload['tasks'].pop()
            break
    for index, row in enumerate(dates[:SNAPSHOT_LIMIT], 1):
        alerts = row['alerts'] if 'alerts' in row.keys() else ()
        item = {'id': index, 'title': row['title'], 'date': datetime.fromtimestamp(row['due'], now.tzinfo).strftime('%Y-%m-%d'),
                'status': row['status'], 'show_from': datetime.fromtimestamp(row['visible_from'], now.tzinfo).strftime('%Y-%m-%d'),
                'alerts': [datetime.fromtimestamp(ts, now.tzinfo).isoformat() for ts in alerts]}
        payload['dates'].append(item)
        if len(encode()) > 60000:
            payload['dates'].pop()
            break
    request = urllib.request.Request(url, data=encode(),
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            obj = json.load(response)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 422:
            raise ValueError('invalid model reply') from exc
        raise
    if not isinstance(obj, dict):
        raise ValueError('invalid Harness response')
    # Counts come from the local request, not the model's JSON.
    obj['_tasks_sent'] = len(payload['tasks'])
    obj['_dates_sent'] = len(payload['dates'])
    return obj
