from __future__ import annotations

import html
import json
import os
import secrets
import sqlite3
from datetime import datetime, time, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST = os.getenv("ADMIN_HOST", "0.0.0.0")
PORT = int(os.getenv("ADMIN_PORT", "8080"))
DATA = Path(os.getenv("DB_PATH", "/app/data/bot.db")).parent
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/data/runtime.json"))
PASSWORD = os.getenv("ADMIN_PASSWORD", "12345")
SESSIONS: set[str] = set()
LOCAL_TZ = timezone(timedelta(hours=8))


def preset_range(name: str) -> tuple[str, str]:
    now = datetime.now(LOCAL_TZ)
    today = now.date()
    if name == "yesterday": start, end = today - timedelta(days=1), today - timedelta(days=1)
    elif name == "week": start, end = today - timedelta(days=today.weekday()), today
    elif name == "last_week":
        end = today - timedelta(days=today.weekday() + 1); start = end - timedelta(days=6)
    elif name == "month": start, end = today.replace(day=1), today
    elif name == "last_month":
        end = today.replace(day=1) - timedelta(days=1); start = end.replace(day=1)
    elif name == "year": start, end = today.replace(month=1, day=1), today
    else: start = end = today
    start_dt = datetime.combine(start, time.min, LOCAL_TZ).astimezone(timezone.utc).replace(tzinfo=None)
    end_dt = datetime.combine(end, time.max, LOCAL_TZ).astimezone(timezone.utc).replace(tzinfo=None)
    return start_dt.strftime("%Y-%m-%d %H:%M:%S"), end_dt.strftime("%Y-%m-%d %H:%M:%S")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"channel": "dingtalk", "dingtalk": {"client_id": os.getenv("DINGTALK_CLIENT_ID", ""), "client_secret": os.getenv("DINGTALK_CLIENT_SECRET", "")}, "admin_password": PASSWORD, "full_access": False}
    return json.loads(CONFIG_PATH.read_text())


def current_password() -> str:
    return str(load_config().get("admin_password") or PASSWORD)


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    tmp.replace(CONFIG_PATH)


def logged_in(handler: BaseHTTPRequestHandler) -> bool:
    cookie = SimpleCookie(handler.headers.get("Cookie", ""))
    sid = cookie.get("sid")
    return bool(sid and sid.value in SESSIONS)


def page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>{title}</title>
<style>:root{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#172033;background:#f4f7fb}}*{{box-sizing:border-box}}body{{margin:0}}.shell{{max-width:1180px;margin:0 auto;padding:32px 22px}}.top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}}.brand{{font-size:22px;font-weight:750}}.muted{{color:#667085}}.panel{{background:#fff;border:1px solid #e4e9f0;border-radius:14px;padding:22px;box-shadow:0 8px 28px #12233d0a;margin-bottom:18px}}.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}}label{{display:block;font-size:13px;font-weight:650;margin-bottom:7px;color:#344054}}input,select{{width:100%;border:1px solid #d0d5dd;border-radius:8px;padding:10px 11px;font:inherit;background:#fff}}button{{border:0;border-radius:8px;background:#1664d9;color:white;padding:10px 16px;font:inherit;font-weight:650;cursor:pointer}}button.secondary{{background:#eef4ff;color:#1555ad}}.tabs{{display:flex;gap:8px;margin-bottom:16px}}.tab{{padding:9px 13px;border-radius:8px;text-decoration:none;color:#475467;background:#f2f4f7}}.tab.active{{background:#1664d9;color:#fff}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{text-align:left;padding:12px 9px;border-bottom:1px solid #eef1f5;vertical-align:top}}th{{color:#667085;font-size:12px}}td.content{{white-space:pre-wrap;max-width:680px;word-break:break-word}}.badge{{display:inline-block;padding:4px 8px;border-radius:999px;font-size:12px;font-weight:650}}.wechat{{background:#e7f8ef;color:#16804b}}.dingtalk{{background:#e8f1ff;color:#1555ad}}.login{{max-width:390px;margin:12vh auto}}img.qr{{width:220px;border-radius:10px;border:1px solid #e4e9f0}}@media(max-width:760px){{.grid{{grid-template-columns:1fr}}.shell{{padding:20px 14px}}table{{font-size:12px}}}}
</style><div class=shell>{body}</div></html>"""


def login_page(error="") -> str:
    msg = f"<p style='color:#c0392b'>{html.escape(error)}</p>" if error else ""
    return page("登录 - IM 网关", f"<div class='panel login'><div class=brand>本地 Codex IM 网关</div><p class=muted>请输入管理密码</p>{msg}<form method=post action=/login><label>密码<input type=password name=password autofocus></label><button>登录</button></form></div>")


def dashboard(c: dict, view: str, channel: str, start: str, end: str) -> str:
    active_range = "custom"
    if view == "records" and not start and not end:
        start, end = preset_range("today"); active_range = "today"
    qr_path = DATA / "weixin_qr.json"
    qr = json.loads(qr_path.read_text()) if qr_path.exists() else {}
    qr_html = f"<p>状态：{html.escape(qr.get('status', '未启动'))}</p>"
    if qr.get("qr_data") and qr.get("status") in {"waiting", "scaned", "wait"}:
        qr_html += f"<img class=qr src='{html.escape(qr['qr_data'])}' alt='微信登录二维码'>"
    rows = []
    db = DATA / "bot.db"
    if db.exists():
        conn = sqlite3.connect(db)
        query = "SELECT created_at, platform, role, session_id, content FROM messages WHERE platform=?"
        args = [channel]
        if start: query += " AND created_at >= ?"; args.append(start.replace("T", " "))
        if end: query += " AND created_at <= ?"; args.append(end.replace("T", " "))
        query += " ORDER BY id DESC LIMIT 500"
        for created, platform, role, session_id, content in conn.execute(query, args):
            rows.append(f"<tr><td>{html.escape(created)}</td><td><span class='badge {platform}'>{'微信' if platform == 'wechat' else '钉钉'}</span></td><td>{'用户' if role == 'user' else '助手'}</td><td>{html.escape(session_id[:28])}</td><td class=content>{html.escape(content)}</td></tr>")
        conn.close()
    table = "".join(rows) or "<tr><td colspan=5 class=muted>当前筛选暂无记录</td></tr>"
    selected_d = "selected" if channel == "dingtalk" else ""; selected_w = "selected" if channel == "wechat" else ""
    body = f"<div class=top><div><div class=brand>本地 Codex IM 网关</div><div class=muted>渠道配置与对话审计</div></div><a class=tab href=/logout>退出</a></div>"
    body += f"<div class=tabs><a class='tab {'active' if view == 'config' else ''}' href='/?view=config'>配置</a><a class='tab {'active' if view == 'records' else ''}' href='/?view=records&channel={channel}'>聊天记录</a></div>"
    if view == "config":
        full_access = bool(c.get("full_access", False))
        checked = "checked" if full_access else ""
        access_panel = f"<div class=panel><h2>Codex 权限</h2><form method=post action=/permissions><label style='display:flex;gap:10px;align-items:center'><input type=checkbox name=full_access value=1 {checked} style='width:auto'>启用完全权限</label><p class=muted>关闭时使用只读沙箱。启用后 Codex 可修改文件、执行命令并访问宿主机资源，请仅在可信工作场景使用。</p><button class='secondary'>保存权限</button></form></div>"
        body += f"<div class=panel><h2>消息入口</h2><form method=post action=/config><div class=grid><div><label>当前渠道</label><select name=channel><option value=dingtalk {selected_d}>钉钉</option><option value=wechat {selected_w}>微信</option></select></div><div style='display:flex;align-items:end'><button>保存渠道</button></div></div><div class=grid style='margin-top:18px'><div><label>钉钉 Client ID</label><input name=client_id value='{html.escape(c.get('dingtalk', {}).get('client_id', ''))}'></div><div><label>钉钉 Client Secret</label><input type=password name=client_secret value='{html.escape(c.get('dingtalk', {}).get('client_secret', ''))}'></div></div></form></div>" + access_panel
        body += f"<div class=panel><h2>微信登录</h2>{qr_html}<p class=muted>选择微信并保存后，系统自动生成二维码。扫码成功后凭据保存在本机数据目录。</p></div>"
        body += "<div class=panel><h2>管理密码</h2><form method=post action=/password><div class=grid><div><label>新密码</label><input type=password name=password minlength=5 required></div><div style='display:flex;align-items:end'><button>修改密码</button></div></div><p class=muted>默认密码为 12345，修改后立即生效。</p></form></div>"
    else:
        presets = [("today", "今天"), ("yesterday", "昨天"), ("week", "本周"), ("last_week", "上周"), ("month", "本月"), ("last_month", "上月"), ("year", "本年度")]
        preset_links = "".join(f"<a class='tab {'active' if active_range == key else ''}' href='/?view=records&channel={channel}&range={key}'>{label}</a>" for key, label in presets)
        body += f"<div class=panel><div class=top><h2>聊天记录</h2><span class=muted>{len(rows)} 条</span></div><div class=tabs><a class='tab {'active' if channel == 'dingtalk' else ''}' href='/?view=records&channel=dingtalk&range=today'>钉钉</a><a class='tab {'active' if channel == 'wechat' else ''}' href='/?view=records&channel=wechat&range=today'>微信</a></div><div class=tabs style='flex-wrap:wrap'>{preset_links}</div><form method=get class=grid><input type=hidden name=view value=records><input type=hidden name=channel value='{channel}'><div><label>开始时间</label><input type=datetime-local name=start value='{html.escape(start.replace(' ', 'T')[:16])}'></div><div><label>结束时间</label><input type=datetime-local name=end value='{html.escape(end.replace(' ', 'T')[:16])}'></div><div><button class=secondary>筛选记录</button></div></form><div style='overflow:auto;margin-top:16px'><table><thead><tr><th>时间</th><th>渠道</th><th>角色</th><th>会话</th><th>消息</th></tr></thead><tbody>{table}</tbody></table></div></div>"
    return page("控制台 - IM 网关", body)


class Handler(BaseHTTPRequestHandler):
    def respond(self, body: str, status=200, headers=None):
        data = body.encode(); self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items(): self.send_header(key, value)
        self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/wechat/status":
            p = DATA / "weixin_qr.json"; data = p.read_text() if p.exists() else '{"status":"not_started"}'
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data.encode()))); self.end_headers(); self.wfile.write(data.encode()); return
        if parsed.path == "/logout": SESSIONS.discard(self.sid()); self.respond("", 303, {"Location":"/login", "Set-Cookie":"sid=; Max-Age=0; Path=/"}); return
        if parsed.path == "/login": self.respond(login_page()); return
        if not logged_in(self): self.respond(login_page(), 401); return
        q = parse_qs(parsed.query); view = q.get("view", ["config"])[0]; start = q.get("start", [""])[0]; end = q.get("end", [""])[0]
        if view == "records" and q.get("range", [""])[0]: start, end = preset_range(q["range"][0])
        self.respond(dashboard(load_config(), view, q.get("channel", [load_config().get("channel", "dingtalk")])[0], start, end))

    def sid(self):
        cookie = SimpleCookie(self.headers.get("Cookie", "")); return cookie.get("sid").value if cookie.get("sid") else ""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0")); data = parse_qs(self.rfile.read(length).decode()); path = urlparse(self.path).path
        if path == "/login":
            if secrets.compare_digest(data.get("password", [""])[0], current_password()):
                sid = secrets.token_urlsafe(24); SESSIONS.add(sid); self.respond("", 303, {"Location":"/", "Set-Cookie":f"sid={sid}; HttpOnly; SameSite=Strict; Path=/"})
            else: self.respond(login_page("密码错误"), 401)
            return
        if not logged_in(self): self.respond(login_page(), 401); return
        if path == "/config":
            old = load_config(); save_config({"channel": data.get("channel", ["dingtalk"])[0], "dingtalk": {"client_id": data.get("client_id", [""])[0], "client_secret": data.get("client_secret", [""])[0]}, "admin_password": old.get("admin_password", PASSWORD), "full_access": bool(old.get("full_access", False))})
            self.respond("", 303, {"Location":"/"}); return
        if path == "/permissions":
            old = load_config(); old["full_access"] = data.get("full_access", [""])[0] == "1"; save_config(old)
            self.respond("", 303, {"Location":"/?view=config"}); return
        if path == "/password":
            new_password = data.get("password", [""])[0]
            if len(new_password) < 5: self.respond(login_page("密码至少需要 5 个字符"), 400); return
            old = load_config(); old["admin_password"] = new_password; save_config(old)
            self.respond("", 303, {"Location":"/?view=config"}); return

    def log_message(self, fmt, *args): pass


if __name__ == "__main__": ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
