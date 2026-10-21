#!/usr/bin/env python3
"""Scoped Dropbox Markdown editor; Python standard library only."""
import argparse
import base64
import difflib
import getpass
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

MAX_NOTE = 10 * 1024 * 1024
DEFAULT_TOKEN = Path.home() / '.config/codex-dropbox/access.json'


class Failure(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Failure('Redirect rejected; no credentials forwarded')


def request(url, data, headers=None, upload=False):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=60) as response:
            return response.read(MAX_NOTE + 1), response.headers
    except urllib.error.HTTPError as exc:
        if upload and exc.code >= 500:
            raise Failure('Upload outcome unknown after server error; compare remote note before retry') from None
        if upload and exc.code == 409:
            raise Failure('Dropbox conflict/deleted note; obtain a new checkout and review; bundle retained') from None
        raise Failure(f'Dropbox HTTP {exc.code}; bundle retained; no automatic retry') from None
    except (TimeoutError, socket.timeout, urllib.error.URLError, http.client.HTTPException, ConnectionError):
        detail = 'Upload outcome unknown; compare remote note before any retry' if upload else 'Network request failed'
        raise Failure(detail) from None


def json_request(endpoint, payload, token):
    raw, _ = request('https://api.dropboxapi.com/2/' + endpoint,
                     json.dumps(payload).encode(), {'Authorization': 'Bearer ' + token,
                                                   'Content-Type': 'application/json'})
    return json.loads(raw)


def required(name):
    value = os.environ.get(name, '')
    if not value:
        raise Failure(f'Set {name}')
    return value


def parts(value):
    if not isinstance(value, str) or '\\' in value or ':' in value or any(unicodedata.category(c) == 'Cc' for c in value):
        raise Failure('Invalid path')
    result = value.split('/')
    if any(not p or p in ('.', '..') or p.startswith('.') for p in result):
        raise Failure('Empty, traversal, or hidden path component rejected')
    return result


def root():
    value = required('DROPBOX_VAULT_ROOT')
    if not value.startswith('/'):
        raise Failure('Vault root must be an absolute non-root folder')
    parts(value[1:])
    return value


def relative(value):
    parts(value)
    if not value.lower().endswith('.md'):
        raise Failure('Only existing .md notes are supported')
    return value


def regular(path):
    if path.is_symlink() or not path.is_file():
        raise Failure(f'Expected regular file: {path.name}')
    if any(p.is_symlink() for p in path.parents):
        raise Failure('Symlink parent rejected')
    return path


def read_note(path):
    with regular(path).open('rb') as source:
        data = source.read(MAX_NOTE + 1)
    validate_note(data)
    return data


def validate_note(data):
    if len(data) > MAX_NOTE or b'\0' in data:
        raise Failure('Note exceeds 10 MiB or contains NUL')
    try:
        data.decode('utf-8')
    except UnicodeDecodeError:
        raise Failure('Note must be plain UTF-8') from None


def private_json(path, value, replace=False):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise Failure('Symlink credential destination rejected')
    path = path.resolve(strict=False)
    # Credentials belong outside the checkout/workspace, even if ignored.
    repo = Path(__file__).resolve().parents[4]
    if path.is_relative_to(repo) or path.is_relative_to(Path.cwd().resolve()):
        raise Failure('Credential output must be outside the repository/workspace')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_permissions(path.parent)
    if replace and path.exists():
        previous = json.loads(regular(path).read_text(encoding='utf-8'))
        private_permissions(path)
        if set(previous) != {'access_token', 'expires_at'}:
            raise Failure('Existing file is not an access credential; choose another path')
        fd, temporary = tempfile.mkstemp(prefix='.access-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(value, output)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return
    with open(path, 'x', encoding='utf-8', opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
        json.dump(value, output)


def private_permissions(path):
    if os.name == 'posix':
        stat = path.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise Failure('Use an owner-only private credential directory/file (0700/0600)')


def token(path):
    try:
        source = regular(Path(path).expanduser())
        private_permissions(source)
        value = json.loads(source.read_text(encoding='utf-8'))
        if (set(value) != {'access_token', 'expires_at'} or not isinstance(value['access_token'], str)
                or not value['access_token'] or not isinstance(value['expires_at'], (int, float))
                or not math.isfinite(value['expires_at'])):
            raise ValueError()
        if value['expires_at'] <= time.time() + 30:
            raise Failure('Access token expired; remove cache file and rerun setup/prepare or reset environment cache')
        return value['access_token']
    except (OSError, ValueError, TypeError, KeyError):
        raise Failure('Invalid access credential file; rerun setup/prepare') from None


def exchange(fields):
    raw, _ = request('https://api.dropboxapi.com/oauth2/token', urllib.parse.urlencode(fields).encode(),
                     {'Content-Type': 'application/x-www-form-urlencoded'})
    return json.loads(raw)


def authorize(args):
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    app_key = required('DROPBOX_APP_KEY')
    print('Open this URL and approve your own app:')
    print('https://www.dropbox.com/oauth2/authorize?' + urllib.parse.urlencode({
        'client_id': app_key, 'response_type': 'code', 'token_access_type': 'offline',
        'code_challenge': challenge, 'code_challenge_method': 'S256'}))
    result = exchange({'grant_type': 'authorization_code', 'code': getpass.getpass('Authorization code: '),
                       'client_id': app_key, 'code_verifier': verifier})
    refresh = result.get('refresh_token')
    if not isinstance(refresh, str) or not refresh:
        raise Failure('OAuth response missing refresh token')
    private_json(args.out, {'refresh_token': refresh})
    print('Refresh token saved to private output file; transfer through Cloud secret settings')


def prepare(args):
    result = exchange({'grant_type': 'refresh_token', 'refresh_token': required('DROPBOX_REFRESH_TOKEN'),
                       'client_id': required('DROPBOX_APP_KEY')})
    access, lifetime = result.get('access_token'), result.get('expires_in')
    if (not isinstance(access, str) or not access or not isinstance(lifetime, (int, float))
            or not math.isfinite(lifetime) or lifetime <= 0):
        raise Failure('Invalid token response')
    private_json(args.credentials, {'access_token': access, 'expires_at': time.time() + lifetime}, replace=True)
    print('Short-lived credential file created')


def list_notes(args):
    base, access = root(), token(args.credentials)
    result = json_request('files/list_folder', {'path': base, 'recursive': True, 'include_deleted': False}, access)
    while True:
        for entry in result['entries']:
            path = entry.get('path_display', '')
            if entry.get('.tag') == 'file' and path.lower().startswith(base.lower() + '/'):
                try:
                    print(relative(path[len(base) + 1:]))
                except Failure:
                    pass
        if not result['has_more']:
            break
        result = json_request('files/list_folder/continue', {'cursor': result['cursor']}, access)


def checkout(args):
    base, name = root(), relative(args.note)
    out = Path(args.out)
    if any(p.is_symlink() for p in (out, *out.parents)) or out.exists():
        raise Failure('Output bundle must be a new directory without symlink parents')
    raw, headers = request('https://content.dropboxapi.com/2/files/download', b'', {
        'Authorization': 'Bearer ' + token(args.credentials),
        'Dropbox-API-Arg': json.dumps({'path': base + '/' + name}, ensure_ascii=True)})
    validate_note(raw)
    meta = json.loads(headers['Dropbox-API-Result'])
    rev = meta.get('rev', '')
    if not re.fullmatch(r'[0-9a-fA-F]{9,}', rev):
        raise Failure('Download missing valid revision')
    out.mkdir(parents=True, exist_ok=False)
    (out / 'original.md').write_bytes(raw)
    (out / 'edited.md').write_bytes(raw)
    (out / 'state.json').write_text(json.dumps({'root': base, 'relative': name, 'rev': rev,
        'sha256': hashlib.sha256(raw).hexdigest()}), encoding='utf-8')
    print('Checked out existing note to ' + str(out))


def bundle(path):
    directory = Path(path)
    if directory.is_symlink() or not directory.is_dir():
        raise Failure('Invalid bundle directory')
    try:
        state = json.loads(regular(directory / 'state.json').read_text(encoding='utf-8'))
        if set(state) != {'root', 'relative', 'rev', 'sha256'} or state['root'] != root():
            raise ValueError()
        relative(state['relative'])
        if not isinstance(state['rev'], str) or not re.fullmatch(r'[0-9a-fA-F]{9,}', state['rev']):
            raise ValueError()
        original, edited = read_note(directory / 'original.md'), read_note(directory / 'edited.md')
        if hashlib.sha256(original).hexdigest() != state['sha256']:
            raise ValueError()
        return state, original, edited
    except (OSError, ValueError, KeyError, TypeError):
        raise Failure('Invalid or modified bundle baseline/metadata') from None


def show_diff(original, edited):
    # keepends preserves CRLF and missing-final-newline distinctions in the snapshots.
    lines = difflib.unified_diff(original.decode('utf-8').splitlines(keepends=True),
        edited.decode('utf-8').splitlines(keepends=True), fromfile='original.md', tofile='edited.md')
    output = ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n' for line in lines)
    print(output, end='' if output.endswith('\n') or not output else '\n')


def apply(args):
    state, original, edited = bundle(args.bundle)
    show_diff(original, edited)
    if not args.confirm:
        raise Failure('Review diff first; apply requires --confirm and user authorization to save')
    if original == edited:
        print('Unchanged; no upload')
        return
    raw, _ = request('https://content.dropboxapi.com/2/files/upload', edited, {
        'Authorization': 'Bearer ' + token(args.credentials), 'Content-Type': 'application/octet-stream',
        'Dropbox-API-Arg': json.dumps({'path': state['root'] + '/' + state['relative'],
            'mode': {'.tag': 'update', 'update': state['rev']}, 'autorename': False,
            'strict_conflict': True, 'client_modified': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')},
            ensure_ascii=True)}, upload=True)
    result = json.loads(raw)
    if not result.get('rev'):
        raise Failure('Upload response invalid; outcome unknown, compare remote note')
    print('Remote save succeeded; revision ' + result['rev'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', default=str(DEFAULT_TOKEN), help='Short-lived token file outside workspace')
    sub = parser.add_subparsers(dest='command', required=True)
    auth = sub.add_parser('authorize', help='Local interactive PKCE setup')
    auth.add_argument('--out', required=True)
    sub.add_parser('prepare', help='Cloud setup: exchange environment secrets for short-lived access')
    sub.add_parser('list', help='List existing Markdown paths in vault')
    co = sub.add_parser('checkout')
    co.add_argument('note')
    co.add_argument('--out', required=True)
    for command in ('diff', 'apply'):
        child = sub.add_parser(command)
        child.add_argument('bundle')
        if command == 'apply':
            child.add_argument('--confirm', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'diff':
            _, original, edited = bundle(args.bundle)
            show_diff(original, edited)
        else:
            {'authorize': authorize, 'prepare': prepare, 'list': list_notes,
             'checkout': checkout, 'apply': apply}[args.command](args)
        return 0
    except (Failure, OSError, ValueError, KeyError, TypeError) as exc:
        # Do not print response bodies, tokens, environment values, or request objects.
        print(str(exc) if isinstance(exc, Failure) else 'Invalid local data or API response', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
