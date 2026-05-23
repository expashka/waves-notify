#!/usr/bin/env python3
"""waves-notify — universal lead notification service.

Channels: Telegram bot + Email (SMTP).
Both channels are optional and independent.

Endpoints:
  POST /lead      — receive form submission, save to JSONL, notify all channels
  POST /notify    — send arbitrary text notification (internal use, requires token)
  GET  /health    — service health

Telegram bot commands:
  /start, /chat_id, /notify_list, /add_notify, /remove_notify
"""

import asyncio
import json
import os
import smtplib
import ssl
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from aiohttp import web

try:
    from aiogram import Bot
    from aiogram.types import (
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        KeyboardButton,
        ReplyKeyboardMarkup,
    )
    AIOGRAM_AVAILABLE = True
except ImportError:
    AIOGRAM_AVAILABLE = False
    Bot = None


# ───────────── config ─────────────

def _load_env_file():
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()

_load_env_file()

def _parse_ids(value: str) -> set[str]:
    return {s.strip() for s in value.replace(";", ",").split(",") if s.strip()}

# Telegram
TG_BOT_TOKEN          = os.getenv("TG_BOT_TOKEN", "").strip()
TG_ADMIN_CHAT_ID      = os.getenv("TG_ADMIN_CHAT_ID", "").strip()
TG_SUPER_ADMIN_IDS    = _parse_ids(os.getenv("TG_SUPER_ADMIN_IDS", ""))
TG_POLLING_ENABLED    = os.getenv("TG_POLLING_ENABLED", "1").strip() != "0"
TG_RECIPIENTS_FILE    = Path(os.getenv("TG_RECIPIENTS_FILE") or "/data/tg_recipients.json")

if TG_ADMIN_CHAT_ID:
    TG_SUPER_ADMIN_IDS.add(TG_ADMIN_CHAT_ID)

# Email — provider presets (host, port, ssl)
_SMTP_PRESETS: dict[str, tuple[str, int, bool]] = {
    "yandex": ("smtp.yandex.ru",  465, True),
    "mailru":  ("smtp.mail.ru",    465, True),
    "gmail":   ("smtp.gmail.com",  465, True),
}

SMTP_USER     = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_FROM     = os.getenv("SMTP_FROM", SMTP_USER).strip()
SMTP_TO       = _parse_ids(os.getenv("SMTP_TO", ""))

_provider = os.getenv("SMTP_PROVIDER", "").strip().lower()
_preset = _SMTP_PRESETS.get(_provider, ("", 465, True))
SMTP_HOST = os.getenv("SMTP_HOST", _preset[0]).strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", str(_preset[1])))
SMTP_SSL  = os.getenv("SMTP_SSL", "1" if _preset[2] else "0").strip() != "0"

# General
NOTIFY_SECRET = os.getenv("NOTIFY_SECRET", "").strip()
SITE_NAME     = os.getenv("SITE_NAME", "waves-notify").strip()
PORT          = int(os.getenv("PORT", "8080"))
LEADS_FILE    = Path(os.getenv("LEADS_FILE") or "/data/leads.jsonl")


# ───────────── Telegram setup ─────────────

bot: Any = None
_polling_task: asyncio.Task | None = None
_known_chats: set[str] = set()
_admin_pending_actions: dict[str, str] = {}

if AIOGRAM_AVAILABLE and TG_BOT_TOKEN:
    bot = Bot(TG_BOT_TOKEN)
elif not TG_BOT_TOKEN:
    print("⚠  TG_BOT_TOKEN not set — Telegram notifications disabled", flush=True)
elif not AIOGRAM_AVAILABLE:
    print("⚠  aiogram not installed — Telegram notifications disabled", flush=True)


# ───────────── helpers ─────────────

def msk_now() -> str:
    msk = timezone(timedelta(hours=3))
    return datetime.now(msk).strftime("%d.%m.%Y %H:%M МСК")

def safe(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]

def normalize_chat_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("-"):
        return text if text[1:].isdigit() else ""
    return text if text.isdigit() else ""


# ───────────── recipients ─────────────

def load_recipients() -> set[str]:
    recipients = set()
    if TG_ADMIN_CHAT_ID:
        recipients.add(TG_ADMIN_CHAT_ID)
    try:
        if TG_RECIPIENTS_FILE.exists():
            data = json.loads(TG_RECIPIENTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                recipients.update(str(x).strip() for x in data if str(x).strip())
    except Exception:
        pass
    return recipients

def save_recipients(recipients: set[str]):
    TG_RECIPIENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TG_RECIPIENTS_FILE.write_text(
        json.dumps(sorted(recipients), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ───────────── email ─────────────

def send_email(subject: str, body: str) -> list[str]:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and SMTP_TO):
        return []
    errors = []
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(sorted(SMTP_TO))
    try:
        if SMTP_SSL:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM, list(SMTP_TO), msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM, list(SMTP_TO), msg.as_string())
    except Exception as exc:
        errors.append(str(exc))
        print(f"email error: {exc}", flush=True)
    return errors

async def send_email_async(subject: str, body: str) -> list[str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, send_email, subject, body)


# ───────────── telegram ─────────────

def _admin_kb() -> "ReplyKeyboardMarkup":
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📬 Рассылка"), KeyboardButton(text="👮 Админы")],
            [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="➖ Удалить")],
            [KeyboardButton(text="🆔 Мой chat_id"), KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )

def _pending_cancel_kb() -> "InlineKeyboardMarkup":
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin:pending:cancel")]]
    )

def _onboarding_kb(target: str, subscribed: bool) -> "InlineKeyboardMarkup":
    btn = (
        InlineKeyboardButton(text="➖ Убрать", callback_data=f"admin:remove:{target}")
        if subscribed
        else InlineKeyboardButton(text="➕ Добавить", callback_data=f"admin:add:{target}")
    )
    return InlineKeyboardMarkup(inline_keyboard=[[btn, InlineKeyboardButton(text="🆔 ID", callback_data=f"admin:chatid:{target}")]])

MENU_BUTTONS = {"📬 Рассылка", "👮 Админы", "➕ Добавить", "➖ Удалить", "🆔 Мой chat_id", "❌ Отмена"}


async def tg_broadcast(text: str, chat_ids: list[str]):
    if not bot:
        return
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, text, disable_web_page_preview=True)
        except Exception as exc:
            print(f"telegram send error {chat_id}: {exc}", flush=True)

async def _notify_new_chat(message: dict, chat_id: str):
    if chat_id in _known_chats:
        return
    _known_chats.add(chat_id)
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    subscribed = chat_id in load_recipients()
    text = (
        f"🆕 Новое подключение\n\n"
        f"chat_id: {chat_id}\n"
        f"type: {chat.get('type') or '-'}\n"
        f"user: {user.get('username') or user.get('first_name') or '-'}\n"
        f"статус: {'в рассылке' if subscribed else 'не в рассылке'}"
    )
    for admin_id in sorted(TG_SUPER_ADMIN_IDS):
        try:
            await bot.send_message(admin_id, text, reply_markup=_onboarding_kb(chat_id, subscribed))
        except Exception as exc:
            print(f"telegram onboarding error {admin_id}: {exc}", flush=True)

async def _format_recipients(recipients: set[str]) -> str:
    if not recipients:
        return "Список пуст."
    lines = []
    for chat_id in sorted(recipients):
        label = ""
        try:
            chat = await bot.get_chat(chat_id)
            label = getattr(chat, "username", None) and f"@{chat.username}" or getattr(chat, "title", None) or getattr(chat, "first_name", None) or ""
        except Exception:
            pass
        lines.append(f"- {chat_id}" + (f" — {label}" if label else ""))
    return "\n".join(lines)

async def handle_tg_message(message: dict):
    chat_id = str((message.get("chat") or {}).get("id") or "")
    text = str(message.get("text") or "").strip()
    if not chat_id:
        return

    await _notify_new_chat(message, chat_id)
    is_admin = chat_id in TG_SUPER_ADMIN_IDS
    pending = _admin_pending_actions.get(chat_id)

    if pending and (text.startswith("/") or text in MENU_BUTTONS):
        _admin_pending_actions.pop(chat_id, None)
        await bot.send_message(chat_id, "Режим ввода отменён.")
        pending = None

    if pending and not text.startswith("/"):
        if not is_admin:
            _admin_pending_actions.pop(chat_id, None)
            await bot.send_message(chat_id, "Нет доступа.")
            return
        target = normalize_chat_id(text)
        if not target:
            await bot.send_message(chat_id, "Неверный chat_id.")
            return
        recipients = load_recipients()
        if pending == "add":
            recipients.add(target)
            save_recipients(recipients)
            await bot.send_message(chat_id, f"Добавлен: {target}")
        elif pending == "remove":
            recipients.discard(target)
            save_recipients(recipients)
            await bot.send_message(chat_id, f"Удалён: {target}")
        _admin_pending_actions.pop(chat_id, None)
        return

    if not text:
        return

    if text == "🆔 Мой chat_id":
        await bot.send_message(chat_id, f"chat_id: {chat_id}")
        return
    if text == "❌ Отмена":
        _admin_pending_actions.pop(chat_id, None)
        await bot.send_message(chat_id, "Отменено.")
        return
    if text == "📬 Рассылка":
        if not is_admin:
            await bot.send_message(chat_id, "Нет доступа.")
            return
        await bot.send_message(chat_id, await _format_recipients(load_recipients()))
        return
    if text == "👮 Админы":
        if not is_admin:
            await bot.send_message(chat_id, "Нет доступа.")
            return
        admins = sorted(TG_SUPER_ADMIN_IDS)
        await bot.send_message(chat_id, "\n".join(f"- {a}" for a in admins) if admins else "Нет админов.")
        return
    if text == "➕ Добавить":
        if not is_admin:
            await bot.send_message(chat_id, "Нет доступа.")
            return
        _admin_pending_actions[chat_id] = "add"
        await bot.send_message(chat_id, "Пришлите chat_id для добавления.", reply_markup=_pending_cancel_kb())
        return
    if text == "➖ Удалить":
        if not is_admin:
            await bot.send_message(chat_id, "Нет доступа.")
            return
        _admin_pending_actions[chat_id] = "remove"
        await bot.send_message(chat_id, "Пришлите chat_id для удаления.", reply_markup=_pending_cancel_kb())
        return

    if not text.startswith("/"):
        return

    parts = text.strip().split()
    cmd = parts[0].split("@", 1)[0].lower()
    args = parts[1:]

    if cmd == "/start":
        if is_admin:
            await bot.send_message(chat_id, f"🔧 Панель {SITE_NAME}", reply_markup=_admin_kb())
        else:
            await bot.send_message(chat_id, f"✅ Бот {SITE_NAME} запущен.\n/chat_id — мой ID")
        return
    if cmd == "/chat_id":
        await bot.send_message(chat_id, f"chat_id: {chat_id}")
        return
    if not is_admin:
        await bot.send_message(chat_id, "Нет доступа.")
        return
    recipients = load_recipients()
    if cmd == "/notify_list":
        await bot.send_message(chat_id, await _format_recipients(recipients))
    elif cmd == "/add_notify":
        target = normalize_chat_id(args[0]) if args else ""
        if not target:
            await bot.send_message(chat_id, "Укажи chat_id.")
            return
        recipients.add(target)
        save_recipients(recipients)
        await bot.send_message(chat_id, f"Добавлен: {target}")
    elif cmd == "/remove_notify":
        target = normalize_chat_id(args[0]) if args else ""
        if not target:
            await bot.send_message(chat_id, "Укажи chat_id.")
            return
        recipients.discard(target)
        save_recipients(recipients)
        await bot.send_message(chat_id, f"Удалён: {target}")

async def handle_tg_callback(callback: dict):
    data = str(callback.get("data") or "")
    callback_id = str(callback.get("id") or "")
    message = callback.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id") or "")
    message_id = message.get("message_id")

    if not data.startswith("admin:"):
        return
    if chat_id not in TG_SUPER_ADMIN_IDS:
        await bot.answer_callback_query(callback_id, text="Нет доступа.", show_alert=True)
        return
    parts = data.split(":", 2)
    if len(parts) != 3:
        return
    _, action, target = parts
    if action == "pending" and target == "cancel":
        _admin_pending_actions.pop(chat_id, None)
        await bot.answer_callback_query(callback_id, text="Отменено.")
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception:
            pass
        return
    recipients = load_recipients()
    if action == "add":
        recipients.add(target)
        save_recipients(recipients)
        await bot.answer_callback_query(callback_id, text="Добавлен.")
    elif action == "remove":
        recipients.discard(target)
        save_recipients(recipients)
        await bot.answer_callback_query(callback_id, text="Удалён.")
    elif action == "chatid":
        await bot.answer_callback_query(callback_id, text=f"chat_id: {target}", show_alert=True)
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id,
            reply_markup=_onboarding_kb(target, target in recipients)
        )
    except Exception:
        pass

async def polling_loop():
    offset = 0
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=25, allowed_updates=["message", "callback_query"])
            for update in updates:
                offset = update.update_id + 1
                data = update.model_dump()
                if data.get("message"):
                    await handle_tg_message(data["message"])
                if data.get("callback_query"):
                    await handle_tg_callback(data["callback_query"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"telegram polling error: {exc}", flush=True)
            await asyncio.sleep(5)


# ───────────── HTTP handlers ─────────────

def _check_token(request: web.Request) -> bool:
    if not NOTIFY_SECRET:
        return True
    return request.headers.get("X-Notify-Token", "") == NOTIFY_SECRET

def _format_lead(lead: dict) -> str:
    lines = [f"📋 Новая заявка — {SITE_NAME}", ""]
    if lead.get("name"):
        lines.append(f"👤 {lead['name']}")
    if lead.get("contact"):
        lines.append(f"📞 {lead['contact']}")
    if lead.get("email"):
        lines.append(f"✉️  {lead['email']}")
    if lead.get("message"):
        lines += ["", f"💬 {lead['message']}"]
    lines += ["", f"🕐 {lead.get('received_at_human', msk_now())}"]
    if lead.get("page"):
        lines.append(f"🔗 {lead['page']}")
    return "\n".join(lines)

async def lead_handler(request: web.Request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "invalid_payload"}, status=400)

    lead = {
        "name":             safe(payload.get("name"), 200),
        "contact":          safe(payload.get("contact"), 100),
        "email":            safe(payload.get("email"), 200),
        "message":          safe(payload.get("message"), 2000),
        "consent":          bool(payload.get("consent")),
        "page":             safe(payload.get("page"), 500),
        "received_at":      datetime.now(timezone.utc).isoformat(),
        "received_at_human": msk_now(),
        "ip":               request.headers.get("X-Real-IP", request.remote or ""),
    }

    if not lead["name"] and not lead["contact"] and not lead["email"]:
        return web.json_response({"ok": False, "error": "name_or_contact_required"}, status=400)
    if not lead["consent"]:
        return web.json_response({"ok": False, "error": "consent_required"}, status=400)

    try:
        LEADS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LEADS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(lead, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"lead save error: {exc}", flush=True)
        return web.json_response({"ok": False, "error": "save_failed"}, status=500)

    text = _format_lead(lead)
    recipients = sorted(load_recipients())
    asyncio.create_task(tg_broadcast(text, recipients))
    asyncio.create_task(send_email_async(f"Новая заявка — {SITE_NAME}", text))

    return web.json_response({"ok": True})

async def notify_handler(request: web.Request):
    if not _check_token(request):
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    text = safe(payload.get("text"), 4000)
    if not text:
        return web.json_response({"ok": False, "error": "empty_text"}, status=400)

    requested_chat_id = safe(payload.get("chat_id"), 64)
    chat_ids = [requested_chat_id] if requested_chat_id else sorted(load_recipients())
    chat_ids = [c for c in dict.fromkeys(chat_ids) if c]

    sent = 0
    errors: list[str] = []
    if bot:
        for chat_id in chat_ids:
            try:
                await bot.send_message(chat_id, text, disable_web_page_preview=True)
                sent += 1
            except Exception as exc:
                errors.append(f"{chat_id}: {exc}")

    email_errors = await send_email_async(f"Уведомление — {SITE_NAME}", text)
    errors.extend(email_errors)

    return web.json_response({"ok": True, "tg_sent": sent, "errors": errors})

async def health_handler(_: web.Request):
    return web.json_response({
        "ok": True,
        "service": "waves-notify",
        "telegram": bool(bot),
        "email": bool(SMTP_HOST and SMTP_USER),
        "polling": bool(_polling_task and not _polling_task.done()),
        "recipients": len(load_recipients()),
    })


# ───────────── startup / cleanup ─────────────

async def on_startup(_: web.Application):
    global _polling_task
    if not bot:
        return
    startup_msg = f"✅ {SITE_NAME} запущен."
    for chat_id in sorted(load_recipients() | TG_SUPER_ADMIN_IDS):
        try:
            await bot.send_message(chat_id, startup_msg)
        except Exception as exc:
            print(f"startup notify error {chat_id}: {exc}", flush=True)
    if TG_POLLING_ENABLED:
        await bot.delete_webhook(drop_pending_updates=False)
        _polling_task = asyncio.create_task(polling_loop())

async def on_cleanup(_: web.Application):
    if _polling_task:
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
    if bot:
        await bot.session.close()


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_post("/lead", lead_handler)
    app.router.add_post("/notify", notify_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
