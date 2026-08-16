from __future__ import annotations

import asyncio
import logging
import os
import secrets
import json
import threading
import sqlite3
import subprocess
try:
    import tiktoken
except ImportError:
    tiktoken = None
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timezone


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("dingtalk-codex-bot.bridge")
HOST = os.getenv("CODEX_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.getenv("CODEX_BRIDGE_PORT", "8787"))
TOKEN = os.getenv("CODEX_BRIDGE_TOKEN", "")
CODEX_BIN = os.getenv("CODEX_BIN", "/Applications/ChatGPT.app/Contents/Resources/codex")
CODEX_CWD = os.getenv("CODEX_CWD", str(Path.cwd()))
RUNTIME_CONFIG_PATH = Path(os.getenv("CODEX_RUNTIME_CONFIG_PATH", str(Path(CODEX_CWD) / "data" / "runtime.json")))
TIMEOUT = int(os.getenv("CODEX_BRIDGE_TIMEOUT_SECONDS", "180"))
STATUS_TTL_SECONDS = int(os.getenv("CODEX_STATUS_TTL_SECONDS", str(TIMEOUT + 60)))
MAX_INPUT_TOKENS = int(os.getenv("MAX_INPUT_TOKENS", "2500"))
STATUSES: dict[str, dict] = {}
STATUS_LOCK = threading.Lock()
STATUS_FILE = Path(os.getenv("CODEX_STATUS_FILE", str(Path(CODEX_CWD) / "data" / "codex_status.json")))
CODEX_MEMORY_DIR = Path(os.getenv("CODEX_MEMORY_DIR", str(Path.home() / ".codex" / "memories"))).expanduser()
MEMORY_CONTEXT_TOKENS = int(os.getenv("CODEX_MEMORY_CONTEXT_TOKENS", "6000"))
DB_PATH = Path(os.getenv("DB_PATH", str(Path(CODEX_CWD) / "data" / "bot.db")))
if not DB_PATH.exists() and DB_PATH == Path("/app/data/bot.db"):
    DB_PATH = Path(CODEX_CWD) / "data" / "bot.db"
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "32"))
CONTEXT_SUMMARY_THRESHOLD_TOKENS = int(os.getenv("CONTEXT_SUMMARY_THRESHOLD_TOKENS", "6000"))
SUMMARY_MAX_TOKENS = int(os.getenv("SUMMARY_MAX_TOKENS", "1200"))
TOTAL_CONTEXT_TOKENS = int(os.getenv("TOTAL_CONTEXT_TOKENS", "16000"))


def token_count(text: str) -> int:
    if not text:
        return 0
    if tiktoken:
        try:
            return len(tiktoken.get_encoding("cl100k_base").encode(text, disallowed_special=()))
        except Exception:
            pass
    return max(1, (len(text) + 2) // 3)


def truncate_tokens(text: str, limit: int, from_end: bool = False) -> str:
    if limit <= 0 or not text:
        return ""
    if token_count(text) <= limit:
        return text
    if tiktoken:
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            ids = enc.encode(text, disallowed_special=())
            ids = ids[-limit:] if from_end else ids[:limit]
            return enc.decode(ids)
        except Exception:
            pass
    chars = max(1, limit * 3)
    return text[-chars:] if from_end else text[:chars]


def authorized(headers) -> bool:
    if not TOKEN:
        return False
    return secrets.compare_digest(headers.get("Authorization", ""), f"Bearer {TOKEN}")


def full_access_enabled(actor_id: str = "", prompt: str = "") -> bool:
    try:
        if RUNTIME_CONFIG_PATH.exists():
            config = json.loads(RUNTIME_CONFIG_PATH.read_text())
            if not config.get("full_access", False): return False
            users = {item.strip() for item in str(config.get("full_access_users", "")).replace("\n", ",").split(",") if item.strip()}
            if users and actor_id not in users: return False
            risky = any(word in prompt for word in ("删除", "清空", "批量修改", "执行命令", "sudo", "rm -"))
            return not (config.get("high_risk_confirm", True) and risky and "确认执行高风险操作" not in prompt)
    except (OSError, ValueError):
        LOGGER.warning("Unable to read runtime permission config; using read-only mode")
    return False


def runtime_limits() -> tuple[int, int, int, int, int, int]:
    values = (MAX_HISTORY_MESSAGES, CONTEXT_SUMMARY_THRESHOLD_TOKENS, MAX_INPUT_TOKENS, SUMMARY_MAX_TOKENS, MEMORY_CONTEXT_TOKENS, TOTAL_CONTEXT_TOKENS)
    try:
        if RUNTIME_CONFIG_PATH.exists():
            config = json.loads(RUNTIME_CONFIG_PATH.read_text())
            return (
                max(1, int(config.get("max_history_messages", values[0]))),
                max(100, int(config.get("context_summary_threshold_tokens", config.get("context_summary_threshold", values[1])))),
                max(100, int(config.get("max_message_tokens", values[2]))),
                max(100, int(config.get("summary_max_tokens", config.get("summary_max_chars", values[3])))),
                max(500, int(config.get("memory_context_tokens", values[4]))),
                max(1000, int(config.get("total_context_tokens", values[5]))),
            )
    except (OSError, ValueError, TypeError):
        LOGGER.warning("Unable to read conversation limit configuration")
    return values


def set_status(session_id: str, status: str, detail: str = "") -> None:
    with STATUS_LOCK:
        previous = STATUSES.get(session_id, {})
        now = datetime.now(timezone.utc).isoformat()
        value = {
            "session_id": session_id,
            "status": status,
            "detail": detail,
            "started_at": previous.get("started_at") or now,
            "updated_at": now,
        }
        STATUSES[session_id] = value
        persist_statuses_locked()


def persist_statuses_locked() -> None:
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(STATUSES, ensure_ascii=False, indent=2))
        tmp.replace(STATUS_FILE)
    except OSError:
        LOGGER.warning("Unable to persist Codex status file")


def load_statuses_from_disk() -> None:
    if not STATUS_FILE.exists():
        return
    try:
        values = json.loads(STATUS_FILE.read_text())
        if isinstance(values, dict):
            STATUSES.update({str(key): value for key, value in values.items() if isinstance(value, dict)})
    except (OSError, ValueError):
        LOGGER.warning("Unable to read existing Codex status file")


def parse_status_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def stale_status(item: dict) -> bool:
    if item.get("status") not in {"working", "finalizing"}:
        return False
    updated_at = parse_status_time(str(item.get("updated_at", "")))
    if not updated_at:
        return True
    age = datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)
    return age.total_seconds() > STATUS_TTL_SECONDS


def interrupt_active_statuses(reason: str) -> None:
    changed = False
    now = datetime.now(timezone.utc).isoformat()
    with STATUS_LOCK:
        for item in STATUSES.values():
            if item.get("status") in {"working", "finalizing"}:
                item["status"] = "interrupted"
                item["detail"] = reason
                item["updated_at"] = now
                changed = True
        if changed:
            persist_statuses_locked()


def load_codex_memory_context(max_tokens: int = MEMORY_CONTEXT_TOKENS) -> str:
    """Load the local Codex memory corpus for ephemeral IM requests."""
    if not CODEX_MEMORY_DIR.is_dir():
        LOGGER.warning("Codex memory directory does not exist: %s", CODEX_MEMORY_DIR)
        return ""
    files = sorted(CODEX_MEMORY_DIR.rglob("*.md"))
    chunks: list[str] = []
    used = 0
    for path in files:
        if ".git" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if not text:
            continue
        chunk = f"[{path}]\n{text}"
        remaining = max_tokens - used
        if remaining <= 0:
            break
        clipped = truncate_tokens(chunk, remaining)
        chunks.append(clipped)
        used += token_count(clipped)
    if chunks:
        LOGGER.info("Loaded Codex memory context: %d files, %d tokens", len(chunks), used)
    return "\n\n".join(chunks)


def load_conversation_context(session_id: str, max_messages: int) -> tuple[str, str, str, int, int]:
    if not DB_PATH.exists():
        return "", "", "", 0, 0
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, max_messages),
        ).fetchall()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS conversation_summaries (session_id TEXT PRIMARY KEY, summary TEXT NOT NULL, source_count INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
            if "version" not in {row[1] for row in conn.execute("PRAGMA table_info(conversation_summaries)")}: conn.execute("ALTER TABLE conversation_summaries ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
            summary_row = conn.execute("SELECT summary, source_count, version FROM conversation_summaries WHERE session_id=?", (session_id,)).fetchone()
        except sqlite3.OperationalError:
            summary_row = None
        total_count = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,)).fetchone()[0]
        source_count = int((summary_row or ("", 0))[1] or 0)
        pending_rows = conn.execute("SELECT role, content FROM messages WHERE session_id=? ORDER BY id ASC LIMIT -1 OFFSET ?", (session_id, source_count)).fetchall()
        conn.close()
    except sqlite3.Error:
        LOGGER.exception("Unable to load conversation history")
        return "", "", "", 0, 0
    rows.reverse()
    summary, source_count, _version = summary_row or ("", 0, 0)
    recent = "\n".join(f"{role}: {content}" for role, content in rows if content)
    pending = "\n".join(f"{role}: {content}" for role, content in pending_rows if content)
    return recent, pending, summary, int(source_count or 0), int(total_count or 0)


def save_conversation_summary(session_id: str, summary: str, source_count: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS conversation_summaries (session_id TEXT PRIMARY KEY, summary TEXT NOT NULL, source_count INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
    if "version" not in {row[1] for row in conn.execute("PRAGMA table_info(conversation_summaries)")}: conn.execute("ALTER TABLE conversation_summaries ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
    conn.execute("INSERT INTO conversation_summaries(session_id, summary, source_count, version, updated_at) VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP) ON CONFLICT(session_id) DO UPDATE SET summary=excluded.summary, source_count=excluded.source_count, version=conversation_summaries.version+1, updated_at=CURRENT_TIMESTAMP", (session_id, summary, source_count))
    conn.commit(); conn.close()


def build_summary_prompt(session_id: str, existing_summary: str, history: str) -> str:
    return (
        "你是会话摘要器。请将以下即时通讯会话压缩成可供后续助手继续工作的中文摘要。"
        "保留用户目标、已确认事实、关键决定、待办事项、文件路径和未解决问题；删除寒暄和重复内容。"
        "不要编造信息，控制在 2000 字以内，只输出摘要正文。\n\n"
        f"会话：{session_id}\n已有摘要：\n{existing_summary or '无'}\n新增消息：\n{history}"
    )


def invoke_codex_sync_summary(session_id: str, existing_summary: str, history: str) -> str:
    full_access = full_access_enabled()
    sandbox = "danger-full-access" if full_access else "read-only"
    proc = subprocess.run(
        [CODEX_BIN, "exec", "--ephemeral", "--skip-git-repo-check", "-s", sandbox, "--color", "never", "--json", build_summary_prompt(session_id, existing_summary, history)],
        cwd=CODEX_CWD, capture_output=True, text=True, timeout=min(TIMEOUT, 120), check=False,
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip()[-500:] or f"summary exit {proc.returncode}")
    answer = ""
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            answer = str(item.get("text") or "").strip()
    return truncate_tokens(answer, SUMMARY_MAX_TOKENS)


async def invoke_codex(prompt: str, session_id: str, actor_id: str = "") -> str:
    full_access = full_access_enabled(actor_id, prompt)
    sandbox = "danger-full-access" if full_access else "read-only"
    LOGGER.warning("Invoking Codex with sandbox mode: %s", sandbox)
    proc = await asyncio.create_subprocess_exec(
        CODEX_BIN,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "-s",
        sandbox,
        "--color",
        "never",
        "--json",
        prompt,
        cwd=CODEX_CWD,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("本地 Codex 请求超时")
    if proc.returncode:
        detail = stderr.decode(errors="replace").strip()[-1000:]
        if "502 Bad Gateway" in detail or "Upstream request failed" in detail:
            raise RuntimeError("Codex 上游模型服务返回 502，当前自定义 Provider 暂时不可用；请检查 ~/.codex/config.toml 中的 model_providers.custom.base_url 或稍后重试")
        raise RuntimeError(f"本地 Codex 执行失败: {detail or proc.returncode}")
    answer = ""
    for line in stdout.decode(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "item.started":
            item = event.get("item") or {}
            set_status(session_id, "working", describe_item(item))
        elif event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                answer = str(item.get("text") or "").strip()
        elif event.get("type") == "turn.completed":
            set_status(session_id, "finalizing", "正在整理最终回复")
    if not answer:
        raise RuntimeError("本地 Codex 没有返回内容")
    return answer[-18000:]


def describe_item(item: dict) -> str:
    item_type = item.get("type", "")
    if item_type == "command_execution":
        return "正在执行命令"
    if item_type in {"file_read", "file_search"}:
        return "正在读取或搜索文件"
    if item_type in {"mcp_tool_call", "web_search"}:
        return "正在调用外部工具"
    if item_type == "agent_message":
        return "正在生成回复"
    return "正在分析任务"


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload: dict, status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json({"ok": True, "backend": "local-codex"})
            return
        parsed = urlparse(self.path)
        if parsed.path == "/v1/status":
            if not authorized(self.headers):
                self._json({"error": "unauthorized"}, 401)
                return
            session_id = parse_qs(parsed.query).get("session_id", ["default"])[0]
            with STATUS_LOCK:
                status = STATUSES.get(session_id, {"status": "queued", "detail": "等待 Codex 启动"})
                if stale_status(status):
                    status = {
                        **status,
                        "status": "interrupted",
                        "detail": "任务状态超时，可能是 Bridge 或 Codex 进程已中断",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    STATUSES[session_id] = status
                    persist_statuses_locked()
            self._json(status)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path != "/v1/chat":
            self._json({"error": "not found"}, 404)
            return
        if not authorized(self.headers):
            self._json({"error": "unauthorized"}, 401)
            return
        session_id = "default"
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            prompt = str(body.get("prompt", "")).strip()
            session_id = str(body.get("session_id", "")).strip()
            actor_id = str(body.get("actor_id", "")).strip()
            max_messages, summary_threshold, max_message_tokens, summary_max_tokens, memory_tokens, total_tokens = runtime_limits()
            if not prompt:
                self._json({"error": "invalid prompt"}, 400)
                return
            prompt = truncate_tokens(prompt, max_message_tokens)
            if not session_id:
                session_id = "default"
            access_note = (
                "当前请求运行在完全权限模式，可以修改文件、执行命令并访问宿主机资源；仅执行用户明确要求的操作。"
                if full_access_enabled(actor_id, prompt) else
                "如果需要修改文件或执行操作，当前请求运行在只读沙箱；请说明限制。"
            )
            memory_context = load_codex_memory_context(memory_tokens)
            conversation_context, pending_context, conversation_summary, summary_source_count, total_count = load_conversation_context(session_id, max_messages)
            if token_count(pending_context) > summary_threshold and total_count > summary_source_count:
                try:
                    summary = invoke_codex_sync_summary(session_id, conversation_summary, pending_context)
                    if summary:
                        save_conversation_summary(session_id, summary, total_count)
                        conversation_summary = truncate_tokens(summary, summary_max_tokens)
                except Exception:
                    LOGGER.exception("Unable to summarize conversation; using truncated history")
            conversation_context = truncate_tokens(conversation_context, summary_threshold, from_end=True)
            # Keep the current request and control instructions intact; reclaim budget
            # from lower-priority memory and summary content first.
            prompt_tokens = token_count(prompt)
            summary_budget = min(summary_max_tokens, max(100, total_tokens // 8))
            conversation_summary = truncate_tokens(conversation_summary, summary_budget)
            fixed_without_memory = token_count(conversation_summary) + prompt_tokens + 500
            memory_budget = min(memory_tokens, max(500, total_tokens - fixed_without_memory - 1000))
            memory_context = truncate_tokens(memory_context, memory_budget)
            memory_note = (
                "以下是本机桌面 Codex 的完整长期记忆内容。回答涉及用户资料或历史记忆的问题时，"
                "必须先参考这些内容；不要因为当前请求是临时会话就声称没有长期记忆。除非用户明确要求，"
                "不要主动省略或改写记忆中的字段。\n\n"
                f"{memory_context}\n\n"
                if memory_context else
                "本次未读取到本机 Codex 长期记忆内容，不要虚构已保存的资料。\n\n"
            )
            conversation_note = (
                "以下是当前会话的历史消息。请保持上下文连续，除非用户明确要求新开会话，不要把当前问题当成全新对话。\n\n"
                f"会话摘要：{conversation_summary}\n\n"
                f"{conversation_context}\n\n"
                if conversation_context or conversation_summary else "当前会话暂无历史消息。\n\n"
            )
            fixed_tokens = token_count(memory_note) + token_count(conversation_summary) + token_count(prompt) + 500
            recent_budget = max(500, total_tokens - fixed_tokens)
            conversation_context = truncate_tokens(conversation_context, recent_budget, from_end=True)
            instruction = (
            "你是通过即时通讯接入的本地 Codex 工作助手。"
            f"请直接回答用户问题；{access_note}\n\n"
            f"{memory_note}"
            f"{conversation_note}"
            f"会话标识：{session_id}\n用户消息：{prompt}"
            )
            set_status(session_id, "working", "正在启动 Codex")
            answer = asyncio.run(invoke_codex(instruction, session_id, actor_id))
            set_status(session_id, "completed", "已完成")
            self._json({"session_id": session_id, "answer": answer})
        except (ValueError, RuntimeError) as exc:
            set_status(session_id, "failed", str(exc))
            LOGGER.warning("Bridge request failed: %s", exc)
            self._json({"error": str(exc)}, 502)
        except Exception:
            set_status(session_id, "failed", "Bridge 内部错误")
            LOGGER.exception("Unexpected bridge error")
            self._json({"error": "internal bridge error"}, 500)

    def log_message(self, format, *args):
        LOGGER.info("%s - %s", self.address_string(), format % args)


def main() -> None:
    if not TOKEN:
        raise SystemExit("CODEX_BRIDGE_TOKEN must be configured")
    load_statuses_from_disk()
    interrupt_active_statuses("Bridge 已重启，上一次未完成任务已中断")
    LOGGER.info("Starting local Codex bridge on %s:%s", HOST, PORT)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
