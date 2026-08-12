from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from pathlib import Path

import aiohttp
import dingtalk_stream
from dingtalk_stream import AckMessage


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("dingtalk-codex-bot")

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a concise work assistant. Reply in the user's language. Do not claim to have completed actions you did not perform.",
)
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "").strip()
ALLOWED_USERS = {x.strip() for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()}
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "6000"))
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "12"))
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "120"))
DB_PATH = Path(os.getenv("DB_PATH", "/app/data/bot.db"))


class ConversationStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
            "role TEXT NOT NULL, content TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        self.conn.commit()
        self.lock = asyncio.Lock()

    async def history(self, session_id: str) -> list[dict[str, str]]:
        async with self.lock:
            rows = self.conn.execute(
                "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, MAX_HISTORY_MESSAGES),
            ).fetchall()
        return [{"role": role, "content": content} for role, content in reversed(rows)]

    async def append(self, session_id: str, role: str, content: str) -> None:
        async with self.lock:
            self.conn.execute(
                "INSERT INTO messages(session_id, role, content) VALUES (?, ?, ?)",
                (session_id, role, content),
            )
            self.conn.commit()

    async def clear(self, session_id: str) -> None:
        async with self.lock:
            self.conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            self.conn.commit()


STORE = ConversationStore(DB_PATH)
REQUEST_LOCKS: dict[str, asyncio.Lock] = {}


async def ask_openai(session_id: str, prompt: str) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    history = await STORE.history(session_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": prompt}]
    payload = {"model": OPENAI_MODEL, "messages": messages}
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{OPENAI_BASE_URL}/chat/completions", headers=headers, json=payload) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                message = body.get("error", {}).get("message", str(body)) if isinstance(body, dict) else str(body)
                raise RuntimeError(f"AI request failed ({response.status}): {message}")
    answer = body["choices"][0]["message"]["content"].strip()
    await STORE.append(session_id, "user", prompt)
    await STORE.append(session_id, "assistant", answer)
    return answer


class WorkBotHandler(dingtalk_stream.ChatbotHandler):
    async def process(self, callback: dingtalk_stream.CallbackMessage):
        message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
        sender_id = str(getattr(message, "sender_staff_id", "") or getattr(message, "sender_id", ""))
        session_id = str(getattr(message, "conversation_id", "") or sender_id)
        text = message.text.content.strip()

        if ALLOWED_USERS and sender_id not in ALLOWED_USERS:
            LOGGER.warning("Rejected sender: %s", sender_id)
            self.reply_text("当前账号未被授权使用此机器人。", message)
            return AckMessage.STATUS_OK, "OK"

        if text in {"/reset", "重置会话"}:
            await STORE.clear(session_id)
            self.reply_text("会话已重置。", message)
            return AckMessage.STATUS_OK, "OK"

        if COMMAND_PREFIX:
            if not text.startswith(COMMAND_PREFIX):
                return AckMessage.STATUS_OK, "IGNORED"
            text = text[len(COMMAND_PREFIX) :].strip()

        if not text:
            self.reply_text("请输入工作问题。", message)
            return AckMessage.STATUS_OK, "OK"
        if len(text) > MAX_INPUT_CHARS:
            self.reply_text(f"消息过长，最多允许 {MAX_INPUT_CHARS} 个字符。", message)
            return AckMessage.STATUS_OK, "OK"

        lock = REQUEST_LOCKS.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            self.reply_text("上一条任务仍在处理中，请稍后再试。", message)
            return AckMessage.STATUS_OK, "OK"

        async with lock:
            try:
                answer = await ask_openai(session_id, text)
            except Exception:
                LOGGER.exception("Failed to process message from %s", sender_id)
                answer = "请求处理失败，请稍后再试。"
            self.reply_text(answer[:18000], message)
        return AckMessage.STATUS_OK, "OK"


def main() -> None:
    if not OPENAI_API_KEY:
        raise SystemExit("OPENAI_API_KEY is required")
    client_id = os.environ["DINGTALK_CLIENT_ID"]
    client_secret = os.environ["DINGTALK_CLIENT_SECRET"]
    credential = dingtalk_stream.Credential(client_id, client_secret)
    client = dingtalk_stream.DingTalkStreamClient(
        credential,
        websocket_connect_options={"open_timeout": 15, "ping_interval": 20, "ping_timeout": 20},
    )
    client.register_callback_handler(
        dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
        WorkBotHandler(),
    )
    LOGGER.info("Starting DingTalk Stream bot with model %s", OPENAI_MODEL)
    client.start_forever()


if __name__ == "__main__":
    main()
