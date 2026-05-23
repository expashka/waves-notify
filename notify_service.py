#!/usr/bin/env python3
"""waves-notify — universal lead notification service.

Channels: Telegram bot + Email (SMTP). Both are optional and independent.

Endpoints:
  POST /lead      — receive form submission, save to JSONL, notify all channels
  POST /notify    — send arbitrary text (internal use, requires NOTIFY_SECRET token)
  GET  /health    — service health

Bot flow:
  Staff:  /start → request notifications → admin approves TG → admin requests email
          → staff enters email → receives confirmation code → enters code → done
  Admin:  approve/decline join requests, request email from staff, manage recipients
"""

import asyncio
import json
import os
import random
import smtplib
import ssl
import string
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

SITE_NAME     = os.getenv("SITE_NAME", "waves-notify").strip()
PORT          = int(os.getenv("PORT", "8080"))
NOTIFY_SECRET = os.getenv("NOTIFY_SECRET", "").strip()
LEADS_FILE    = Path(os.getenv("LEADS_FILE") or "/data/leads.jsonl")

TG_BOT_TOKEN       = os.getenv("TG_BOT_TOKEN", "").strip()
TG_ADMIN_CHAT_ID   = os.getenv("TG_ADMIN_CHAT_ID", "").strip()
TG_SUPER_ADMIN_IDS = _parse_ids(os.getenv("TG_SUPER_ADMIN_IDS", ""))
TG_POLLING_ENABLED = os.getenv("TG_POLLING_ENABLED", "1").strip() != "0"
RECIPIENTS_FILE    = Path(os.getenv("RECIPIENTS_FILE") or "/data/recipients.json")

if TG_ADMIN_CHAT_ID:
    TG_SUPER_ADMIN_IDS.add(TG_ADMIN_CHAT_ID)

_SMTP_PRESETS: dict[str, tuple[str, int, bool]] = {
    "yandex": ("smtp.yandex.ru", 465, True),
    "mailru":  ("smtp.mail.ru",   465, True),
    "gmail":   ("smtp.gmail.com", 465, True),
}
SMTP_USER     = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_FROM     = os.getenv("SMTP_FROM", SMTP_USER).strip()
SMTP_TO_EXTRA = _parse_ids(os.getenv("SMTP_TO", ""))  # fixed extra recipients
_provider     = os.getenv("SMTP_PROVIDER", "").strip().lower()
_preset       = _SMTP_PRESETS.get(_provider, ("", 465, True))
SMTP_HOST     = os.getenv("SMTP_HOST", _preset[0]).strip()
SMTP_PORT     = int(os.getenv("SMTP_PORT", str(_preset[1])))
SMTP_SSL      = os.getenv("SMTP_SSL", "1" if _preset[2] else "0").strip() != "0"

EMAIL_CODE_TTL = int(os.getenv("EMAIL_CODE_TTL", "600"))  # seconds


# ───────────── Telegram setup ─────────────

bot: Any = None
_polling_task: asyncio.Task | None = None

if AIOGRAM_AVAILABLE and TG_BOT_TOKEN:
    bot = Bot(TG_BOT_TOKEN)
elif not TG_BOT_TOKEN:
    print("⚠  TG_BOT_TOKEN not set — Telegram disabled", flush=True)
elif not AIOGRAM_AVAILABLE:
    print("⚠  aiogram not installed — Telegram disabled", flush=True)


# ───────────── helpers ─────────────

def msk_now() -> str:
    return datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M МСК")

def safe(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]

def normalize_chat_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("-"):
        return text if text[1:].isdigit() else ""
    return text if text.isdigit() else ""

def is_valid_email(value: str) -> bool:
    import re
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value.strip()))

def generate_code() -> str:
    return "".join(random.choices(string.digits, k=6))

def display_name(r: dict) -> str:
    name = r.get("name") or ""
    username = r.get("username") or ""
    parts = [name or "Без имени"]
    if username:
        parts.append(f"@{username}")
    return " · ".join(parts)


# ───────────── recipients storage ─────────────
# Format: list of dicts
# { "telegram_id": "123", "name": "Ivan", "username": "ivan", "email": "ivan@mail.ru",
#   "channels": ["telegram", "email"] }

def load_recipients() -> list[dict]:
    try:
        if RECIPIENTS_FILE.exists():
            data = json.loads(RECIPIENTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []

def save_recipients(recipients: list[dict]):
    RECIPIENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECIPIENTS_FILE.write_text(
        json.dumps(recipients, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def find_recipient(telegram_id: str) -> dict | None:
    for r in load_recipients():
        if str(r.get("telegram_id") or "") == telegram_id:
            return r
    return None

def upsert_recipient(telegram_id: str, **fields):
    recipients = load_recipients()
    for r in recipients:
        if str(r.get("telegram_id") or "") == telegram_id:
            r.update(fields)
            save_recipients(recipients)
            return
    entry: dict = {"telegram_id": telegram_id, "channels": ["telegram"], **fields}
    recipients.append(entry)
    save_recipients(recipients)

def remove_recipient(telegram_id: str):
    recipients = [r for r in load_recipients() if str(r.get("telegram_id") or "") != telegram_id]
    save_recipients(recipients)

def tg_chat_ids_for_leads() -> list[str]:
    ids = []
    for r in load_recipients():
        if "telegram" in r.get("channels", []) and r.get("telegram_id"):
            ids.append(str(r["telegram_id"]))
    # also include admins
    for aid in sorted(TG_SUPER_ADMIN_IDS):
        if aid not in ids:
            ids.append(aid)
    return ids

def email_addresses_for_leads() -> list[str]:
    emails = list(SMTP_TO_EXTRA)
    for r in load_recipients():
        if "email" in r.get("channels", []) and r.get("email"):
            em = str(r["email"]).strip()
            if em and em not in emails:
                emails.append(em)
    return emails


# ───────────── pending email confirmations ─────────────
# chat_id → { "email": str, "code": str, "expires": float }
_pending_email: dict[str, dict] = {}

# chat_id → what we're waiting for: "email_input"
_waiting_input: dict[str, str] = {}


# ───────────── email sending ─────────────

def send_email(to: list[str], subject: str, body: str) -> list[str]:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD) or not to:
        return []
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(to)
    try:
        if SMTP_SSL:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM, to, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM, to, msg.as_string())
    except Exception as exc:
        print(f"email error: {exc}", flush=True)
        return [str(exc)]
    return []

async def send_email_async(to: list[str], subject: str, body: str) -> list[str]:
    return await asyncio.get_event_loop().run_in_executor(None, send_email, to, subject, body)

async def send_confirmation_code(chat_id: str, email: str) -> bool:
    code = generate_code()
    _pending_email[chat_id] = {
        "email": email,
        "code": code,
        "expires": datetime.now(timezone.utc).timestamp() + EMAIL_CODE_TTL,
    }
    subject = f"Подтверждение email — {SITE_NAME}"
    body = (
        f"Ваш код подтверждения: {code}\n\n"
        f"Введите его в Telegram-боте {SITE_NAME}.\n"
        f"Код действителен {EMAIL_CODE_TTL // 60} минут."
    )
    errors = await send_email_async([email], subject, body)
    return len(errors) == 0


# ───────────── Telegram keyboards ─────────────

def kb_user_start() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔔 Запросить уведомления", callback_data="user:request")
    ]])

def kb_admin_join_request(requester_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подключить",  callback_data=f"admin:approve:{requester_id}"),
        InlineKeyboardButton(text="❌ Отклонить",   callback_data=f"admin:decline:{requester_id}"),
    ]])

def kb_admin_after_approve(telegram_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📧 Запросить email", callback_data=f"admin:request_email:{telegram_id}"),
    ]])

def kb_admin_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Получатели")],
            [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="➖ Удалить")],
            [KeyboardButton(text="🆔 Мой ID")],
        ],
        resize_keyboard=True,
    )

def kb_cancel_input() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Отмена", callback_data="user:cancel_input")
    ]])

def kb_resend_code(email: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Отправить код снова", callback_data="user:resend_code"),
        InlineKeyboardButton(text="Отмена", callback_data="user:cancel_input"),
    ]])

ADMIN_BUTTONS = {"👥 Получатели", "➕ Добавить", "➖ Удалить", "🆔 Мой ID"}


# ───────────── Telegram broadcast ─────────────

async def tg_broadcast(text: str, chat_ids: list[str]):
    if not bot:
        return
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, text, disable_web_page_preview=True)
        except Exception as exc:
            print(f"tg send error {chat_id}: {exc}", flush=True)

async def format_recipients_list() -> str:
    recipients = load_recipients()
    if not recipients:
        return "Список получателей пуст."
    lines = [f"Получателей: {len(recipients)}\n"]
    for r in recipients:
        name = display_name(r)
        channels = []
        if "telegram" in r.get("channels", []):
            channels.append("TG")
        if "email" in r.get("channels", []):
            channels.append(f"✉️ {r.get('email', '')}")
        ch_str = " · ".join(channels) if channels else "нет каналов"
        lines.append(f"• {name}\n  {ch_str}")
    return "\n".join(lines)


# ───────────── Telegram message handler ─────────────

async def handle_tg_message(message: dict):
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    chat_id   = str(chat.get("id") or "")
    text      = str(message.get("text") or "").strip()
    is_admin  = chat_id in TG_SUPER_ADMIN_IDS

    if not chat_id or not text:
        return

    # ── handle pending input (email or chat_id) ──
    waiting = _waiting_input.get(chat_id)

    if waiting and (text.startswith("/") or text in ADMIN_BUTTONS):
        _waiting_input.pop(chat_id, None)
        _pending_email.pop(chat_id, None)

    elif waiting == "email_input":
        email = text.strip()
        if not is_valid_email(email):
            await bot.send_message(
                chat_id,
                "❌ Некорректный email. Попробуйте ещё раз:",
                reply_markup=kb_cancel_input(),
            )
            return
        await bot.send_message(chat_id, f"⏳ Отправляю код на {email}…")
        ok = await send_confirmation_code(chat_id, email)
        if not ok:
            await bot.send_message(
                chat_id,
                "❌ Не удалось отправить письмо. Проверьте адрес или попробуйте позже.",
                reply_markup=kb_cancel_input(),
            )
            return
        _waiting_input[chat_id] = "email_code"
        await bot.send_message(
            chat_id,
            f"📬 Код отправлен на {email}.\n\nВведите 6-значный код из письма:",
            reply_markup=kb_resend_code(email),
        )
        return

    elif waiting == "email_code":
        pending = _pending_email.get(chat_id)
        if not pending:
            _waiting_input.pop(chat_id, None)
            await bot.send_message(chat_id, "Сессия истекла. Начните заново.")
            return
        if datetime.now(timezone.utc).timestamp() > pending["expires"]:
            _waiting_input.pop(chat_id, None)
            _pending_email.pop(chat_id, None)
            await bot.send_message(chat_id, "⏰ Код устарел. Начните заново.")
            return
        if text.strip() != pending["code"]:
            await bot.send_message(
                chat_id,
                "❌ Неверный код. Попробуйте ещё раз:",
                reply_markup=kb_resend_code(pending["email"]),
            )
            return
        # Code correct — save email
        email = pending["email"]
        _waiting_input.pop(chat_id, None)
        _pending_email.pop(chat_id, None)
        r = find_recipient(chat_id)
        if r:
            r["email"] = email
            if "email" not in r.get("channels", []):
                r.setdefault("channels", []).append("email")
            recipients = load_recipients()
            save_recipients(recipients)
        else:
            upsert_recipient(chat_id, email=email, channels=["telegram", "email"],
                             name=str(user.get("first_name") or "").strip(),
                             username=str(user.get("username") or "").strip())
        await bot.send_message(chat_id, f"✅ Email {email} подтверждён и подключён.")
        # Notify admins
        name = display_name(find_recipient(chat_id) or {})
        for admin_id in sorted(TG_SUPER_ADMIN_IDS):
            try:
                await bot.send_message(admin_id, f"✅ {name} подтвердил email: {email}")
            except Exception:
                pass
        return

    elif waiting == "add_chat_id" and is_admin:
        target = normalize_chat_id(text)
        if not target:
            await bot.send_message(chat_id, "Неверный формат. Введите числовой chat_id:", reply_markup=kb_cancel_input())
            return
        _waiting_input.pop(chat_id, None)
        upsert_recipient(target)
        await bot.send_message(chat_id, f"✅ Добавлен получатель с ID: {target}", reply_markup=kb_admin_main())
        try:
            await bot.send_message(target, f"✅ Вы добавлены в рассылку {SITE_NAME}.")
        except Exception:
            pass
        return

    elif waiting == "remove_chat_id" and is_admin:
        target = normalize_chat_id(text)
        if not target:
            await bot.send_message(chat_id, "Неверный формат:", reply_markup=kb_cancel_input())
            return
        _waiting_input.pop(chat_id, None)
        remove_recipient(target)
        await bot.send_message(chat_id, f"Удалён получатель с ID: {target}", reply_markup=kb_admin_main())
        try:
            await bot.send_message(target, f"ℹ️ Вы удалены из рассылки {SITE_NAME}.")
        except Exception:
            pass
        return

    # ── commands ──
    if text == "/start":
        if is_admin:
            await bot.send_message(chat_id, f"👋 Панель управления {SITE_NAME}", reply_markup=kb_admin_main())
        else:
            r = find_recipient(chat_id)
            if r:
                ch = r.get("channels", [])
                status_parts = []
                if "telegram" in ch:
                    status_parts.append("Telegram ✅")
                if "email" in ch:
                    status_parts.append(f"Email ✅ ({r.get('email', '')})")
                await bot.send_message(chat_id, f"Вы подключены к уведомлениям.\n\n" + "\n".join(status_parts))
            else:
                await bot.send_message(
                    chat_id,
                    f"👋 Привет!\n\nЭтот бот отправляет уведомления от {SITE_NAME}.\n\nНажмите кнопку, чтобы запросить доступ:",
                    reply_markup=kb_user_start(),
                )
        return

    if text in ("/id", "/chat_id"):
        await bot.send_message(chat_id, f"Ваш ID: `{chat_id}`", parse_mode="Markdown")
        return

    if not is_admin:
        return

    # ── admin menu ──
    if text == "🆔 Мой ID":
        await bot.send_message(chat_id, f"Ваш ID: `{chat_id}`", parse_mode="Markdown")
        return

    if text == "👥 Получатели":
        await bot.send_message(chat_id, await format_recipients_list())
        return

    if text == "➕ Добавить":
        _waiting_input[chat_id] = "add_chat_id"
        await bot.send_message(chat_id, "Введите chat_id пользователя:", reply_markup=kb_cancel_input())
        return

    if text == "➖ Удалить":
        _waiting_input[chat_id] = "remove_chat_id"
        await bot.send_message(chat_id, "Введите chat_id для удаления:", reply_markup=kb_cancel_input())
        return


# ───────────── Telegram callback handler ─────────────

async def handle_tg_callback(callback: dict):
    data        = str(callback.get("data") or "")
    callback_id = str(callback.get("id") or "")
    message     = callback.get("message") or {}
    chat        = message.get("chat") or {}
    chat_id     = str(chat.get("id") or "")
    message_id  = message.get("message_id")
    from_user   = callback.get("from_user") or callback.get("from") or {}
    is_admin    = chat_id in TG_SUPER_ADMIN_IDS

    async def edit_text(new_text: str):
        try:
            await bot.edit_message_text(new_text, chat_id=chat_id, message_id=message_id)
        except Exception:
            pass

    # ── user actions ──
    if data == "user:request":
        if find_recipient(chat_id):
            await bot.answer_callback_query(callback_id, text="Вы уже в списке рассылки.")
            return
        name = str(from_user.get("first_name") or "").strip()
        username = str(from_user.get("username") or "").strip()
        label = " · ".join(filter(None, [name or "Без имени", f"@{username}" if username else "", f"ID: {chat_id}"]))
        for admin_id in sorted(TG_SUPER_ADMIN_IDS):
            try:
                await bot.send_message(
                    admin_id,
                    f"🔔 Запрос на уведомления\n\n👤 {label}\n\nПодключить?",
                    reply_markup=kb_admin_join_request(chat_id),
                )
            except Exception as exc:
                print(f"tg admin notify error {admin_id}: {exc}", flush=True)
        await bot.answer_callback_query(callback_id)
        await edit_text("⏳ Запрос отправлен. Ожидайте подтверждения администратора.")
        return

    if data == "user:cancel_input":
        _waiting_input.pop(chat_id, None)
        _pending_email.pop(chat_id, None)
        await bot.answer_callback_query(callback_id, text="Отменено.")
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception:
            pass
        return

    if data == "user:resend_code":
        pending = _pending_email.get(chat_id)
        if not pending:
            await bot.answer_callback_query(callback_id, text="Сессия истекла. Начните заново.", show_alert=True)
            return
        ok = await send_confirmation_code(chat_id, pending["email"])
        if ok:
            await bot.answer_callback_query(callback_id, text="Код отправлен повторно.")
        else:
            await bot.answer_callback_query(callback_id, text="Не удалось отправить письмо.", show_alert=True)
        return

    # ── admin actions ──
    if not data.startswith("admin:"):
        return

    if not is_admin:
        await bot.answer_callback_query(callback_id, text="Нет доступа.", show_alert=True)
        return

    parts = data.split(":", 2)
    if len(parts) != 3:
        return
    _, action, target = parts

    if action == "approve":
        from_info = callback.get("from_user") or callback.get("from") or {}
        # Try to get user info from original message text
        upsert_recipient(target, channels=["telegram"])
        await bot.answer_callback_query(callback_id, text="Telegram подключён.")
        orig_text = message.get("text") or ""
        await edit_text(orig_text + "\n\n✅ Telegram подключён")
        try:
            await bot.send_message(
                target,
                f"✅ Вы подключены к уведомлениям {SITE_NAME} через Telegram.",
                reply_markup=kb_admin_join_request.__class__,  # clear kb
            )
        except Exception:
            pass
        # Offer admin to request email
        await bot.send_message(
            chat_id,
            f"Хотите также подключить email для этого получателя?",
            reply_markup=kb_admin_after_approve(target),
        )
        return

    if action == "decline":
        await bot.answer_callback_query(callback_id, text="Отклонено.")
        orig_text = message.get("text") or ""
        await edit_text(orig_text + "\n\n❌ Отклонено")
        try:
            await bot.send_message(target, "Администратор отклонил ваш запрос.")
        except Exception:
            pass
        return

    if action == "request_email":
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception:
            pass
        await bot.answer_callback_query(callback_id)
        try:
            await bot.send_message(
                target,
                f"📧 Администратор предлагает также подключить уведомления на email.\n\nВведите ваш email:",
                reply_markup=kb_cancel_input(),
            )
            _waiting_input[target] = "email_input"
        except Exception as exc:
            await bot.send_message(chat_id, f"Не удалось отправить запрос пользователю: {exc}")
        return


# ───────────── Telegram polling ─────────────

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
            print(f"tg polling error: {exc}", flush=True)
            await asyncio.sleep(5)


# ───────────── HTTP handlers ─────────────

def _check_token(request: web.Request) -> bool:
    if not NOTIFY_SECRET:
        return True
    return request.headers.get("X-Notify-Token", "") == NOTIFY_SECRET

def _format_lead(lead: dict) -> str:
    lines = [f"📋 Новая заявка — {SITE_NAME}", ""]
    if lead.get("name"):    lines.append(f"👤 {lead['name']}")
    if lead.get("contact"): lines.append(f"📞 {lead['contact']}")
    if lead.get("email"):   lines.append(f"✉️  {lead['email']}")
    if lead.get("message"): lines += ["", f"💬 {lead['message']}"]
    lines += ["", f"🕐 {lead.get('received_at_human', msk_now())}"]
    if lead.get("page"):    lines.append(f"🔗 {lead['page']}")
    return "\n".join(lines)

async def lead_handler(request: web.Request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "invalid_payload"}, status=400)

    lead = {
        "name":              safe(payload.get("name"), 200),
        "contact":           safe(payload.get("contact"), 100),
        "email":             safe(payload.get("email"), 200),
        "message":           safe(payload.get("message"), 2000),
        "consent":           bool(payload.get("consent")),
        "page":              safe(payload.get("page"), 500),
        "received_at":       datetime.now(timezone.utc).isoformat(),
        "received_at_human": msk_now(),
        "ip":                request.headers.get("X-Real-IP", request.remote or ""),
    }
    if not any([lead["name"], lead["contact"], lead["email"]]):
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
    asyncio.create_task(tg_broadcast(text, tg_chat_ids_for_leads()))
    asyncio.create_task(send_email_async(email_addresses_for_leads(), f"Новая заявка — {SITE_NAME}", text))

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

    requested_id = safe(payload.get("chat_id"), 64)
    chat_ids = [requested_id] if requested_id else tg_chat_ids_for_leads()

    tg_sent, errors = 0, []
    if bot:
        for cid in chat_ids:
            try:
                await bot.send_message(cid, text, disable_web_page_preview=True)
                tg_sent += 1
            except Exception as exc:
                errors.append(str(exc))

    email_errors = await send_email_async(email_addresses_for_leads(), f"Уведомление — {SITE_NAME}", text)
    errors.extend(email_errors)

    return web.json_response({"ok": True, "tg_sent": tg_sent, "errors": errors})

async def health_handler(_: web.Request):
    return web.json_response({
        "ok":         True,
        "service":    "waves-notify",
        "telegram":   bool(bot),
        "email":      bool(SMTP_HOST and SMTP_USER),
        "polling":    bool(_polling_task and not _polling_task.done()),
        "recipients": len(load_recipients()),
    })


# ───────────── app lifecycle ─────────────

async def on_startup(_: web.Application):
    global _polling_task
    if not bot:
        return
    for admin_id in sorted(TG_SUPER_ADMIN_IDS):
        try:
            await bot.send_message(admin_id, f"✅ {SITE_NAME} запущен.")
        except Exception as exc:
            print(f"startup notify error {admin_id}: {exc}", flush=True)
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
