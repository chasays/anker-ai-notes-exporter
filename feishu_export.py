#!/usr/bin/env python3
"""Export latest Feishu recording, AI notes, transcript and a local extractive summary.

Python 3.10+, standard library + official lark-cli. Credentials stay outside source.
Default: existing user OAuth. Application-only notes: --identity bot --note-id ID.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
DEFAULT_CREDENTIALS = ROOT / 'feishu_credentials.md'
MEDIA_HOSTS = {'internal-api-drive-stream.feishu.cn',
               'internal-api-drive-stream.larksuite.com'}
BENCHMARK = ipaddress.ip_network('198.18.0.0/15')


class ExportError(Exception):
    pass


def credentials(path):
    """JSON, KEY=value or four-line Markdown App ID / value / App Secret / value."""
    try:
        text = Path(path).read_text(encoding='utf-8-sig')
    except OSError:
        raise ExportError(f'无法读取凭据文件：{path}') from None
    values = {}
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            values = {str(k).lower().replace('_', '').replace(' ', ''): str(v)
                      for k, v in obj.items()}
    except ValueError:
        pending = None
        for line in text.splitlines():
            line = line.strip().strip(chr(96)).strip()
            if not line:
                continue
            match = re.match(r'^[*# ]*(app[ _]?id|app[ _]?secret)[* ]*(?::|：|=)?(.*)$', line, re.I)
            if match:
                pending = match[1].lower().replace('_', '').replace(' ', '')
                value = match[2].strip().strip(chr(96) + "\"'").strip()
                if value:
                    values[pending], pending = value, None
            elif pending:
                values[pending], pending = line.strip(chr(96) + "\"'").strip(), None
    app_id, secret = values.get('appid', ''), values.get('appsecret', '')
    if not re.fullmatch(r'cli_[a-zA-Z0-9]+', app_id) or not secret:
        raise ExportError('凭据格式不正确：需要 App ID 和 App Secret；不会打印文件内容。')
    return app_id, secret


def unwrap(result):
    return result.get('data', result)


class CLI:
    def __init__(self, identity='user', profile=None, cwd=ROOT, secret=''):
        self.identity, self.profile, self.cwd, self.secret = identity, profile, cwd, secret
        self.env = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',
                        LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1', TZ='Asia/Shanghai')

    def call(self, args, *, stdin=None, identity=True, timeout=180):
        cmd = ['lark-cli'] + (['--profile', self.profile] if self.profile else []) + list(args)
        if identity:
            cmd += ['--as', self.identity]
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                               cwd=self.cwd, env=self.env, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ExportError('飞书请求超时，请稍后重试。') from None
        except OSError:
            raise ExportError('找不到 lark-cli，请先安装 @larksuite/cli。') from None
        raw = p.stderr if p.returncode else p.stdout
        try:
            result = json.loads(raw[raw.index('{'):])
        except (ValueError, TypeError):
            raise ExportError('CLI 没有返回有效 JSON，请检查安装和登录状态。') from None
        if p.returncode or result.get('ok') is False:
            message = str(result.get('error', {}).get('message', '飞书请求失败'))
            if self.secret:
                message = message.replace(self.secret, '[hidden]')
            message = re.sub(r'https?://[^\s]+', '[URL hidden]', message)
            raise ExportError(message)
        return result


def prepare_cli(args, app_id, secret):
    if not shutil.which('lark-cli'):
        raise ExportError('找不到 lark-cli，请先运行 npm install -g @larksuite/cli。')
    cli = CLI(args.identity, args.profile, secret=secret)
    if args.identity == 'user':
        who = unwrap(cli.call(['whoami'], identity=False))
        if who.get('appId') != app_id:
            raise ExportError('用户会话与凭据文件的 App ID 不一致；请指定正确的 --profile。')
        if not who.get('available'):
            raise ExportError('用户会话不可用，请先用该应用完成 lark-cli auth login。')
        return cli
    if not args.profile:
        # Creates a dedicated bot profile once, with secret via stdin, without --use.
        cli.profile = 'feishu-export-' + hashlib.sha256(app_id.encode()).hexdigest()[:10]
        try:
            who = unwrap(cli.call(['whoami'], identity=False))
        except ExportError:
            CLI(secret=secret).call(['profile', 'add', '--name', cli.profile,
                                    '--app-id', app_id, '--app-secret-stdin', '--brand', 'feishu'],
                                   stdin=secret + '\n', identity=False)
        else:
            if who.get('appId') != app_id:
                raise ExportError('脚本应用 profile 的 App ID 不一致。')
    else:
        who = unwrap(cli.call(['whoami'], identity=False))
        if who.get('appId') != app_id:
            raise ExportError('指定应用 profile 的 App ID 不一致。')
    return cli


def duration_seconds(info):
    text = info.split('时长:', 1)[-1]
    total = 0
    for pattern, scale in [(r'(\d+)\s*小时', 3600), (r'(\d+)\s*分', 60), (r'(\d+)\s*秒', 1)]:
        m = re.search(pattern, text)
        if m:
            total += int(m[1]) * scale
    return total


def start_time(info):
    m = re.search(r'开始时间:\s*(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})', info)
    return m[1] if m else ''


def find_recording(cli, day, minimum):
    found = {}
    for kind in ['--owner-ids', '--participant-ids'] if cli.identity == 'user' else [None]:
        page = ''
        while True:
            args = ['minutes', '+search', '--start', day, '--end', day,
                    '--page-size', '30', '--format', 'json']
            if kind:
                args += [kind, 'me']
            if page:
                args += ['--page-token', page]
            data = unwrap(cli.call(args))
            for item in data.get('items', []):
                found[item['token']] = item
            if not data.get('has_more'):
                break
            next_page = data.get('page_token')
            if not next_page or next_page == page:
                raise ExportError('搜索分页不完整，未擅自选取最新录音。')
            page = next_page
    choices = [x for x in found.values() if duration_seconds(x.get('display_info', '')) >= minimum]
    if not choices:
        raise ExportError(f'{day} 没有找到符合时长条件的录音。')
    return max(choices, key=lambda x: (start_time(x.get('display_info', '')), x['token']))


def public_ip(address):
    try:
        ip = ipaddress.ip_address(address)
        return ip.is_global and not ip.is_multicast and not (
            isinstance(ip, ipaddress.IPv4Address) and ip in BENCHMARK)
    except ValueError:
        return False


def media_url(url):
    parsed = urllib.parse.urlsplit(url)
    try:
        safe = (parsed.scheme == 'https' and parsed.hostname in MEDIA_HOSTS
                and parsed.port in (None, 443) and not parsed.username
                and not parsed.password and not parsed.fragment)
    except ValueError:
        safe = False
    if not safe:
        raise ExportError('媒体 URL 未通过 HTTPS 和飞书媒体域名检查。')
    return parsed


def resolve_public(host):
    try:
        local = sorted({x[4][0] for x in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
    except OSError:
        local = []
    if local and all(public_ip(x) for x in local):
        return local, 'system'
    # Repair synthetic/fake-IP DNS, keeping public-IP checks and verified TLS/SNI.
    url = 'https://dns.google/resolve?' + urllib.parse.urlencode({'name': host, 'type': 'A'})
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            data = json.loads(r.read(1024 * 1024))
    except Exception:
        raise ExportError('媒体 DNS 返回非公网地址，公共 HTTPS DNS 也不可用；请调整代理 DNS 后重试。') from None
    ips = sorted({x['data'] for x in data.get('Answer', []) if x.get('type') == 1})
    if data.get('Status') != 0 or not ips or not all(public_ip(x) for x in ips):
        raise ExportError('公共 DNS 没有全部有效的公网地址，已停止媒体下载。')
    return ips, 'https-dns'


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip):
        super().__init__(host, port=443, timeout=60, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        sock = socket.create_connection((self.ip, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def download_audio(url, folder):
    parsed = media_url(url)
    ips, dns_source = resolve_public(parsed.hostname)
    for ip in ips:
        partial = None
        conn = PinnedHTTPS(parsed.hostname, ip)
        created = False
        try:
            target = parsed.path + ('?' + parsed.query if parsed.query else '')
            conn.request('GET', target, headers={'Accept': 'audio/*,video/*,application/octet-stream'})
            r = conn.getresponse()
            if 300 <= r.status < 400:
                raise ExportError('媒体服务器返回重定向，未请求新地址。')
            if r.status != 200:
                raise ExportError(f'媒体服务器返回 HTTP {r.status}。')
            mime = r.getheader('Content-Type', '').split(';')[0].lower()
            if not (mime.startswith(('audio/', 'video/')) or mime == 'application/octet-stream'):
                raise ExportError('媒体服务器返回非音视频内容。')
            expected = r.getheader('Content-Length')
            fd, temporary_name = tempfile.mkstemp(prefix='.audio-', suffix='.part', dir=folder)
            partial = Path(temporary_name)
            created = True
            total, first = 0, b''
            with os.fdopen(fd, 'wb') as f:
                while chunk := r.read(1024 * 1024):
                    first = first or chunk[:32]
                    total += len(chunk)
                    if total > 2 * 1024 ** 3:
                        raise ExportError('媒体文件超过 2 GB 上限。')
                    f.write(chunk)
            if not total or (expected and total != int(expected)):
                raise ExportError('媒体文件为空或下载不完整。')
            if first.startswith(b'OggS'):
                ext = '.ogg'
            elif len(first) >= 12 and first[4:8] == b'ftyp':
                ext = '.mp4' if mime.startswith('video/') else '.m4a'
            elif first.startswith(b'RIFF') and first[8:12] == b'WAVE':
                ext = '.wav'
            elif first.startswith(b'ID3') or first[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'):
                ext = '.mp3'
            else:
                raise ExportError('下载内容没有可识别的音视频文件头。')
            output = folder / ('audio' + ext)
            if output.exists():
                if output.is_symlink() or file_hash(output) != file_hash(partial):
                    raise ExportError('已存在不同的音频目标，未覆盖。')
                partial.unlink()  # Same bytes as the fresh authenticated download.
            else:
                partial.replace(output)
            return {'file': str(output), 'bytes': total, 'mime': mime, 'dns': dns_source,
                    'sha256': file_hash(output)}
        except ExportError:
            if created and partial and partial.exists():
                partial.unlink()
            raise
        except (OSError, http.client.HTTPException):
            if created and partial and partial.exists():
                partial.unlink()
        finally:
            conn.close()
    raise ExportError('媒体公网连接失败，请检查本机代理或网络后重试。')


def save(path, content):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.part', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def fetch_doc(cli, token, folder, stem):
    response = cli.call(['docs', '+fetch', '--doc', token, '--doc-format', 'markdown', '--detail', 'simple'])
    content = unwrap(response).get('document', {}).get('content', '')
    if not content.strip():
        raise ExportError('文档返回空正文。')
    save(folder / (stem + '.response.json'), json.dumps(response, ensure_ascii=False, indent=2))
    save(folder / (stem + '.md'), content)
    return content


def local_summary(title, transcript):
    """Local extractive summary: representative quotations, not invented decisions."""
    clean = re.sub(r'<[^>]+>', ' ', transcript)
    clean = re.sub(r'!\[[^]]*\]\([^)]*\)', '', clean)
    candidates = []
    for line in clean.splitlines():
        if re.match(r'^\s*(Title|Time|Keywords):', line):
            continue
        if re.match(r'^\s*\d{4}-\d{2}-\d{2}.*\|\d+s\s*$', line):
            continue
        for sentence in re.split(r'(?<=[。！？])', line):
            sentence = re.sub(r'^[#>* -]+', '', sentence).strip()
            if 25 <= len(sentence) <= 350:
                candidates.append(sentence)
    if not candidates:
        raise ExportError('逐字稿没有足够的发言正文，不能生成概要。')
    stop = {'这个', '那个', '就是', '然后', '我们', '你们', '他们', '可以', '一个',
            '所以', '因为', '现在', '的话', '对吧', '什么', '是的', '一下', '不是'}
    def words(s):
        return [p[i:i+2] for p in re.findall(r'[\u4e00-\u9fff]+', s)
                for i in range(len(p)-1) if p[i:i+2] not in stop]
    freq = Counter(w for s in candidates for w in set(words(s)))
    size = max(1, math.ceil(len(candidates)/8))
    chosen, seen = [], set()
    for start in range(0, len(candidates), size):
        ranked = sorted(enumerate(candidates[start:start+size]),
                        key=lambda x: sum(math.log1p(freq[w]) for w in set(words(x[1]))) / math.sqrt(len(x[1])),
                        reverse=True)
        for offset, sentence in ranked:
            if sentence not in seen:
                chosen.append((start+offset, sentence))
                seen.add(sentence)
                break
    chosen.sort()
    return (f'# {title} — 本地提取式概要\n\n'
            '以下按对话顺序选取逐字稿中的代表性原句，供快速浏览。'
            '没有使用额外 AI 服务；不是人工确认的会议决策或行动清单。\n\n'
            + '\n'.join('- '+s for _, s in chosen) + '\n')


def export_note(cli, note_id, folder, state):
    raw = unwrap(cli.call(['note', '+detail', '--note-id', note_id, '--format', 'json']))
    note = raw.get('note', raw)
    state.update(note_id=note_id, note_display_type=note.get('note_display_type', 'unknown'))
    if note.get('note_doc_token'):
        state['ai_notes_doc_token'] = note['note_doc_token']
        fetch_doc(cli, note['note_doc_token'], folder, 'ai_notes')
        state['ai_notes'] = str(folder / 'ai_notes.md')
    kind = note.get('note_display_type', 'unknown')
    if kind in ('normal', 'unknown') and note.get('verbatim_doc_token'):
        text = fetch_doc(cli, note['verbatim_doc_token'], folder, 'transcript')
        state.update(transcript=str(folder / 'transcript.md'),
                     transcript_source='AI Notes 关联逐字稿文档')
        return text
    if kind == 'unified':
        if cli.identity != 'user':
            raise ExportError('unified 逐字稿只支持用户身份，脚本没有切换身份。')
        data = unwrap(cli.call(['note', '+transcript', '--note-id', note_id, '--format', 'json']))
        file = data.get('saved_path') or data.get('transcript_file')
        if not file:
            raise ExportError('CLI 未返回 unified 逐字稿文件路径。')
        source = Path(file)
        if not source.is_absolute():
            source = cli.cwd / source
        text = source.read_text(encoding='utf-8')
        save(folder / 'transcript.md', text)
        state.update(transcript=str(folder / 'transcript.md'), transcript_source='unified Note transcript')
        return text
    return ''


def run(args):
    app_id, secret = credentials(args.credentials)
    cli = prepare_cli(args, app_id, secret)
    state = {'identity': cli.identity, 'date': args.date,
             'exported_at': datetime.now().astimezone().isoformat(timespec='seconds'), 'errors': []}
    if args.doc_url:
        folder = args.output / ('ai-notes-' + hashlib.sha256(args.doc_url.encode()).hexdigest()[:10])
        folder.mkdir(parents=True, exist_ok=True)
        fetch_doc(cli, args.doc_url, folder, 'ai_notes')
        state['ai_notes'] = str(folder / 'ai_notes.md')
    else:
        token, note_id, title, info = args.minute_token, args.note_id, 'AI notes', ''
        if not note_id:
            if not token:
                item = find_recording(cli, args.date, args.min_duration)
                token, info = item['token'], item.get('display_info', '')
            raw = unwrap(cli.call(['minutes', '+detail', '--minute-tokens', token, '--format', 'json']))
            rows = raw.get('minutes', [])
            if not rows:
                raise ExportError('录音详情未返回目标数据。')
            title, note_id = rows[0].get('title', '录音'), rows[0].get('note_id')
        name = re.sub(r'[^\w\u4e00-\u9fff.-]+', '_', title)[:70]
        folder = args.output / f'{args.date}-{name}-{(token or note_id)[-6:]}'
        folder.mkdir(parents=True, exist_ok=True)
        state.update(title=title, duration_seconds=duration_seconds(info),
                     start_time=start_time(info), minute_token=token)
        transcript = ''
        if note_id:
            try:
                transcript = export_note(cli, note_id, folder, state)
            except ExportError as e:
                state['errors'].append('AI notes/逐字稿：' + str(e))
        if not transcript and token:
            try:
                raw = unwrap(cli.call(['minutes', '+detail', '--minute-tokens', token,
                                       '--transcript', '--output-dir', 'downloads/cli-transcripts', '--format', 'json']))
                rows = raw.get('minutes', [])
                file = rows[0].get('artifacts', {}).get('transcript_file') if rows else None
                if not file:
                    raise ExportError('CLI 未返回逐字稿文件。')
                source = Path(file)
                if not source.is_absolute():
                    source = cli.cwd / source
                transcript = source.read_text(encoding='utf-8')
                body = '\n'.join(l for l in transcript.splitlines()
                                 if l.strip() and not l.startswith('Keywords:')
                                 and not re.match(r'^\d{4}-\d{2}-\d{2}.*\|\d+s$', l))
                if len(body.strip()) < 25:
                    transcript = ''
                    raise ExportError('转写只有元数据，没有对话正文。')
                save(folder / 'transcript.md', transcript)
                state.update(transcript=str(folder / 'transcript.md'), transcript_source='Minutes transcript')
            except ExportError as e:
                state['errors'].append('逐字稿：' + str(e))
        if transcript:
            state['transcript_chars'] = len(transcript)
            try:
                save(folder / 'summary.md', local_summary(title, transcript))
                state.update(summary=str(folder / 'summary.md'), summary_type='local-extractive')
            except ExportError as e:
                state['errors'].append('概要：' + str(e))
        if token and not args.no_audio:
            try:
                old = folder / 'manifest.json'
                try:
                    previous = json.loads(old.read_text()) if old.exists() else {}
                    if not isinstance(previous, dict):
                        previous = {}
                except (ValueError, OSError):
                    previous = {}
                audio = previous.get('audio', {})
                file = Path(audio.get('file', ''))
                if (previous.get('minute_token') == token and file.is_file()
                        and file.parent.resolve() == folder.resolve()
                        and audio.get('sha256') == file_hash(file)):
                    state['audio'] = audio
                else:
                    raw = unwrap(cli.call(['minutes', '+download', '--minute-tokens', token,
                                           '--url-only', '--format', 'json']))
                    if not raw.get('download_url'):
                        raise ExportError('没有取得媒体下载链接。')
                    state['audio'] = download_audio(raw['download_url'], folder)
            except (ExportError, ValueError) as e:
                state['errors'].append('音频：' + str(e))
    save(folder / 'manifest.json', json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    public = {k: v for k, v in state.items() if k not in ('minute_token', 'note_id', 'ai_notes_doc_token')}
    public['output_dir'] = str(folder)
    print(json.dumps(public, ensure_ascii=False, indent=2))
    return 2 if state['errors'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--credentials', type=Path, default=DEFAULT_CREDENTIALS,
                        help='App ID/Secret 文件；支持现有 tmp.md 或 JSON')
    parser.add_argument('--identity', choices=['user', 'bot'], default='user')
    parser.add_argument('--profile', help='现有 CLI profile，不改变默认 profile')
    parser.add_argument('--date', default=date.today().isoformat(), help='录音日期，默认今天')
    parser.add_argument('--min-duration', type=int, default=0, help='最低录音秒数，例如 3600')
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--minute-token', help='指定录音，跳过搜索')
    target.add_argument('--note-id', help='直接读取 AI notes，跳过录音搜索')
    target.add_argument('--doc-url', help='直接读取 AI notes Docx 链接')
    parser.add_argument('--output', type=Path, default=ROOT / 'downloads')
    parser.add_argument('--no-audio', action='store_true')
    args = parser.parse_args()
    try:
        date.fromisoformat(args.date)
        if args.min_duration < 0:
            raise ValueError()
    except ValueError:
        parser.error('日期必须是 YYYY-MM-DD，时长必须是非负秒数。')
    args.output = args.output.expanduser().resolve()
    args.credentials = args.credentials.expanduser().resolve()
    try:
        return run(args)
    except ExportError as e:
        print('未完成：' + str(e), file=sys.stderr)
        return 1
    except OSError:
        print('未完成：本地文件读取或保存失败，请检查输出目录和凭据文件权限。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
