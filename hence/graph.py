"""Microsoft Graph delegated device-code authentication and read-only delta sync."""
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCOPES = 'offline_access https://graph.microsoft.com/Mail.ReadBasic'
INITIAL_URL = 'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$select=id,subject,from'


def token_url(tenant, action='token'):
    if not re.fullmatch(r'[A-Za-z0-9.-]+', tenant):
        raise ValueError('invalid tenant')
    return f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/{action}'


def post_form(url, form):
    request = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method='POST')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            error = json.load(exc)
        except (ValueError, OSError):
            raise RuntimeError(f'OAuth request failed with HTTP {exc.code}') from None
        raise OAuthError(error.get('error', 'unknown_error')) from None


class OAuthError(RuntimeError):
    pass


def save_token(path, data):
    directory = str(Path(path).parent)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=directory, prefix='.graph-')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def get_access_token(path):
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if data.get('expires_at', 0) > time.time() + 120:
        return data['access_token']
    result = post_form(token_url(data['tenant']), {'client_id':data['client_id'], 'scope':SCOPES,
        'grant_type':'refresh_token', 'refresh_token':data['refresh_token']})
    data.update(access_token=result['access_token'], refresh_token=result.get('refresh_token', data['refresh_token']),
                expires_at=time.time() + int(result.get('expires_in', 3600)))
    save_token(path, data)
    return data['access_token']


def fetch_page(box, url):
    url = url or INITIAL_URL
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'graph.microsoft.com' or not parsed.path.startswith('/v1.0/'):
        raise ValueError('invalid Graph cursor URL')
    token = get_access_token(box['token_file'])
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}',
        'Prefer': 'IdType="ImmutableId"', 'Accept':'application/json'})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)
