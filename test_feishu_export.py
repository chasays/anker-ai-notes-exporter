import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import feishu_export as export


class CredentialTests(unittest.TestCase):
    def test_default_credentials_are_local_to_script(self):
        self.assertEqual(export.DEFAULT_CREDENTIALS,
                         Path(export.__file__).resolve().parent / '.env')

    def parse(self, text):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'credentials'
            file.write_text(text, encoding='utf-8')
            return export.credentials(file)

    def test_existing_markdown_format(self):
        self.assertEqual(self.parse('App ID\ncli_test123\nApp Secret\n"test-secret"\n'),
                         ('cli_test123', 'test-secret'))

    def test_json_and_env(self):
        for text in ['{"app_id":"cli_test123","app_secret":"test-secret"}',
                     'APP_ID=cli_test123\nAPP_SECRET=test-secret']:
            self.assertEqual(self.parse(text), ('cli_test123', 'test-secret'))

    def test_dotenv_comments_quotes_and_export(self):
        self.assertEqual(self.parse(
            '# Local application credentials\n'
            'export APP_ID=cli_test123 # application\n'
            'APP_SECRET="test # secret" # keep the quoted hash\n'),
            ('cli_test123', 'test # secret'))

    def test_dotenv_values_are_not_interpolated(self):
        self.assertEqual(self.parse(
            "APP_ID='cli_test123'\nAPP_SECRET='literal-$HOME-#secret'\n"),
            ('cli_test123', 'literal-$HOME-#secret'))

    def test_missing_secret_is_not_echoed(self):
        with self.assertRaises(export.ExportError) as cm:
            self.parse('App ID\ncli_test123\nApp Secret\n')
        self.assertNotIn('cli_test123', str(cm.exception))


class SearchTests(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(export.duration_seconds('时长: 1 小时 18 分 16 秒'), 4696)
        self.assertEqual(export.duration_seconds('时长: 39 秒'), 39)

    def test_paginate_deduplicate_and_select_by_time(self):
        older = {'token': 'older', 'display_info': '旧\n开始时间: 2026.10.06 08:00:00 时长: 2 小时'}
        latest = {'token': 'latest', 'display_info': '新\n开始时间: 2026.10.06 16:54:42 时长: 1 小时 18 分 16 秒'}
        short = {'token': 'short', 'display_info': '短\n开始时间: 2026.10.06 19:00:00 时长: 39 秒'}
        class Fake:
            identity = 'user'
            def __init__(self): self.calls = []
            def call(self, args):
                self.calls.append(args)
                if '--participant-ids' in args:
                    return {'data': {'items': [latest], 'has_more': False}}
                if '--page-token' in args:
                    return {'data': {'items': [latest, short], 'has_more': False}}
                return {'data': {'items': [older], 'has_more': True, 'page_token': 'page2'}}
        fake = Fake()
        self.assertEqual(export.find_recording(fake, '2026-10-06', 3600)['token'], 'latest')
        self.assertEqual(len(fake.calls), 3)

    def test_incomplete_pagination_is_not_silently_accepted(self):
        class Fake:
            identity = 'bot'
            def call(self, args): return {'data': {'items': [], 'has_more': True}}
        with self.assertRaises(export.ExportError):
            export.find_recording(Fake(), '2026-10-06', 0)


class DownloadSafetyTests(unittest.TestCase):
    def test_only_exact_https_media_host(self):
        export.media_url('https://internal-api-drive-stream.feishu.cn/path?signature=test')
        for url in ['http://internal-api-drive-stream.feishu.cn/a',
                    'https://internal-api-drive-stream.feishu.cn.evil.test/a',
                    'https://name@internal-api-drive-stream.feishu.cn/a',
                    'https://internal-api-drive-stream.feishu.cn:444/a',
                    'https://127.0.0.1/a']:
            with self.assertRaises(export.ExportError): export.media_url(url)

    def test_private_and_fake_ips_rejected(self):
        for ip in ['127.0.0.1', '10.0.0.1', '169.254.0.1', '198.18.0.5',
                   '198.19.255.254', '::1', '::ffff:198.18.0.5', '224.0.0.1']:
            self.assertFalse(export.public_ip(ip), ip)
        self.assertTrue(export.public_ip('98.96.242.53'))

    def test_fake_dns_uses_only_verified_public_answers(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, _):
                return json.dumps({'Status': 0, 'Answer': [{'type': 1, 'data': '98.96.242.53'}]}).encode()
        fake_local = [(2, 1, 6, '', ('198.18.0.5', 443))]
        with patch.object(export.socket, 'getaddrinfo', return_value=fake_local), \
             patch.object(export.urllib.request, 'urlopen', return_value=Response()):
            self.assertEqual(export.resolve_public('internal-api-drive-stream.feishu.cn'),
                             (['98.96.242.53'], 'https-dns'))

    def test_redirect_is_never_followed(self):
        class Response:
            status = 302
        class Connection:
            def __init__(self, host, ip): pass
            def request(self, *args, **kwargs): pass
            def getresponse(self): return Response()
            def close(self): pass
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(export, 'resolve_public', return_value=(['98.96.242.53'], 'system')), \
             patch.object(export, 'PinnedHTTPS', Connection):
            with self.assertRaises(export.ExportError):
                export.download_audio('https://internal-api-drive-stream.feishu.cn/a', Path(folder))
            self.assertEqual(list(Path(folder).iterdir()), [])


class TranscriptTests(unittest.TestCase):
    def test_normal_note_uses_nested_note_and_independent_doc(self):
        class Fake:
            identity = 'user'
            def call(self, args):
                return {'data': {'note': {'note_display_type': 'normal',
                    'note_doc_token': 'notes', 'verbatim_doc_token': 'verbatim'}}}
        state = {}
        with patch.object(export, 'fetch_doc', return_value='逐字稿正文') as fetch:
            self.assertEqual(export.export_note(Fake(), 'note', Path('/tmp/test'), state), '逐字稿正文')
            self.assertEqual([call.args[1] for call in fetch.call_args_list], ['notes', 'verbatim'])
            self.assertEqual(state['transcript_source'], 'AI Notes 关联逐字稿文档')

    def test_metadata_is_not_a_summary(self):
        with self.assertRaises(export.ExportError):
            export.local_summary('测试', '2026-10-06 00:52:29 CST|39s\nKeywords:\n')

    def test_summary_only_quotes_transcript(self):
        sentence = '首页应该先展示会员当前门店信息，切换门店以后及时刷新教练和课程列表。'
        summary = export.local_summary('讨论', sentence)
        self.assertIn(sentence, summary)
        self.assertIn('本地提取式概要', summary)


class SaveTests(unittest.TestCase):
    def test_private_atomic_document_save(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            target = folder / 'transcript.md'
            export.save(target, '完整正文')
            self.assertEqual(target.read_text(), '完整正文')
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(folder.glob('*.part')), [])

    def test_target_symlink_is_replaced_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            other = folder / 'preserved.md'
            other.write_text('保留原文')
            target = folder / 'transcript.md'
            target.symlink_to(other)
            export.save(target, '导出正文')
            self.assertEqual(other.read_text(), '保留原文')
            self.assertEqual(target.read_text(), '导出正文')
            self.assertFalse(target.is_symlink())


if __name__ == '__main__':
    unittest.main()
