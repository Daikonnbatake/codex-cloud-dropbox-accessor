import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

SCRIPT = Path(__file__).resolve().parents[1] / '.agents/skills/edit-dropbox-obsidian/scripts/dropbox_obsidian.py'
spec = importlib.util.spec_from_file_location('editor', SCRIPT)
editor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(editor)


class EditorTests(unittest.TestCase):
    def setUp(self):
        work = SCRIPT.parents[4] / '.dropbox-work'
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'DROPBOX_VAULT_ROOT': '/Apps/remotely-save/Vault',
            'DROPBOX_APP_KEY': 'app', 'DROPBOX_REFRESH_TOKEN': 'secret-refresh'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.args = argparse.Namespace(credentials=str(self.base / 'access.json'),
            note='日記/テスト.md', out=str(self.base / 'bundle'), bundle=str(self.base / 'bundle'), confirm=True)
        # Fake test credentials stay in ignored workspace; production path rejection is tested separately.
        self.placement = patch.object(editor.Path, 'is_relative_to', return_value=False)
        self.placement.start()
        self.addCleanup(self.placement.stop)
        editor.private_json(self.args.credentials, {'access_token': 'secret-access', 'expires_at': time.time() + 1000})

    def checkout(self, data=b'old\n'):
        with patch.object(editor, 'request', return_value=(data, {'Dropbox-API-Result': json.dumps({'rev': '0123456789'})})):
            editor.checkout(self.args)

    def test_unicode_checkout_and_revision_guard(self):
        original = '日本語\r\n'.encode()
        with patch.object(editor, 'request', return_value=(original, {'Dropbox-API-Result': '{"rev":"0123456789"}'})) as call:
            editor.checkout(self.args)
            call.call_args.args[2]['Dropbox-API-Arg'].encode('ascii')
        self.assertEqual((Path(self.args.out) / 'original.md').read_bytes(), original)
        (Path(self.args.out) / 'edited.md').write_bytes('更新\n'.encode())
        with patch.object(editor, 'request', return_value=(b'{"rev":"abcdef0123"}', {})) as call:
            editor.apply(self.args)
            options = json.loads(call.call_args.args[2]['Dropbox-API-Arg'])
            self.assertEqual(options['mode'], {'.tag': 'update', 'update': '0123456789'})
            self.assertTrue(options['strict_conflict'])
            self.assertFalse(options['autorename'])
            self.assertIn('client_modified', options)

    def test_no_change_and_local_overwrite(self):
        self.checkout()
        with patch.object(editor, 'request') as call:
            editor.apply(self.args)
            call.assert_not_called()
            with self.assertRaises(editor.Failure):
                editor.checkout(self.args)
            call.assert_not_called()
        with self.assertRaises(FileExistsError):
            editor.private_json(self.args.credentials, {})

    def test_paths_and_bundle_tampering(self):
        for path in ('../x.md', '/x.md', 'C:/x.md', 'a/../x.md', 'a\\b.md', '.obsidian/x.md', 'a//x.md', 'x\n.md', 'x.txt'):
            with self.subTest(path=path), self.assertRaises(editor.Failure):
                editor.relative(path)
        for value in ('/', '', 'relative', '/Vault/../Other'):
            with patch.dict(os.environ, {'DROPBOX_VAULT_ROOT': value}), self.assertRaises(editor.Failure):
                editor.root()
        self.checkout()
        state_path = Path(self.args.out) / 'state.json'
        state = json.loads(state_path.read_text())
        state['root'] = '/Other'
        state_path.write_text(json.dumps(state))
        with self.assertRaises(editor.Failure):
            editor.bundle(self.args.bundle)
        state['root'] = os.environ['DROPBOX_VAULT_ROOT']
        state_path.write_text(json.dumps(state))
        (Path(self.args.out) / 'original.md').write_bytes(b'changed')
        with self.assertRaises(editor.Failure):
            editor.bundle(self.args.bundle)

    def test_pagination_and_root_case(self):
        results = [{'entries': [{'.tag': 'file', 'path_display': '/apps/remotely-save/vault/日本語.md'},
                    {'.tag': 'file', 'path_display': '/Apps/remotely-save/Vault/.obsidian/a.md'}],
                    'has_more': True, 'cursor': 'next'},
                   {'entries': [{'.tag': 'file', 'path_display': '/Apps/remotely-save/Vault/b.md'}], 'has_more': False}]
        with patch.object(editor, 'json_request', side_effect=results) as call, contextlib.redirect_stdout(io.StringIO()) as output:
            editor.list_notes(self.args)
        self.assertEqual(output.getvalue().splitlines(), ['日本語.md', 'b.md'])
        self.assertEqual(call.call_args.args[0], 'files/list_folder/continue')

    def test_expiry_and_utf8(self):
        Path(self.args.credentials).write_text(json.dumps({'access_token': 'secret', 'expires_at': 1}))
        with self.assertRaisesRegex(editor.Failure, 'expired'):
            editor.token(self.args.credentials)
        for data in (b'\xff', b'a\0b', b'x' * (editor.MAX_NOTE + 1)):
            with self.assertRaises(editor.Failure):
                editor.validate_note(data)

    def test_pkce_prepare_no_secret_output(self):
        self.args.out = str(self.base / 'refresh-token.json')
        with patch.object(editor, 'exchange', return_value={'refresh_token': 'secret-new-refresh'}) as call, \
                patch.object(editor.getpass, 'getpass', return_value='secret-code'), contextlib.redirect_stdout(io.StringIO()) as output:
            editor.authorize(self.args)
        self.assertIn('code_challenge_method=S256', output.getvalue())
        for secret in ('secret-new-refresh', 'secret-code', 'secret-refresh'):
            self.assertNotIn(secret, output.getvalue())
        self.assertIn('code_verifier', call.call_args.args[0])
        self.args.credentials = str(self.base / 'prepared.json')
        with patch.object(editor, 'exchange', return_value={'access_token': 'secret-new-access', 'expires_in': 14400}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            editor.prepare(self.args)
        self.assertNotIn('secret', output.getvalue())
        self.assertEqual(set(json.loads(Path(self.args.credentials).read_text())), {'access_token', 'expires_at'})
        with patch.object(editor, 'exchange', return_value={'access_token': 'replacement', 'expires_in': 14400}):
            editor.prepare(self.args)
        self.assertEqual(json.loads(Path(self.args.credentials).read_text())['access_token'], 'replacement')

    def test_credential_workspace_boundary(self):
        self.placement.stop()
        with self.assertRaisesRegex(editor.Failure, 'outside'):
            editor.private_json(self.base / 'bad.json', {})
        repo = SCRIPT.parents[4]
        lexical = repo.parent / 'outside-root' / '..' / repo.name / '.dropbox-work' / 'must-not-exist.json'
        with self.assertRaisesRegex(editor.Failure, 'outside'):
            editor.private_json(lexical, {'access_token': 'fake-only'})
        self.assertFalse((repo / '.dropbox-work/must-not-exist.json').exists())

    def test_symlink_input_rejected(self):
        with patch.object(editor.Path, 'is_symlink', return_value=True):
            with self.assertRaises(editor.Failure):
                editor.checkout(self.args)
            with self.assertRaises(editor.Failure):
                editor.regular(Path(self.args.credentials))

    def test_failed_upload_keeps_bundle_and_never_retries(self):
        self.checkout()
        (Path(self.args.out) / 'edited.md').write_bytes(b'new')
        for code, message in ((409, 'conflict'), (503, 'unknown')):
            error = urllib.error.HTTPError('https://content.dropboxapi.com', code, 'failure', {}, None)
            with patch.object(editor.urllib.request, 'build_opener') as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaisesRegex(editor.Failure, message):
                    editor.apply(self.args)
                self.assertEqual(opener.return_value.open.call_count, 1)
            self.assertEqual((Path(self.args.out) / 'edited.md').read_bytes(), b'new')
        with patch.object(editor.urllib.request, 'build_opener') as opener:
            opener.return_value.open.side_effect = TimeoutError()
            with self.assertRaisesRegex(editor.Failure, 'unknown'):
                editor.apply(self.args)
            self.assertEqual(opener.return_value.open.call_count, 1)

    def test_diff_missing_newline_and_confirm(self):
        self.checkout(b'old')
        (Path(self.args.out) / 'edited.md').write_bytes(b'new')
        self.args.confirm = False
        with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(editor, 'request') as call:
            with self.assertRaisesRegex(editor.Failure, '--confirm'):
                editor.apply(self.args)
            call.assert_not_called()
        self.assertIn('-old\n\\ No newline', output.getvalue())
        self.assertIn('+new\n\\ No newline', output.getvalue())


if __name__ == '__main__':
    unittest.main()
