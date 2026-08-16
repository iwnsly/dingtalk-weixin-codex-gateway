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
from urllib.parse import parse_qs, urlparse, urlencode

from scheduled_jobs import load_jobs, mutate_jobs

HOST = os.getenv("ADMIN_HOST", "0.0.0.0")
PORT = int(os.getenv("ADMIN_PORT", "8080"))
DATA = Path(os.getenv("DB_PATH", "/app/data/bot.db")).parent
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/data/runtime.json"))
STATUS_FILE = DATA / "codex_status.json"
SCHEDULE_FILE = DATA / "scheduled_jobs.json"
PASSWORD = os.getenv("ADMIN_PASSWORD", "12345")
SESSIONS: set[str] = set()
LOCAL_TZ = timezone(timedelta(hours=8))
STATUS_TTL_SECONDS = int(os.getenv("CODEX_STATUS_TTL_SECONDS", str(int(os.getenv("CODEX_BRIDGE_TIMEOUT_SECONDS", "180")) + 60)))


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
        return {"channel": "dingtalk", "dingtalk": {"client_id": os.getenv("DINGTALK_CLIENT_ID", ""), "client_secret": os.getenv("DINGTALK_CLIENT_SECRET", "")}, "admin_password": PASSWORD, "full_access": False, "wechat_scheduled_jobs": False, "dingtalk_scheduled_jobs": False}
    return json.loads(CONFIG_PATH.read_text())


def current_password() -> str:
    return str(load_config().get("admin_password") or PASSWORD)


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    tmp.replace(CONFIG_PATH)


def load_scheduled_jobs() -> list[dict]:
    return load_jobs(SCHEDULE_FILE)


def scheduled_job_channel(job: dict) -> str:
    channel = str(job.get("channel", "")).strip().lower()
    if channel in {"wechat", "dingtalk"}:
        return channel
    session_id = str(job.get("session_id", ""))
    return "dingtalk" if session_id.startswith("dingtalk:") else "wechat"


def scheduled_job_content(job: dict) -> str:
    if job.get("content"):
        return str(job["content"])
    if job.get("prompt"):
        return str(job["prompt"])
    types = {"daily_fortune": "由 Codex 生成当日运势并发送"}
    return types.get(str(job.get("type", "")), "未配置执行内容")


def parse_status_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_statuses(values: dict) -> tuple[dict, bool]:
    changed = False
    now = datetime.now(timezone.utc)
    for item in values.values():
        if not isinstance(item, dict) or item.get("status") not in {"working", "finalizing"}:
            continue
        updated_at = parse_status_time(str(item.get("updated_at", "")))
        expired = not updated_at or (now - updated_at.astimezone(timezone.utc)).total_seconds() > STATUS_TTL_SECONDS
        if expired:
            item["status"] = "interrupted"
            item["detail"] = "任务状态超时，可能是 Bridge 或 Codex 进程已中断"
            item["updated_at"] = now.isoformat()
            changed = True
    return values, changed


def load_statuses() -> list[dict]:
    if not STATUS_FILE.exists():
        return []
    try:
        values = json.loads(STATUS_FILE.read_text())
        if not isinstance(values, dict):
            return []
        values, changed = normalize_statuses(values)
        if changed:
            tmp = STATUS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(values, ensure_ascii=False, indent=2))
            tmp.replace(STATUS_FILE)
        return sorted(values.values(), key=lambda item: item.get("updated_at", ""), reverse=True)
    except (OSError, ValueError, AttributeError):
        return []


def logged_in(handler: BaseHTTPRequestHandler) -> bool:
    cookie = SimpleCookie(handler.headers.get("Cookie", ""))
    sid = cookie.get("sid")
    return bool(sid and sid.value in SESSIONS)


def page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>{title}</title>
<style>:root{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#172033;background:#f4f7fb}}*{{box-sizing:border-box}}body{{margin:0}}.shell{{max-width:1180px;margin:0 auto;padding:32px 22px}}.top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}}.brand{{font-size:22px;font-weight:750}}.muted{{color:#667085}}.panel{{background:#fff;border:1px solid #e4e9f0;border-radius:14px;padding:22px;box-shadow:0 8px 28px #12233d0a;margin-bottom:18px}}.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}}label{{display:block;font-size:13px;font-weight:650;margin-bottom:7px;color:#344054}}input,select{{width:100%;border:1px solid #d0d5dd;border-radius:8px;padding:10px 11px;font:inherit;background:#fff}}button{{border:0;border-radius:8px;background:#1664d9;color:white;padding:10px 16px;font:inherit;font-weight:650;cursor:pointer}}button.secondary{{background:#eef4ff;color:#1555ad}}button.danger{{background:#fff0f0;color:#b42318}}.tabs{{display:flex;gap:8px;margin-bottom:16px}}.tab{{padding:9px 13px;border-radius:8px;text-decoration:none;color:#475467;background:#f2f4f7}}.tab.active{{background:#1664d9;color:#fff}}table{{width:100%;border-collapse:collapse;font-size:13px}}.schedule-table{{min-width:960px}}th,td{{text-align:left;padding:12px 9px;border-bottom:1px solid #eef1f5;vertical-align:top}}th{{color:#667085;font-size:12px}}td.content{{white-space:pre-wrap;max-width:680px;word-break:break-word}}.badge{{display:inline-block;padding:4px 8px;border-radius:999px;font-size:12px;font-weight:650}}.wechat{{background:#e7f8ef;color:#16804b}}.dingtalk{{background:#e8f1ff;color:#1555ad}}.enabled{{background:#e7f8ef;color:#16804b}}.disabled{{background:#f2f4f7;color:#667085}}.actions{{display:flex;gap:6px;align-items:center}}.actions form{{margin:0}}.login{{max-width:390px;margin:12vh auto}}img.qr{{width:220px;border-radius:10px;border:1px solid #e4e9f0}}@media(max-width:760px){{.grid{{grid-template-columns:1fr}}.shell{{padding:20px 14px}}table{{font-size:12px}}}}
</style><style>.task-dot{{display:inline-block;width:9px;height:9px;border-radius:50%;background:#98a2b3;margin-right:7px}}.task-dot.active{{background:#f5a623;box-shadow:0 0 0 4px #fff3d6}}.filter-label{{font-size:12px;font-weight:700;color:#667085;margin:4px 0 8px}}.selection{{display:inline-block;padding:4px 8px;border-radius:6px;background:#e8f1ff;color:#1555ad;font-size:12px;font-weight:700}}</style><div class=shell>{body}</div></html>"""


def login_page(error="") -> str:
    msg = f"<p style='color:#c0392b'>{html.escape(error)}</p>" if error else ""
    return page("登录 - IM 网关", f"<div class='panel login'><div class=brand>本地 Codex IM 网关</div><p class=muted>请输入管理密码</p>{msg}<form method=post action=/login><label>密码<input type=password name=password autofocus></label><button>登录</button></form></div>")


def dashboard(c: dict, view: str, channel: str, start: str, end: str, session_id: str = "", range_name: str = "") -> str:
    archived_view = view == "archived"
    active_range = range_name if range_name in {"today", "yesterday", "week", "last_week", "month", "last_month", "year"} else "custom"
    if view == "records" and not start and not end:
        start, end = preset_range("today"); active_range = "today"
    qr_path = DATA / "weixin_qr.json"
    qr = json.loads(qr_path.read_text()) if qr_path.exists() else {}
    qr_html = f"<p>状态：{html.escape(qr.get('status', '未启动'))}</p>"
    if qr.get("qr_data") and qr.get("status") in {"waiting", "scaned", "wait"}:
        qr_html += f"<img class=qr src='{html.escape(qr['qr_data'])}' alt='微信登录二维码'>"
    rows = []
    sessions = []
    db = DATA / "bot.db"
    if db.exists():
        conn = sqlite3.connect(db)
        if session_id:
            query = "SELECT created_at, platform, role, session_id, content FROM messages WHERE platform=?"
            args = [channel]
            if start: query += " AND created_at >= ?"; args.append(start.replace("T", " "))
            if end: query += " AND created_at <= ?"; args.append(end.replace("T", " "))
            query += " AND session_id=? ORDER BY id DESC LIMIT 500"; args.append(session_id)
            for created, platform, role, record_session_id, content in conn.execute(query, args):
                rows.append(f"<tr><td>{html.escape(created)}</td><td><span class='badge {platform}'>{'微信' if platform == 'wechat' else '钉钉'}</span></td><td>{'用户' if role == 'user' else '助手'}</td><td>{html.escape(record_session_id[:28])}</td><td class=content>{html.escape(content)}</td></tr>")
        conn.close()
        if not session_id:
            conn = sqlite3.connect(db)
            query = """SELECT m.session_id, MIN(m.created_at), MAX(m.created_at), COUNT(*),
                COALESCE(
                    (SELECT content FROM messages u WHERE u.session_id=m.session_id AND u.platform=m.platform AND u.role='user' ORDER BY u.id LIMIT 1),
                    (SELECT content FROM messages f WHERE f.session_id=m.session_id AND f.platform=m.platform ORDER BY f.id LIMIT 1)
                )
                FROM messages m JOIN sessions s ON s.session_id=m.session_id WHERE m.platform=? AND s.archived_at IS %s""" % ("NOT NULL" if archived_view else "NULL")
            args = [channel]
            if start: query += " AND m.created_at >= ?"; args.append(start.replace("T", " "))
            if end: query += " AND m.created_at <= ?"; args.append(end.replace("T", " "))
            query += " GROUP BY m.session_id ORDER BY MAX(m.created_at) DESC"
            for sid, first_seen, last_seen, count, title in conn.execute(query, args):
                title = str(title or "（无标题消息）").strip()
                short_title = title if len(title) <= 48 else title[:48] + "…"
                params = urlencode({"view": "records", "channel": channel, "range": "custom", "start": start, "end": end, "session": sid})
                action = "restore" if archived_view else "archive"
                label = "恢复" if archived_view else "归档"
                sessions.append(f"<tr><td><a title='{html.escape(title, quote=True)}' href='/?{params}'>{html.escape(short_title)}</a><div class=muted style='font-size:11px;margin-top:4px'>{html.escape(sid[:42])}</div></td><td>{html.escape(first_seen or '')}</td><td>{html.escape(last_seen or '')}</td><td>{count}</td><td><form method=post action=/session/{action}><input type=hidden name=session_id value='{html.escape(sid)}'><button class=secondary>{label}</button></form></td></tr>")
            conn.close()
    table = "".join(rows) or "<tr><td colspan=5 class=muted>当前筛选暂无记录</td></tr>"
    selected_d = "selected" if channel == "dingtalk" else ""; selected_w = "selected" if channel == "wechat" else ""
    body = f"<div class=top><div><div class=brand>本地 Codex IM 网关</div><div class=muted>渠道配置与对话审计</div></div><a class=tab href=/logout>退出</a></div>"
    body += f"<div class=tabs><a class='tab {'active' if view == 'records' else ''}' href='/?view=records&channel={channel}'>聊天记录</a><a class='tab {'active' if view == 'schedules' else ''}' href='/?view=schedules'>定时任务</a><a class='tab {'active' if view == 'config' else ''}' href='/?view=config'>配置</a></div>"
    if view == "config":
        full_access = bool(c.get("full_access", False))
        access_users = str(c.get("full_access_users", ""))
        checked = "checked" if full_access else ""
        access_panel = f"<div class=panel><h2>Codex 权限</h2><form method=post action=/permissions><label style='display:flex;gap:10px;align-items:center'><input type=checkbox name=full_access value=1 {checked} style='width:auto'>启用完全权限</label><label>完全权限用户白名单（每行一个账号）<textarea name=full_access_users rows=3 style='width:100%;padding:10px'>{html.escape(access_users)}</textarea></label><label style='display:flex;gap:10px;align-items:center'><input type=checkbox name=high_risk_confirm value=1 {'checked' if c.get('high_risk_confirm', True) else ''} style='width:auto'>高风险操作必须明确确认</label><p class=muted>启用后仍要求请求中包含“确认执行高风险操作”，用于降低误操作风险。</p><button class='secondary'>保存权限</button></form></div>"
        limits = {"max_history_messages": int(c.get("max_history_messages", 32)), "context_summary_threshold_tokens": int(c.get("context_summary_threshold_tokens", c.get("context_summary_threshold", 6000))), "max_message_tokens": int(c.get("max_message_tokens", c.get("max_message_chars", 2500))), "summary_max_tokens": int(c.get("summary_max_tokens", c.get("summary_max_chars", 1200))), "memory_context_tokens": int(c.get("memory_context_tokens", 6000)), "total_context_tokens": int(c.get("total_context_tokens", 16000))}
        body += f"<div class=panel><h2>消息入口</h2><form method=post action=/config><div class=grid><div><label>当前渠道</label><select name=channel><option value=dingtalk {selected_d}>钉钉</option><option value=wechat {selected_w}>微信</option></select></div><div style='display:flex;align-items:end'><button>保存渠道</button></div></div><div class=grid style='margin-top:18px'><div><label>钉钉 Client ID</label><input name=client_id value='{html.escape(c.get('dingtalk', {}).get('client_id', ''))}'></div><div><label>钉钉 Client Secret</label><input type=password name=client_secret value='{html.escape(c.get('dingtalk', {}).get('client_secret', ''))}'></div></div><div class=grid style='margin-top:18px'><div><label>保留最近消息数</label><input type=number name=max_history_messages min=1 max=200 value='{limits['max_history_messages']}'></div><div><label>自动摘要阈值（token）</label><input type=number name=context_summary_threshold_tokens min=100 max=100000 value='{limits['context_summary_threshold_tokens']}'></div><div><label>单条消息上限（token）</label><input type=number name=max_message_tokens min=100 max=20000 value='{limits['max_message_tokens']}'></div><div><label>摘要最大长度（token）</label><input type=number name=summary_max_tokens min=100 max=10000 value='{limits['summary_max_tokens']}'></div><div><label>长期记忆上限（token）</label><input type=number name=memory_context_tokens min=500 max=50000 value='{limits['memory_context_tokens']}'></div><div><label>总上下文预算（token）</label><input type=number name=total_context_tokens min=1000 max=100000 value='{limits['total_context_tokens']}'></div></div></form></div>" + access_panel
        body += f"<div class=panel><h2>微信登录</h2>{qr_html}<p class=muted>选择微信并保存后，系统自动生成二维码。扫码成功后凭据保存在本机数据目录。</p></div>"
        body += "<div class=panel><h2>管理密码</h2><form method=post action=/password><div class=grid><div><label>新密码</label><input type=password name=password minlength=5 required></div><div style='display:flex;align-items:end'><button>修改密码</button></div></div><p class=muted>默认密码为 12345，修改后立即生效。</p></form></div>"
    elif view == "schedules":
        schedule_channel = channel if channel in {"wechat", "dingtalk"} else "all"
        schedule_rows = []
        jobs = load_scheduled_jobs()
        for job in jobs:
            job_channel = scheduled_job_channel(job)
            if schedule_channel != "all" and job_channel != schedule_channel:
                continue
            enabled = bool(job.get("enabled", True))
            job_id = str(job.get("id", ""))
            schedule = f"{job.get('time', '--:--')} · {job.get('timezone', 'Asia/Shanghai')}"
            status_labels = {"running": "执行中", "success": "执行成功", "failed": "执行失败"}
            last_status = str(job.get("last_status", ""))
            last_result = str(job.get("last_error") or status_labels.get(last_status) or ("执行成功" if job.get("last_sent_at") else "尚未执行"))
            last_time = str(job.get("last_sent_at") or job.get("last_run_at") or "-").replace("T", " ")[:19]
            toggle_label = "停用" if enabled else "启用"
            schedule_rows.append(f"<tr><td><div style='font-weight:700'>{html.escape(str(job.get('name') or job_id or '未命名任务'))}</div><div class=muted style='font-size:11px;margin-top:4px'>{html.escape(job_id)}</div></td><td><span class='badge {job_channel}'>{'微信' if job_channel == 'wechat' else '钉钉'}</span></td><td><span class='badge {'enabled' if enabled else 'disabled'}'>{'已启用' if enabled else '已停用'}</span></td><td>{html.escape(schedule)}</td><td class=content>{html.escape(scheduled_job_content(job))}</td><td><div>{html.escape(last_result)}</div><div class=muted style='font-size:11px;margin-top:4px'>{html.escape(last_time)}</div></td><td><div class=actions><form method=post action=/schedule/toggle><input type=hidden name=job_id value='{html.escape(job_id, quote=True)}'><button class=secondary>{toggle_label}</button></form><form method=post action=/schedule/delete onsubmit=\"return confirm('确定删除这个定时任务吗？')\"><input type=hidden name=job_id value='{html.escape(job_id, quote=True)}'><button class=danger>删除</button></form></div></td></tr>")
        schedule_table = "".join(schedule_rows) or "<tr><td colspan=7 class=muted>当前渠道暂无定时任务</td></tr>"
        body += f"<div class=panel><div class=top><div><h2 style='margin:0 0 6px'>定时任务</h2><div class=muted>统一查看各渠道任务的计划、执行内容和最近结果</div></div><span class=selection>{len(jobs)} 个任务</span></div><div class=filter-label>渠道筛选</div><div class=tabs><a class='tab {'active' if schedule_channel == 'all' else ''}' href='/?view=schedules&channel=all'>全部</a><a class='tab {'active' if schedule_channel == 'wechat' else ''}' href='/?view=schedules&channel=wechat'>微信</a><a class='tab {'active' if schedule_channel == 'dingtalk' else ''}' href='/?view=schedules&channel=dingtalk'>钉钉</a></div><form method=post action=/schedule-executors style='margin:18px 0'><div class=grid><label style='display:flex;gap:10px;align-items:center'><input type=checkbox name=wechat_scheduled_jobs value=1 {'checked' if c.get('wechat_scheduled_jobs', False) else ''} style='width:auto'>启用微信定时任务执行器</label><label style='display:flex;gap:10px;align-items:center'><input type=checkbox name=dingtalk_scheduled_jobs value=1 {'checked' if c.get('dingtalk_scheduled_jobs', False) else ''} style='width:auto'>启用钉钉定时任务执行器</label></div><p class=muted>钉钉启用后，需要目标用户或群聊至少向机器人发送过一条消息，以保存主动发送路由。</p><button class=secondary>保存执行器状态</button></form><div style='overflow:auto'><table class=schedule-table><thead><tr><th>任务</th><th>渠道</th><th>状态</th><th>执行计划</th><th>执行内容</th><th>最近执行</th><th>操作</th></tr></thead><tbody>{schedule_table}</tbody></table></div></div>"
    else:
        presets = [("today", "今天"), ("yesterday", "昨天"), ("week", "本周"), ("last_week", "上周"), ("month", "本月"), ("last_month", "上月"), ("year", "本年度")]
        preset_links = "".join(f"<a class='tab {'active' if active_range == key else ''}' href='/?view=records&channel={channel}&range={key}'>{label}</a>" for key, label in presets)
        session_panel = ""
        if not session_id:
            session_table = "".join(sessions) or "<tr><td colspan=4 class=muted>当前筛选暂无会话</td></tr>"
            session_panel = f"<div class=panel><div class=top><h2>按会话查看</h2><span class=muted>{len(sessions)} 个会话</span></div><div style='overflow:auto'><table><thead><tr><th>会话标题</th><th>开始时间</th><th>最后活动</th><th>消息数</th><th>操作</th></tr></thead><tbody>{session_table}</tbody></table></div><p><a class=tab href='/?view=archived&channel={channel}'>查看已归档会话</a></p></div>"
        else:
            return_params = urlencode({"view": "records", "channel": channel, "range": active_range if active_range != "custom" else "today"})
            session_panel = f"<p><a class=tab href='/?{return_params}'>返回会话列表</a> <span class=muted>当前会话：{html.escape(session_id)}</span></p>"
        selected_label = '自定义时间' if active_range == 'custom' and (start or end) else dict(presets).get(active_range, '今天')
        detail_table = f"<div style='overflow:auto;margin-top:16px'><table><thead><tr><th>时间</th><th>渠道</th><th>角色</th><th>会话</th><th>消息</th></tr></thead><tbody>{table}</tbody></table></div>" if session_id else ""
        body += f"<div class=panel><div class=top><h2>聊天记录</h2><span class=selection>{'会话明细' if session_id else '会话分组'} · {selected_label}</span></div><div class=filter-label>渠道筛选</div><div class=tabs><a class='tab {'active' if channel == 'dingtalk' else ''}' href='/?view=records&channel=dingtalk&range=today'>钉钉</a><a class='tab {'active' if channel == 'wechat' else ''}' href='/?view=records&channel=wechat&range=today'>微信</a></div><div class=filter-label>时间筛选</div><div class=tabs style='flex-wrap:wrap'>{preset_links}</div><form method=get class=grid><input type=hidden name=view value=records><input type=hidden name=channel value='{channel}'><input type=hidden name=session value='{html.escape(session_id)}'><div><label>开始时间</label><input type=datetime-local name=start value='{html.escape(start.replace(' ', 'T')[:16])}'></div><div><label>结束时间</label><input type=datetime-local name=end value='{html.escape(end.replace(' ', 'T')[:16])}'></div><div><button class=secondary>筛选记录</button></div></form>{session_panel}{detail_table}</div>"
    task_rows = []
    for item in load_statuses():
        active = item.get("status") in {"working", "finalizing"}
        task_rows.append(f"<tr><td><span class='task-dot {'active' if active else ''}'></span>{'运行中' if active else html.escape(item.get('status', 'unknown'))}</td><td>{html.escape(item.get('detail', ''))}</td><td>{html.escape(item.get('session_id', '')[:42])}</td><td>{html.escape(item.get('updated_at', '').replace('T', ' ')[:19])}</td></tr>")
    task_table = "".join(task_rows) or "<tr><td colspan=4 class=muted>暂无任务状态</td></tr>"
    task_panel = f"<div class=panel id=task-panel><div class=top><h2>当前任务</h2><span class=muted>自动刷新</span></div><div style='overflow:auto'><table><thead><tr><th>状态</th><th>当前阶段</th><th>会话</th><th>更新时间</th></tr></thead><tbody id=task-body>{task_table}</tbody></table></div></div>"
    body = body.replace("<div class=tabs>", task_panel + "<div class=tabs>", 1)
    body += "<script>setInterval(async()=>{try{const r=await fetch('/api/tasks');if(!r.ok)return;const d=await r.json();const b=document.getElementById('task-body');if(!b)return;b.innerHTML=d.tasks.map(t=>`<tr><td><span class=\"task-dot ${['working','finalizing'].includes(t.status)?'active':''}\"></span>${['working','finalizing'].includes(t.status)?'运行中':t.status}</td><td>${t.detail||''}</td><td>${(t.session_id||'').slice(0,42)}</td><td>${(t.updated_at||'').replace('T',' ').slice(0,19)}</td></tr>`).join('')||'<tr><td colspan=4 class=muted>暂无任务状态</td></tr>'}catch(e){}},5000)</script>"
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
        if parsed.path == "/api/tasks":
            if not logged_in(self): self.respond('{"error":"unauthorized"}', 401); return
            data = json.dumps({"tasks": load_statuses()}, ensure_ascii=False)
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data.encode()))); self.end_headers(); self.wfile.write(data.encode()); return
        if parsed.path == "/logout": SESSIONS.discard(self.sid()); self.respond("", 303, {"Location":"/login", "Set-Cookie":"sid=; Max-Age=0; Path=/"}); return
        if parsed.path == "/login": self.respond(login_page()); return
        if not logged_in(self): self.respond(login_page(), 401); return
        q = parse_qs(parsed.query); view = q.get("view", ["records"])[0]; start = q.get("start", [""])[0]; end = q.get("end", [""])[0]
        range_name = q.get("range", [""])[0]
        if view == "records" and range_name in {"today", "yesterday", "week", "last_week", "month", "last_month", "year"}: start, end = preset_range(range_name)
        default_channel = "all" if view == "schedules" else load_config().get("channel", "dingtalk")
        self.respond(dashboard(load_config(), view, q.get("channel", [default_channel])[0], start, end, q.get("session", [""])[0], range_name))

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
        if path == "/session/archive":
            sid = data.get("session_id", [""])[0]
            conn = sqlite3.connect(DATA / "bot.db"); conn.execute("UPDATE sessions SET archived_at=CURRENT_TIMESTAMP WHERE session_id=?", (sid,)); conn.commit(); conn.close()
            self.respond("", 303, {"Location":"/?view=records"}); return
        if path == "/session/restore":
            sid = data.get("session_id", [""])[0]
            conn = sqlite3.connect(DATA / "bot.db"); conn.execute("UPDATE sessions SET archived_at=NULL, last_active_at=CURRENT_TIMESTAMP WHERE session_id=?", (sid,)); conn.commit(); conn.close()
            self.respond("", 303, {"Location":"/?view=records"}); return
        if path == "/config":
            old = load_config()
            def bounded(name, default, minimum, maximum):
                try: return max(minimum, min(maximum, int(data.get(name, [default])[0])))
                except (TypeError, ValueError): return default
            save_config({"channel": data.get("channel", ["dingtalk"])[0], "dingtalk": {"client_id": data.get("client_id", [""])[0], "client_secret": data.get("client_secret", [""])[0]}, "admin_password": old.get("admin_password", PASSWORD), "full_access": bool(old.get("full_access", False)), "full_access_users": old.get("full_access_users", ""), "high_risk_confirm": bool(old.get("high_risk_confirm", True)), "wechat_scheduled_jobs": bool(old.get("wechat_scheduled_jobs", False)), "dingtalk_scheduled_jobs": bool(old.get("dingtalk_scheduled_jobs", False)), "max_history_messages": bounded("max_history_messages", 32, 1, 200), "context_summary_threshold_tokens": bounded("context_summary_threshold_tokens", 6000, 100, 100000), "max_message_tokens": bounded("max_message_tokens", 2500, 100, 20000), "summary_max_tokens": bounded("summary_max_tokens", 1200, 100, 10000), "memory_context_tokens": bounded("memory_context_tokens", 6000, 500, 50000), "total_context_tokens": bounded("total_context_tokens", 16000, 1000, 100000)})
            self.respond("", 303, {"Location":"/"}); return
        if path == "/permissions":
            old = load_config(); old["full_access"] = data.get("full_access", [""])[0] == "1"; old["full_access_users"] = data.get("full_access_users", [""])[0]; old["high_risk_confirm"] = data.get("high_risk_confirm", [""])[0] == "1"; save_config(old)
            self.respond("", 303, {"Location":"/?view=config"}); return
        if path in {"/wechat-schedule", "/schedule-executors"}:
            old = load_config()
            old["wechat_scheduled_jobs"] = data.get("wechat_scheduled_jobs", [""])[0] == "1"
            if path == "/schedule-executors":
                old["dingtalk_scheduled_jobs"] = data.get("dingtalk_scheduled_jobs", [""])[0] == "1"
            save_config(old)
            self.respond("", 303, {"Location":"/?view=schedules"}); return
        if path in {"/schedule/toggle", "/schedule/delete"}:
            job_id = data.get("job_id", [""])[0]
            if path == "/schedule/delete":
                mutate_jobs(SCHEDULE_FILE, lambda jobs: jobs.__setitem__(slice(None), [job for job in jobs if str(job.get("id", "")) != job_id]))
            else:
                def toggle(jobs):
                    for job in jobs:
                        if str(job.get("id", "")) == job_id:
                            job["enabled"] = not bool(job.get("enabled", True))
                            break
                mutate_jobs(SCHEDULE_FILE, toggle)
            self.respond("", 303, {"Location":"/?view=schedules"}); return
        if path == "/password":
            new_password = data.get("password", [""])[0]
            if len(new_password) < 5: self.respond(login_page("密码至少需要 5 个字符"), 400); return
            old = load_config(); old["admin_password"] = new_password; save_config(old)
            self.respond("", 303, {"Location":"/?view=config"}); return

    def log_message(self, fmt, *args): pass


if __name__ == "__main__": ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
