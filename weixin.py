from __future__ import annotations

import asyncio, base64, hashlib, json, logging, os, secrets, struct, uuid
import io
from pathlib import Path
from urllib.parse import quote
from urllib.parse import unquote
import aiohttp
import sqlite3

BASE = os.getenv('WEIXIN_BASE_URL', 'https://ilinkai.weixin.qq.com').rstrip('/')
DATA = Path(os.getenv('DB_PATH', '/app/data/bot.db')).parent
TOKEN_FILE = DATA / 'weixin_token.json'
QR_FILE = DATA / 'weixin_qr.json'
CODEX_URL = os.getenv('CODEX_BRIDGE_URL', 'http://host.docker.internal:8787/v1/chat')
CODEX_STATUS_URL = os.getenv('CODEX_STATUS_URL', CODEX_URL.rsplit('/', 1)[0] + '/status')
CODEX_TOKEN = os.getenv('CODEX_BRIDGE_TOKEN', '')
PROGRESS_INTERVAL = max(10, int(os.getenv('PROGRESS_INTERVAL_SECONDS', '30')))
LOG = logging.getLogger('dingtalk-codex-bot.weixin')
DB = DATA / 'bot.db'
CODEX_CWD = Path(os.getenv('CODEX_CWD', str(Path.cwd()))).resolve()
FILES_DIR = DATA / 'wechat_files'
MEDIA_KEY_CACHE = DATA / 'wechat_media_keys.json'
MAX_FILE_BYTES = 50 * 1024 * 1024

def record(session_id: str, role: str, content: str) -> None:
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, platform TEXT NOT NULL DEFAULT 'dingtalk')")
    columns = {row[1] for row in conn.execute('PRAGMA table_info(messages)')}
    if 'platform' not in columns: conn.execute("ALTER TABLE messages ADD COLUMN platform TEXT NOT NULL DEFAULT 'dingtalk'")
    conn.execute('INSERT INTO messages(session_id, role, content, platform) VALUES (?, ?, ?, ?)', (session_id, role, content, 'wechat'))
    conn.commit(); conn.close()

def uin():
    return base64.b64encode(str(struct.unpack('>I', os.urandom(4))[0]).encode()).decode()

def qr_data_url(url: str) -> str:
    import qrcode
    image = qrcode.make(url)
    buf = io.BytesIO(); image.save(buf, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()

class WeixinClient:
    def __init__(self, token='', base_url=BASE): self.token, self.base_url, self.s = token, base_url.rstrip('/'), aiohttp.ClientSession()
    def headers(self):
        h={'Content-Type':'application/json','AuthorizationType':'ilink_bot_token','X-WECHAT-UIN':uin()}
        if self.token: h['Authorization']=f'Bearer {self.token}'
        return h
    async def close(self): await self.s.close()
    async def qrcode(self):
        async with self.s.get(f'{self.base_url}/ilink/bot/get_bot_qrcode?bot_type=3') as r:
            return json.loads(await r.text())
    async def qr_status(self, code):
        h={'iLink-App-ClientVersion':'1'}
        try:
            async with self.s.get(f'{self.base_url}/ilink/bot/get_qrcode_status?qrcode={quote(code,safe="")}',headers=h,timeout=35) as r: return json.loads(await r.text())
        except asyncio.TimeoutError: return {'status':'wait'}
    async def updates(self, buf):
        p={'get_updates_buf':buf,'base_info':{'channel_version':'1.0.0'}}
        try:
            async with self.s.post(f'{self.base_url}/ilink/bot/getupdates',json=p,headers=self.headers(),timeout=45) as r:
                body = json.loads(await r.text())
                if r.status >= 400:
                    raise RuntimeError(f'getupdates failed: HTTP {r.status}')
                return body
        except asyncio.TimeoutError: return {'ret':0,'msgs':[],'get_updates_buf':buf}
    async def send(self,to,text,context=''):
        for i in range(0,len(text),2000):
            p={'msg':{'from_user_id':'','to_user_id':to,'client_id':f'bot-{uuid.uuid4().hex[:12]}','message_type':2,'message_state':2,'item_list':[{'type':1,'text_item':{'text':text[i:i+2000]}}],'context_token':context or None},'base_info':{'channel_version':'1.0.0'}}
            async with self.s.post(f'{self.base_url}/ilink/bot/sendmessage',json=p,headers=self.headers()) as r:
                body = await r.text()
                if r.status >= 400: raise RuntimeError(body)
                if body:
                    result = json.loads(body)
                    error = result.get('errcode') or result.get('ret')
                    if error: raise RuntimeError(result.get('errmsg') or f'sendmessage error {error}')

    async def _post_json(self, endpoint, payload):
        payload['base_info'] = {'channel_version': '1.0.0'}
        async with self.s.post(f'{self.base_url}/{endpoint}', json=payload, headers=self.headers()) as r:
            body = json.loads(await r.text())
            if r.status >= 400 or body.get('errcode') or body.get('ret'):
                raise RuntimeError(body.get('errmsg') or body.get('errcode') or body.get('ret') or f'HTTP {r.status}')
            return body

    @staticmethod
    def _decode_aes_key(aes_key):
        key_text = str(aes_key or '').strip()
        if not key_text:
            raise ValueError('empty key')
        raw = base64.b64decode(key_text + ('=' * (-len(key_text) % 4)), validate=True)
        if len(raw) == 16:
            return raw
        if len(raw) == 32:
            decoded = raw.decode('ascii')
            if all(ch in '0123456789abcdefABCDEF' for ch in decoded):
                return bytes.fromhex(decoded)
        raise ValueError(f'unsupported decoded key length {len(raw)}')

    @staticmethod
    def _decrypt(encrypted, aes_keys, expected_md5='', expected_size=0):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        key_texts = list(dict.fromkeys(str(value).strip() for value in aes_keys if value))
        if not key_texts:
            raise RuntimeError('微信文件缺少 AES 密钥')
        for key_text in key_texts:
            try:
                key = WeixinClient._decode_aes_key(key_text)
                decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
                padded = decryptor.update(encrypted) + decryptor.finalize()
                unpadder = PKCS7(128).unpadder()
                plain = unpadder.update(padded) + unpadder.finalize()
            except (ValueError, TypeError, UnicodeDecodeError):
                continue
            if expected_size and len(plain) != expected_size:
                continue
            if expected_md5 and hashlib.md5(plain).hexdigest().lower() != expected_md5.lower():
                continue
            return plain, key_text
        raise RuntimeError('WECHAT_CDN_KEY_MISMATCH')

    @staticmethod
    def _load_media_key_cache():
        try:
            value = json.loads(MEDIA_KEY_CACHE.read_text(encoding='utf-8')) if MEDIA_KEY_CACHE.exists() else {}
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            LOG.warning('Unable to read WeChat media key cache')
            return {}

    @staticmethod
    def _save_media_key_cache(cache):
        try:
            MEDIA_KEY_CACHE.write_text(json.dumps(dict(list(cache.items())[-4096:]), ensure_ascii=False), encoding='utf-8')
        except OSError:
            LOG.warning('Unable to persist WeChat media key cache')

    @staticmethod
    def _encrypt(data):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        key = os.urandom(16); key_hex = key.hex()
        padder = PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        return key_hex, base64.b64encode(key_hex.encode()).decode(), encrypted, hashlib.md5(data).hexdigest()

    async def download_file(self, media, target, fallback_aes_key='', expected_md5='', expected_size=0):
        query = media.get('encrypt_query_param'); aes_key = media.get('aes_key') or fallback_aes_key
        if not query or not aes_key: raise RuntimeError('微信文件缺少 CDN 加密参数')
        full_url = str(media.get('full_url') or '').strip()
        url = full_url or f'https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param={quote(query, safe="")}'
        LOG.info('WeChat CDN URL source: full_url=%s url_len=%d', bool(full_url), len(url))
        async with self.s.get(url) as r:
            if r.status >= 400: raise RuntimeError(f'微信文件下载失败 HTTP {r.status}')
            encrypted = await r.read()
            encrypted_hash = hashlib.sha256(encrypted).hexdigest()
            LOG.info('Downloaded WeChat CDN payload: bytes=%d content_type=%s cipher_sha256=%s aes_key_len=%d', len(encrypted), r.headers.get('Content-Type', ''), encrypted_hash[:12], len(str(aes_key)))
        if len(encrypted) > MAX_FILE_BYTES * 2: raise RuntimeError('文件超过 50 MB 限制')
        cache = self._load_media_key_cache()
        cached_key = cache.get(encrypted_hash, '')
        plain, working_key = self._decrypt(encrypted, (cached_key, aes_key, fallback_aes_key), expected_md5, expected_size)
        target.write_bytes(plain)
        if cache.get(encrypted_hash) != working_key:
            cache[encrypted_hash] = working_key
            self._save_media_key_cache(cache)

    async def send_file(self, to, path, context=''):
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES: raise RuntimeError('文件超过 50 MB 限制')
        key_hex, encoded_key, encrypted, md5 = self._encrypt(data)
        filekey = os.urandom(16).hex()
        upload = await self._post_json('ilink/bot/getuploadurl', {'filekey': filekey, 'media_type': 3, 'to_user_id': to, 'rawsize': len(data), 'rawfilemd5': md5, 'filesize': len(encrypted), 'no_need_thumb': True, 'aeskey': key_hex})
        param = upload.get('upload_param')
        if not param: raise RuntimeError('微信未返回文件上传参数')
        url = f'https://novac2c.cdn.weixin.qq.com/c2c/upload?encrypted_query_param={quote(param, safe="")}&filekey={quote(filekey, safe="")}'
        async with self.s.post(url, data=encrypted, headers={'Content-Type': 'application/octet-stream'}) as r:
            if r.status >= 400: raise RuntimeError(f'微信文件上传失败 HTTP {r.status}')
            download_param = r.headers.get('x-encrypted-param', '')
        if not download_param: raise RuntimeError('微信上传成功但未返回下载参数')
        item = {'type': 4, 'file_item': {'media': {'encrypt_query_param': download_param, 'aes_key': encoded_key, 'encrypt_type': 1}, 'file_name': path.name, 'md5': md5, 'len': str(len(data))}}
        await self._post_json('ilink/bot/sendmessage', {'msg': {'from_user_id': '', 'to_user_id': to, 'client_id': f'bot-{uuid.uuid4().hex[:12]}', 'message_type': 2, 'message_state': 2, 'item_list': [item], 'context_token': context or None}})

async def login():
    c=WeixinClient()
    try:
        while True:
            try:
                q=await c.qrcode()
            except Exception as exc:
                LOG.warning('Unable to fetch WeChat QR code: %s', exc)
                await asyncio.sleep(5)
                continue
            code=q.get('qrcode'); url=q.get('qrcode_img_content')
            QR_FILE.write_text(json.dumps({'status':'waiting','qr_url':url,'qr_data':qr_data_url(url)},ensure_ascii=False))
            while True:
                st=await c.qr_status(code); QR_FILE.write_text(json.dumps({'status':st.get('status','wait'),'qr_url':url},ensure_ascii=False))
                if st.get('status')=='confirmed' and st.get('bot_token'):
                    TOKEN_FILE.write_text(json.dumps({'token':st['bot_token'],'base_url':st.get('baseurl') or BASE,'account_id':st.get('ilink_bot_id','')})); return WeixinClient(st['bot_token'],st.get('baseurl') or BASE)
                if st.get('status')=='expired': break
                await asyncio.sleep(1)
    finally:
        await c.close()

async def main():
    logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'))
    while True:
        cfg=json.loads((DATA/'runtime.json').read_text()) if (DATA/'runtime.json').exists() else {}
        if cfg.get('channel','dingtalk')=='wechat': break
        LOG.info('WeChat adapter disabled; waiting for channel selection')
        await asyncio.sleep(3)
    if TOKEN_FILE.exists():
        saved=json.loads(TOKEN_FILE.read_text()); c=WeixinClient(saved['token'],saved.get('base_url',BASE))
    else:
        LOG.info('Starting QR login; open admin page for QR URL'); c=await login()
    buf=''
    try:
        while True:
            try:
                data=await c.updates(buf); buf=data.get('get_updates_buf') or buf
                if data.get('errcode')==-14 or data.get('ret')==-14: c=await login(); buf=''; continue
                messages = data.get('msgs') or []
                if messages: LOG.info('Received %d WeChat update(s)', len(messages))
                for msg in messages:
                    if msg.get('message_type')!=1: continue
                    items = msg.get('item_list') or []
                    text_parts = []
                    has_voice = False
                    has_untranscribed_voice = False
                    media_types = set()
                    received_files = []
                    for item in items:
                        item_type = item.get('type')
                        if item_type == 1:
                            value = (item.get('text_item') or {}).get('text', '')
                            if value:
                                text_parts.append(value)
                        elif item_type == 3:
                            has_voice = True
                            voice_text = (item.get('voice_item') or {}).get('text', '')
                            if voice_text:
                                text_parts.append(voice_text)
                            else:
                                has_untranscribed_voice = True
                        elif item_type in {2, 4, 5}:
                            media_types.add({2: 'image', 4: 'file', 5: 'video'}[item_type])
                            if item_type == 4:
                                received_files.append(item.get('file_item') or {})
                    text = ''.join(text_parts).strip()
                    if has_voice:
                        LOG.info('Received WeChat voice message (transcript=%s)', bool(text))
                    sid = msg.get('from_user_id') or msg.get('session_id') or 'wechat'
                    context = msg.get('context_token','')
                    downloaded_files = []
                    file_failures = []
                    if received_files:
                        session_dir = FILES_DIR / hashlib.sha256(sid.encode()).hexdigest()[:16]
                        session_dir.mkdir(parents=True, exist_ok=True)
                        for file_item in received_files:
                            name = Path(file_item.get('file_name') or f'wechat-{uuid.uuid4().hex[:8]}.bin').name
                            target = session_dir / name
                            try:
                                expected_size = int(file_item.get('len') or 0)
                                media = file_item.get('media') or {}
                                LOG.info('WeChat file field names: item=%s media=%s; encrypt_type=%s query_len=%d full_url_len=%d media_key_len=%d item_key_len=%d expected_size=%d md5_len=%d', sorted(file_item.keys()), sorted(media.keys()), media.get('encrypt_type'), len(str(media.get('encrypt_query_param') or '')), len(str(media.get('full_url') or '')), len(str(media.get('aes_key') or '')), len(str(file_item.get('aeskey') or '')), expected_size, len(str(file_item.get('md5') or '')))
                                await c.download_file(
                                    media,
                                    target,
                                    file_item.get('aeskey', ''),
                                    file_item.get('md5', ''),
                                    expected_size,
                                )
                                downloaded_files.append((name, target))
                            except RuntimeError as exc:
                                if str(exc) == 'WECHAT_CDN_KEY_MISMATCH':
                                    file_failures.append('key_mismatch')
                                    LOG.warning('WeChat CDN key mismatch for file %s (known server-side deduplication issue)', name)
                                else:
                                    file_failures.append('download')
                                    LOG.exception('Failed to download WeChat file')
                            except Exception:
                                file_failures.append('download')
                                LOG.exception('Failed to download WeChat file')
                        if downloaded_files:
                            file_context = '\n'.join(
                                f'收到的文件：{name}，本地路径：{path.relative_to(DATA.parent)}'
                                for name, path in downloaded_files
                            )
                            text = f'{text}\n\n{file_context}'.strip() if text else file_context
                            LOG.info('Downloaded %d WeChat file(s) for Codex', len(downloaded_files))
                        else:
                            if file_failures and all(failure == 'key_mismatch' for failure in file_failures):
                                await c.send(sid, '文件已收到，但微信 CDN 返回的解密密钥与附件不匹配。这是微信文件去重的已知问题。请改变文件内容后重发，例如压缩成 ZIP 并在压缩包内加入一个新的说明.txt；只改文件名无效。', context)
                            else:
                                await c.send(sid, '已收到文件，但下载失败，请稍后重试。', context)
                            continue
                    if not text:
                        if has_untranscribed_voice:
                            await c.send(sid, '已收到语音，但当前微信接口未提供语音转写文本，暂时无法识别。请改发文字，或稍后重试。', context)
                            LOG.info('Sent WeChat voice transcription unavailable notice')
                        else:
                            LOG.info('Ignored unsupported non-text WeChat message')
                        if media_types:
                            labels = {'image': '图片', 'file': '文件', 'video': '视频'}
                            kinds = '、'.join(labels[k] for k in sorted(media_types))
                            await c.send(sid, f'已收到{kinds}消息。目前此版本支持文本、语音转写和文件处理，图片/视频暂未接入完整解析。请改发文字说明。', context)
                            LOG.info('Sent WeChat media capability notice: %s', ','.join(sorted(media_types)))
                        continue
                    lowered = text.strip()
                    if lowered.startswith('发送文件') or lowered.lower().startswith('send file'):
                        raw_path = lowered.split(':', 1)[1].strip() if ':' in lowered else lowered.split(None, 1)[1].strip() if len(lowered.split(None, 1)) > 1 else ''
                        candidate = (CODEX_CWD / raw_path).resolve() if not os.path.isabs(raw_path) else Path(raw_path).resolve()
                        try:
                            candidate.relative_to(CODEX_CWD)
                            if not candidate.is_file(): raise RuntimeError('文件不存在')
                            await c.send(sid, f'正在发送文件：{candidate.name}', context)
                            await c.send_file(sid, candidate, context)
                            record(f'wechat:{sid}', 'assistant', f'[文件已发送] {candidate.name}')
                        except Exception as exc:
                            await c.send(sid, f'发送文件失败：{exc}', context)
                        continue
                    LOG.info('Forwarding WeChat message to local Codex')
                    record(f'wechat:{sid}', 'user', text)
                    await c.send(sid, '任务已收到，正在调用本地 Codex。', context)

                    async def request_codex():
                        async with aiohttp.ClientSession() as s:
                            async with s.post(CODEX_URL,headers={'Authorization':f'Bearer {CODEX_TOKEN}'},json={'session_id':f'wechat:{sid}','prompt':text}) as r:
                                body=await r.json(content_type=None)
                                if r.status >= 400: raise RuntimeError(body.get('error') or f'Codex bridge HTTP {r.status}')
                                return body

                    task = asyncio.create_task(request_codex())
                    started = asyncio.get_running_loop().time()
                    while True:
                        done, _ = await asyncio.wait({task}, timeout=PROGRESS_INTERVAL)
                        if task in done:
                            try:
                                body = await task
                            except Exception:
                                error_text = str(task.exception() or '') if task.done() else ''
                                if '上游模型服务返回 502' in error_text:
                                    await c.send(sid, '任务已送达，但本地 Codex 当前连接的上游模型服务返回 502，暂时无法生成回复。请稍后重试，或检查 ~/.codex/config.toml 的自定义 Provider 地址。', context)
                                else:
                                    await c.send(sid, '任务处理失败，请检查本地 Codex Bridge 状态后重试。', context)
                                raise
                            break
                        elapsed = int(asyncio.get_running_loop().time() - started)
                        detail = '正在处理请求'
                        try:
                            async with aiohttp.ClientSession() as status_session:
                                async with status_session.get(CODEX_STATUS_URL, params={'session_id': f'wechat:{sid}'}, headers={'Authorization': f'Bearer {CODEX_TOKEN}'}, timeout=5) as status_response:
                                    if status_response.status < 400:
                                        status = await status_response.json(content_type=None)
                                        detail = status.get('detail') or detail
                        except Exception:
                            pass
                        await c.send(sid, f'任务仍在处理中，已用时 {elapsed} 秒，当前状态：{detail}。', context)
                    answer = body.get('answer') or '本地 Codex 暂时不可用。'
                    await c.send(sid, answer, context)
                    record(f'wechat:{sid}', 'assistant', answer)
                    LOG.info('Sent local Codex reply to WeChat')
            except Exception:
                LOG.exception('WeChat polling or message processing failed')
                await asyncio.sleep(2)
    finally: await c.close()

if __name__=='__main__': asyncio.run(main())
