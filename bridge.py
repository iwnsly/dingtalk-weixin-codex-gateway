from __future__ import annotations

import asyncio
import logging
import os
import secrets
import json
import threading
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
            instruction = (
            "你是通过即时通讯接入的本地 Codex 工作助手。"
            f"请直接回答用户问题；{access_note}\n\n"
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
