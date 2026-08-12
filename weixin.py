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
MAX_FILE_BYTES = 10 * 1024 * 1024

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
    def _decrypt(encrypted, aes_key):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        raw = base64.b64decode(aes_key)
        key = bytes.fromhex(raw.decode()) if len(raw) == 32 else raw
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = decryptor.update(encrypted) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()

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

    async def download_file(self, media, target):
        query = media.get('encrypt_query_param'); aes_key = media.get('aes_key')
        if not query or not aes_key: raise RuntimeError('微信文件缺少 CDN 加密参数')
        async with self.s.get(f'https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param={quote(query, safe="")}') as r:
            if r.status >= 400: raise RuntimeError(f'微信文件下载失败 HTTP {r.status}')
            encrypted = await r.read()
        if len(encrypted) > MAX_FILE_BYTES * 2: raise RuntimeError('文件超过 10 MB 限制')
        target.write_bytes(self._decrypt(encrypted, aes_key))

    async def send_file(self, to, path, context=''):
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES: raise RuntimeError('文件超过 10 MB 限制')
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
                    if not text:
                        if has_untranscribed_voice:
                            sid = msg.get('from_user_id') or msg.get('session_id') or 'wechat'
                            await c.send(sid, '已收到语音，但当前微信接口未提供语音转写文本，暂时无法识别。请改发文字，或稍后重试。', msg.get('context_token',''))
                            LOG.info('Sent WeChat voice transcription unavailable notice')
                        else:
                            LOG.info('Ignored unsupported non-text WeChat message')
                        if received_files:
                            sid = msg.get('from_user_id') or msg.get('session_id') or 'wechat'
                            session_dir = FILES_DIR / hashlib.sha256(sid.encode()).hexdigest()[:16]
                            session_dir.mkdir(parents=True, exist_ok=True)
                            downloaded = []
                            for file_item in received_files:
                                name = Path(file_item.get('file_name') or f'wechat-{uuid.uuid4().hex[:8]}.bin').name
                                target = session_dir / name
                                try:
                                    await c.download_file(file_item.get('media') or {}, target)
                                    downloaded.append((name, target))
                                except Exception:
                                    LOG.exception('Failed to download WeChat file')
                            if downloaded:
                                paths = '\n'.join(f'- {name}: {path.relative_to(DATA.parent)}' for name, path in downloaded)
                                await c.send(sid, f'文件已接收并保存到本地：\n{paths}\n\n你可以继续说明要如何处理这些文件。', msg.get('context_token',''))
                                record(f'wechat:{sid}', 'user', '[文件] ' + ', '.join(name for name, _ in downloaded))
                            else:
                                await c.send(sid, '已收到文件，但下载失败，请重新发送。', msg.get('context_token',''))
                        elif media_types:
                            sid = msg.get('from_user_id') or msg.get('session_id') or 'wechat'
                            labels = {'image': '图片', 'file': '文件', 'video': '视频'}
                            kinds = '、'.join(labels[k] for k in sorted(media_types))
                            await c.send(sid, f'已收到{kinds}消息。目前此版本支持文本和语音转写，暂未接入媒体下载、解析和回传。请改发文字说明。', msg.get('context_token',''))
                            LOG.info('Sent WeChat media capability notice: %s', ','.join(sorted(media_types)))
                        continue
                    sid=msg.get('from_user_id') or msg.get('session_id') or 'wechat'
                    lowered = text.strip()
                    if lowered.startswith('发送文件') or lowered.lower().startswith('send file'):
                        raw_path = lowered.split(':', 1)[1].strip() if ':' in lowered else lowered.split(None, 1)[1].strip() if len(lowered.split(None, 1)) > 1 else ''
                        candidate = (CODEX_CWD / raw_path).resolve() if not os.path.isabs(raw_path) else Path(raw_path).resolve()
                        try:
                            candidate.relative_to(CODEX_CWD)
                            if not candidate.is_file(): raise RuntimeError('文件不存在')
                            await c.send(sid, f'正在发送文件：{candidate.name}', msg.get('context_token',''))
                            await c.send_file(sid, candidate, msg.get('context_token',''))
                            record(f'wechat:{sid}', 'assistant', f'[文件已发送] {candidate.name}')
                        except Exception as exc:
                            await c.send(sid, f'发送文件失败：{exc}', msg.get('context_token',''))
                        continue
                    LOG.info('Forwarding WeChat message to local Codex')
                    record(f'wechat:{sid}', 'user', text)
                    context = msg.get('context_token','')
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
