"""Run interactively to connect one Microsoft 365 account using device authorization."""
import argparse
import time

from .graph import OAuthError, SCOPES, post_form, save_token, token_url


def main():
    parser = argparse.ArgumentParser(description='Authorize a Microsoft 365 mailbox using a public Entra app.')
    parser.add_argument('--client-id', required=True)
    parser.add_argument('--tenant', default='organizations', help='Tenant ID, organizations, or common')
    parser.add_argument('--output', required=True, help='Token file under /data, e.g. /data/graph-school.json')
    args = parser.parse_args()
    if not args.output.startswith('/data/') or '..' in args.output.split('/'):
        parser.error('--output must be a path under /data/')
    code = post_form(token_url(args.tenant, 'devicecode'), {'client_id': args.client_id, 'scope': SCOPES})
    print(code['message'], flush=True)
    deadline = time.monotonic() + int(code['expires_in'])
    interval = int(code.get('interval', 5))
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            result = post_form(token_url(args.tenant), {'grant_type':'urn:ietf:params:oauth:grant-type:device_code',
                'client_id':args.client_id, 'device_code':code['device_code']})
            if 'refresh_token' not in result:
                raise RuntimeError('No refresh token returned; check offline_access permission')
            save_token(args.output, {'client_id':args.client_id, 'tenant':args.tenant,
                'access_token':result['access_token'], 'refresh_token':result['refresh_token'],
                'expires_at':time.time() + int(result.get('expires_in', 3600))})
            print('Authorization saved.')
            return
        except OAuthError as exc:
            if str(exc) == 'authorization_pending':
                continue
            if str(exc) == 'slow_down':
                interval += 5
                continue
            raise
    raise RuntimeError('Device authorization expired')


if __name__ == '__main__':
    main()
