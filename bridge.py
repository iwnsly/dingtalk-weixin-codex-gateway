from __future__ import annotations

import asyncio
import logging
import os
import secrets
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


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
TIMEOUT = int(os.getenv("CODEX_BRIDGE_TIMEOUT_SECONDS", "180"))
MAX_INPUT = int(os.getenv("MAX_INPUT_CHARS", "6000"))


def authorized(headers) -> bool:
    if not TOKEN:
        return False
    return secrets.compare_digest(headers.get("Authorization", ""), f"Bearer {TOKEN}")


async def invoke_codex(prompt: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        CODEX_BIN,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "--color",
        "never",
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
    answer = stdout.decode(errors="replace").strip()
    if not answer:
        raise RuntimeError("本地 Codex 没有返回内容")
    return answer[-18000:]


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
            instruction = (
            "你是通过即时通讯接入的本地 Codex 工作助手。"
            "请直接回答用户问题；如果需要修改文件或执行操作，先说明限制，当前请求运行在只读沙箱。\n\n"
            f"会话标识：{session_id}\n用户消息：{prompt}"
            )
            answer = asyncio.run(invoke_codex(instruction))
            self._json({"session_id": session_id, "answer": answer})
        except (ValueError, RuntimeError) as exc:
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
