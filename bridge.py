from __future__ import annotations

import asyncio
import logging
import os
import secrets
import json
import threading
import sqlite3
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
MAX_INPUT = int(os.getenv("MAX_INPUT_CHARS", "6000"))
STATUSES: dict[str, dict] = {}
STATUS_LOCK = threading.Lock()
STATUS_FILE = Path(os.getenv("CODEX_STATUS_FILE", str(Path(CODEX_CWD) / "data" / "codex_status.json")))
CODEX_MEMORY_DIR = Path(os.getenv("CODEX_MEMORY_DIR", str(Path.home() / ".codex" / "memories"))).expanduser()
MEMORY_CONTEXT_CHARS = int(os.getenv("CODEX_MEMORY_CONTEXT_CHARS", "24000"))
DB_PATH = Path(os.getenv("DB_PATH", str(Path(CODEX_CWD) / "data" / "bot.db")))
if not DB_PATH.exists() and DB_PATH == Path("/app/data/bot.db"):
    DB_PATH = Path(CODEX_CWD) / "data" / "bot.db"
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "12"))


def authorized(headers) -> bool:
    if not TOKEN:
        return False
    return secrets.compare_digest(headers.get("Authorization", ""), f"Bearer {TOKEN}")


def full_access_enabled() -> bool:
    try:
        if RUNTIME_CONFIG_PATH.exists():
            return bool(json.loads(RUNTIME_CONFIG_PATH.read_text()).get("full_access", False))
    except (OSError, ValueError):
        LOGGER.warning("Unable to read runtime permission config; using read-only mode")
    return False


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
        try:
            STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATUS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(STATUSES, ensure_ascii=False, indent=2))
            tmp.replace(STATUS_FILE)
        except OSError:
            LOGGER.warning("Unable to persist Codex status file")


def load_codex_memory_context() -> str:
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
        remaining = MEMORY_CONTEXT_CHARS - used
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        used += min(len(chunk), remaining)
    if chunks:
        LOGGER.info("Loaded Codex memory context: %d files, %d chars", len(chunks), used)
    return "\n\n".join(chunks)


def load_conversation_context(session_id: str) -> str:
    if not DB_PATH.exists():
        return ""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, MAX_HISTORY_MESSAGES),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        LOGGER.exception("Unable to load conversation history")
        return ""
    rows.reverse()
    return "\n".join(f"{role}: {content}" for role, content in rows if content)


async def invoke_codex(prompt: str, session_id: str) -> str:
    full_access = full_access_enabled()
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
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            prompt = str(body.get("prompt", "")).strip()
            session_id = str(body.get("session_id", "")).strip()
            if not prompt or len(prompt) > MAX_INPUT:
                self._json({"error": "invalid prompt"}, 400)
                return
            if not session_id:
                session_id = "default"
            access_note = (
                "当前请求运行在完全权限模式，可以修改文件、执行命令并访问宿主机资源；仅执行用户明确要求的操作。"
                if full_access_enabled() else
                "如果需要修改文件或执行操作，当前请求运行在只读沙箱；请说明限制。"
            )
            memory_context = load_codex_memory_context()
            conversation_context = load_conversation_context(session_id)
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
                f"{conversation_context}\n\n"
                if conversation_context else "当前会话暂无历史消息。\n\n"
            )
            instruction = (
            "你是通过即时通讯接入的本地 Codex 工作助手。"
            f"请直接回答用户问题；{access_note}\n\n"
            f"{memory_note}"
            f"{conversation_note}"
            f"会话标识：{session_id}\n用户消息：{prompt}"
            )
            set_status(session_id, "working", "正在启动 Codex")
            answer = asyncio.run(invoke_codex(instruction, session_id))
            set_status(session_id, "completed", "已完成")
            self._json({"session_id": session_id, "answer": answer})
        except (ValueError, RuntimeError) as exc:
            set_status(session_id, "failed", str(exc))
            LOGGER.warning("Bridge request failed: %s", exc)
            self._json({"error": str(exc)}, 502)
        except Exception:
            LOGGER.exception("Unexpected bridge error")
            self._json({"error": "internal bridge error"}, 500)

    def log_message(self, format, *args):
        LOGGER.info("%s - %s", self.address_string(), format % args)


def main() -> None:
    if not TOKEN:
        raise SystemExit("CODEX_BRIDGE_TOKEN must be configured")
    LOGGER.info("Starting local Codex bridge on %s:%s", HOST, PORT)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
