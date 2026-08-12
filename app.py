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
AI_BACKEND = os.getenv("AI_BACKEND", "openai").strip().lower()
CODEX_BRIDGE_URL = os.getenv("CODEX_BRIDGE_URL", "http://host.docker.internal:8787/v1/chat").rstrip("/")
CODEX_BRIDGE_TOKEN = os.getenv("CODEX_BRIDGE_TOKEN", "")
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
RUNTIME_CONFIG_PATH = Path(os.getenv("RUNTIME_CONFIG_PATH", "/app/data/runtime.json"))


def runtime_config() -> dict:
    if not RUNTIME_CONFIG_PATH.exists():
        return {}
    try:
        import json
        return json.loads(RUNTIME_CONFIG_PATH.read_text())
    except (OSError, ValueError):
        LOGGER.exception("Failed to read runtime config")
        return {}


class ConversationStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
            "role TEXT NOT NULL, content TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(messages)")}
        if "platform" not in columns:
            self.conn.execute("ALTER TABLE messages ADD COLUMN platform TEXT NOT NULL DEFAULT 'dingtalk'")
        self.conn.commit()
        self.lock = asyncio.Lock()

    async def history(self, session_id: str) -> list[dict[str, str]]:
        async with self.lock:
            rows = self.conn.execute(
                "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, MAX_HISTORY_MESSAGES),
            ).fetchall()
        return [{"role": role, "content": content} for role, content in reversed(rows)]

    async def append(self, session_id: str, role: str, content: str, platform: str = "dingtalk") -> None:
        async with self.lock:
            self.conn.execute(
                "INSERT INTO messages(session_id, role, content, platform) VALUES (?, ?, ?, ?)",
                (session_id, role, content, platform),
            )
            self.conn.commit()

    async def clear(self, session_id: str) -> None:
        async with self.lock:
            self.conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            self.conn.commit()


STORE = ConversationStore(DB_PATH)
REQUEST_LOCKS: dict[str, asyncio.Lock] = {}


async def ask_openai(session_id: str, prompt: str, platform: str = "dingtalk") -> str:
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
    await STORE.append(session_id, "user", prompt, platform)
    await STORE.append(session_id, "assistant", answer, platform)
    return answer


async def ask_backend(session_id: str, prompt: str, platform: str = "dingtalk") -> str:
    if AI_BACKEND == "codex":
        if not CODEX_BRIDGE_TOKEN:
            raise RuntimeError("CODEX_BRIDGE_TOKEN is not configured")
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        headers = {"Authorization": f"Bearer {CODEX_BRIDGE_TOKEN}"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                CODEX_BRIDGE_URL,
                headers=headers,
                json={"session_id": session_id, "prompt": prompt},
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(body.get("error", str(body)))
        answer = str(body.get("answer", "")).strip()
        if not answer:
            raise RuntimeError("本地 Codex 没有返回内容")
        await STORE.append(session_id, "user", prompt, platform)
        await STORE.append(session_id, "assistant", answer, platform)
        return answer
    return await ask_openai(session_id, prompt, platform)


class WorkBotHandler(dingtalk_stream.ChatbotHandler):
    async def process(self, callback: dingtalk_stream.CallbackMessage):
        # Keep the raw callback available because the SDK only models text,
        # picture and rich-text messages; newer media types remain in the raw dict.
        raw = callback.data if isinstance(callback.data, dict) else {}
        message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
        sender_id = str(getattr(message, "sender_staff_id", "") or getattr(message, "sender_id", ""))
        session_id = str(getattr(message, "conversation_id", "") or sender_id)
        message_type = str(raw.get("msgtype") or getattr(message, "message_type", "") or "")
        text = ""
        if getattr(message, "text", None) and getattr(message.text, "content", None):
            text = message.text.content.strip()
        elif message_type == "richText" and getattr(message, "rich_text_content", None):
            text = "".join(
                item.get("text", "")
                for item in (message.rich_text_content.rich_text_list or [])
                if isinstance(item, dict)
            ).strip()

        # DingTalk may provide a server-side transcript for audio messages.
        if message_type in {"audio", "voice"} and not text:
            content = raw.get("content") or raw.get("audio") or raw.get("voice") or {}
            if isinstance(content, dict):
                text = str(content.get("recognition") or content.get("text") or content.get("transcript") or "").strip()
            if text:
                LOGGER.info("Received DingTalk voice message with transcript")
            else:
                LOGGER.info("Received DingTalk voice message without transcript")
                self.reply_text("已收到语音，但钉钉没有提供可用的转写文本，暂时无法识别。请改发文字。", message)
                return AckMessage.STATUS_OK, "OK"

        if message_type in {"picture", "image"} and not text:
            LOGGER.info("Received DingTalk image message (download code present=%s)", bool((raw.get("content") or {}).get("downloadCode")))
            self.reply_text("已收到图片。目前此版本可以接收图片通知，但尚未接入图片下载和视觉识别，请改发文字描述。", message)
            return AckMessage.STATUS_OK, "OK"

        if message_type in {"file", "document", "video"} and not text:
            LOGGER.info("Received unsupported DingTalk media message: %s", message_type)
            self.reply_text("已收到文件/媒体消息。目前此版本尚未接入钉钉媒体下载与解析，请改发文字说明。", message)
            return AckMessage.STATUS_OK, "OK"

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
                answer = await ask_backend(session_id, text, "dingtalk")
            except Exception:
                LOGGER.exception("Failed to process message from %s", sender_id)
                answer = "本地 Codex 暂时不可用，请检查 Bridge 状态和配置。" if AI_BACKEND == "codex" else "尚未配置 OPENAI_API_KEY，当前只能验证钉钉连接。"
            self.reply_text(answer[:18000], message)
        return AckMessage.STATUS_OK, "OK"


def main() -> None:
    config = runtime_config()
    channel = config.get("channel", "dingtalk")
    if channel != "dingtalk":
        LOGGER.info("DingTalk adapter disabled; selected channel is %s", channel)
        asyncio.run(asyncio.Event().wait())
        return
    if AI_BACKEND != "codex" and not OPENAI_API_KEY:
        LOGGER.warning("OPENAI_API_KEY is not configured; DingTalk connectivity will start, but AI replies are disabled")
    dingtalk = config.get("dingtalk", {})
    client_id = dingtalk.get("client_id") or os.environ["DINGTALK_CLIENT_ID"]
    client_secret = dingtalk.get("client_secret") or os.environ["DINGTALK_CLIENT_SECRET"]
    credential = dingtalk_stream.Credential(client_id, client_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential, logger=LOGGER)
    client.register_callback_handler(
        dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
        WorkBotHandler(),
    )
    LOGGER.info("Starting DingTalk Stream bot with backend %s", AI_BACKEND)
    client.start_forever()


if __name__ == "__main__":
    main()
