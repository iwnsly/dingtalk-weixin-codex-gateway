from __future__ import annotations

import asyncio, base64, json, logging, os, secrets, struct, uuid
import io
from pathlib import Path
from urllib.parse import quote
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
                        if media_types:
                            sid = msg.get('from_user_id') or msg.get('session_id') or 'wechat'
                            labels = {'image': '图片', 'file': '文件', 'video': '视频'}
                            kinds = '、'.join(labels[k] for k in sorted(media_types))
                            await c.send(sid, f'已收到{kinds}消息。目前此版本支持文本和语音转写，暂未接入媒体下载、解析和回传。请改发文字说明。', msg.get('context_token',''))
                            LOG.info('Sent WeChat media capability notice: %s', ','.join(sorted(media_types)))
                        continue
                    sid=msg.get('from_user_id') or msg.get('session_id') or 'wechat'
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
