#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Welcome Bot — Single File
- Panel quản trị ngay trong Telegram (InlineKeyboard)
- Throttle: mỗi nhóm chỉ gửi 1 lời chào trong cooldown (mặc định 10s)
- Chỉ chào NGƯỜI MỚI NHẤT trong event new_chat_members
- Tự xoá lời chào sau N giây (mặc định 0.1s)
- {tag} mention chắc chắn bằng tg://user?id=...
- NEW: DM_NOTIFY toggle — báo ADMIN khi có người nhắn riêng bot (kể cả /start)
- NEW: START_REPLY — tuỳ chỉnh nội dung bot trả lời riêng cho user khi /start
- Sau khi admin SET_* xong -> tự hiển thị lại panel
- Admin: 7550813603
"""

import os
import json
import time
import asyncio
from typing import Dict, List, Optional, Set

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, User
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters
)

# ======== CẤU HÌNH CƠ BẢN ========
BOT_TOKEN = "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE"   # hoặc đặt trong config.json
ADMIN_ID = 7550813603

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
STATE_PATH  = os.path.join(APP_DIR, "state.json")

DEFAULT_CONFIG = {
    "bot_token": BOT_TOKEN,
    "admin_id": ADMIN_ID,
    "enabled": True,
    "delete_after_seconds": 0.1,     # auto-delete sau 0.1s
    "tag_enabled": True,             # bật/tắt {tag}
    "cooldown_seconds": 10.0,        # THROTTLE: mỗi nhóm 1 lần / 10s
    "dm_notify_enabled": True,       # NEW: bật/tắt báo admin khi có người DM bot
    "start_reply": "👋 Xin chào!",   # NEW: nội dung trả lời riêng khi user /start
    "welcome": {
        "text": "Xin chào {tag} 👋\nChào mừng bạn đến với <b>{chat_title}</b>!",
        "photo_path": ""             # "./welcome.jpg" hoặc URL http(s)
    }
}

# Trạng thái thao tác panel theo bước
# user_id -> "SET_TEXT"|"SET_PHOTO"|"SET_DELAY"|"SET_COOLDOWN"|"SET_REPLYTEXT"
pending_action: Dict[int, str] = {}

# Throttle in-memory
last_sent_at: Dict[int, float] = {}        # chat_id -> last monotonic time sent
chat_locks: Dict[int, asyncio.Lock] = {}   # 1 lock / chat để tránh race

# ========== JSON I/O ==========
def ensure_files():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
    if not os.path.exists(STATE_PATH):
        initial = {
            "welcome_messages": {},      # { "<chat_id>": [message_ids...] }
            "stats": {"total_messages_sent": 0},
            "groups": [],                # chat_id mà bot từng gặp
            "last_group_by_user": {}     # { "<user_id>": {"chat_id": int, "chat_title": str} }
        }
        save_state(initial)

def load_config() -> dict:
    ensure_files()
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def load_state() -> dict:
    ensure_files()
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "welcome_messages": {},
            "stats": {"total_messages_sent": 0},
            "groups": [],
            "last_group_by_user": {}
        }

def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ========== Tiện ích ==========
def build_mention_html(user: User) -> str:
    display = (getattr(user, "full_name", None) or user.first_name or "bạn")
    return f'<a href="tg://user?id={user.id}">{display}</a>'

def build_plain_name(user: User) -> str:
    return (getattr(user, "full_name", None) or user.first_name or "người dùng")

def _render_tag(target_user: User, enable_tag: bool) -> str:
    if enable_tag:
        return build_mention_html(target_user)
    name = f"{target_user.first_name or ''} {target_user.last_name or ''}".strip() or "bạn"
    return name

def format_text(template: str, chat_title: str, target_user: User, tag_enabled: bool) -> str:
    mapping = {
        "first_name": (target_user.first_name or ""),
        "last_name": (target_user.last_name or ""),
        "mention": build_mention_html(target_user),
        "tag": _render_tag(target_user, tag_enabled),
        "chat_title": chat_title or "",
    }
    out = template
    for k, v in mapping.items():
        out = out.replace("{" + k + "}", v)
    return out

def _state_groups_add(chat_id: int):
    st = load_state()
    groups: Set[int] = set(st.get("groups", []))
    if chat_id not in groups:
        groups.add(chat_id)
        st["groups"] = list(groups)
        save_state(st)

def _stats_inc_sent():
    st = load_state()
    st.setdefault("stats", {}).setdefault("total_messages_sent", 0)
    st["stats"]["total_messages_sent"] += 1
    save_state(st)

def _set_user_last_group(user_id: int, chat_id: int, chat_title: str):
    st = load_state()
    st.setdefault("last_group_by_user", {})
    st["last_group_by_user"][str(user_id)] = {"chat_id": chat_id, "chat_title": chat_title}
    save_state(st)

def _get_user_last_group(user_id: int) -> Optional[dict]:
    st = load_state()
    return st.get("last_group_by_user", {}).get(str(user_id))

async def _members_count(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Optional[int]:
    try:
        return await context.bot.get_chat_member_count(chat_id)
    except Exception:
        return None

def _lock_for_chat(chat_id: int) -> asyncio.Lock:
    lock = chat_locks.get(chat_id)
    if not lock:
        lock = asyncio.Lock()
        chat_locks[chat_id] = lock
    return lock

# ========== Quản lý xoá tin ==========
async def purge_old_messages(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    st = load_state()
    arr = st.get("welcome_messages", {}).get(str(chat_id), [])
    if not arr:
        return
    failed = []
    for mid in arr:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            failed.append(mid)
    if failed:
        st["welcome_messages"][str(chat_id)] = failed[-10:]  # giữ ít cho gọn
    else:
        st["welcome_messages"].pop(str(chat_id), None)
    save_state(st)

async def track_message(chat_id: int, message_id: int):
    st = load_state()
    st.setdefault("welcome_messages", {})
    arr = st["welcome_messages"].get(str(chat_id), [])
    arr.append(message_id)
    st["welcome_messages"][str(chat_id)] = arr[-20:]
    save_state(st)

# ========== Gửi chào + auto-delete ==========
async def send_and_schedule_delete(
    chat_id: int,
    chat_title: str,
    target_user: User,
    context: ContextTypes.DEFAULT_TYPE
):
    cfg = load_config()
    if not cfg.get("enabled", True):
        return

    text_tpl   = cfg.get("welcome", {}).get("text", "Xin chào {tag}!")
    photo_path = cfg.get("welcome", {}).get("photo_path", "").strip()
    delay      = float(cfg.get("delete_after_seconds", 0.1))
    tag_on     = bool(cfg.get("tag_enabled", True))

    # Xoá lời chào cũ trước khi gửi
    await purge_old_messages(chat_id, context)

    text = format_text(text_tpl, chat_title, target_user, tag_on)

    sent = None
    try:
        if photo_path:
            if photo_path.startswith("http://") or photo_path.startswith("https://"):
                sent = await context.bot.send_photo(
                    chat_id=chat_id, photo=photo_path, caption=text, parse_mode=ParseMode.HTML
                )
            else:
                with open(photo_path, "rb") as f:
                    sent = await context.bot.send_photo(
                        chat_id=chat_id, photo=f, caption=text, parse_mode=ParseMode.HTML
                    )
        else:
            sent = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
    except Exception:
        try:
            sent = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        except Exception:
            return

    if sent:
        await track_message(chat_id, sent.message_id)
        _stats_inc_sent()

        async def delete_later():
            try:
                await asyncio.sleep(delay)
                await context.bot.delete_message(chat_id=chat_id, message_id=sent.message_id)
            except Exception:
                pass

        asyncio.create_task(delete_later())

# ========== Throttle: chỉ 1 lần / 10s mỗi nhóm ==========
def _allowed_to_send_now(chat_id: int, cooldown: float) -> bool:
    now = time.monotonic()
    last = last_sent_at.get(chat_id, 0.0)
    if now - last >= cooldown:
        last_sent_at[chat_id] = now
        return True
    return False

# ========== Panel ==========
def build_panel(cfg: dict) -> InlineKeyboardMarkup:
    enabled   = cfg.get("enabled", True)
    delay     = cfg.get("delete_after_seconds", 0.1)
    tag_on    = cfg.get("tag_enabled", True)
    cooldown  = cfg.get("cooldown_seconds", 10.0)
    dm_on     = cfg.get("dm_notify_enabled", True)
    rows = [
        [
            InlineKeyboardButton("🟢 BẬT" if enabled else "🔴 TẮT", callback_data="TOGGLE_ENABLED"),
            InlineKeyboardButton(f"⏱ Xoá sau: {delay}s", callback_data="SET_DELAY"),
        ],
        [
            InlineKeyboardButton("🏷 TAG: ON" if tag_on else "🏷 TAG: OFF", callback_data="TOGGLE_TAG"),
            InlineKeyboardButton(f"🛑 Cooldown: {cooldown:.1f}s", callback_data="SET_COOLDOWN"),
        ],
        [
            InlineKeyboardButton("🔔 DM_NOTIFY: ON" if dm_on else "🔔 DM_NOTIFY: OFF", callback_data="TOGGLE_DM"),
            InlineKeyboardButton("🗨️ Reply text (/start)", callback_data="SET_REPLYTEXT"),
        ],
        [
            InlineKeyboardButton("📝 Sửa nội dung chào", callback_data="SET_TEXT"),
            InlineKeyboardButton("🖼 Sửa ảnh chào", callback_data="SET_PHOTO"),
        ],
        [
            InlineKeyboardButton("👁 Xem cấu hình", callback_data="SHOW_CFG"),
            InlineKeyboardButton("🧹 Xoá chào cũ (group)", callback_data="CLEAR_WELCOMES"),
        ],
        [
            InlineKeyboardButton("📊 Thống kê", callback_data="SHOW_STATS"),
            InlineKeyboardButton("📤 Preview", callback_data="PREVIEW"),
        ]
    ]
    return InlineKeyboardMarkup(rows)

async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    cfg = load_config()
    await update.message.reply_text("⚙️ Panel quản trị:", reply_markup=build_panel(cfg))

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    q = update.callback_query
    await q.answer()
    cfg = load_config()
    data = q.data

    if data == "TOGGLE_ENABLED":
        cfg["enabled"] = not cfg.get("enabled", True)
        save_config(cfg)
        await q.edit_message_text("⚙️ Panel quản trị:", reply_markup=build_panel(cfg))

    elif data == "TOGGLE_TAG":
        cfg["tag_enabled"] = not cfg.get("tag_enabled", True)
        save_config(cfg)
        await q.edit_message_text("⚙️ Panel quản trị:", reply_markup=build_panel(cfg))

    elif data == "TOGGLE_DM":
        cfg["dm_notify_enabled"] = not cfg.get("dm_notify_enabled", True)
        save_config(cfg)
        await q.edit_message_text("⚙️ Panel quản trị:", reply_markup=build_panel(cfg))

    elif data == "SET_TEXT":
        pending_action[ADMIN_ID] = "SET_TEXT"
        await q.edit_message_text(
            "✍️ Gửi **nội dung chào** mới trong tin nhắn kế tiếp.\n"
            "Biến: {first_name} {last_name} {mention} {tag} {chat_title}",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "SET_REPLYTEXT":
        pending_action[ADMIN_ID] = "SET_REPLYTEXT"
        await q.edit_message_text(
            "🗨️ Gửi **nội dung trả lời /start** cho người dùng (private). Ví dụ: 👋 Xin chào!",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "SET_PHOTO":
        pending_action[ADMIN_ID] = "SET_PHOTO"
        await q.edit_message_text("🖼 Gửi ảnh (upload) hoặc URL http/https.")

    elif data == "SET_DELAY":
        pending_action[ADMIN_ID] = "SET_DELAY"
        await q.edit_message_text("⏱ Gửi số giây tự xoá (vd: 0.1).")

    elif data == "SET_COOLDOWN":
        pending_action[ADMIN_ID] = "SET_COOLDOWN"
        await q.edit_message_text("🛑 Gửi thời gian cooldown (giây) — vd: 10")

    elif data == "SHOW_CFG":
        text = (
            f"<b>enabled</b>: {cfg.get('enabled', True)}\n"
            f"<b>delete_after_seconds</b>: {cfg.get('delete_after_seconds', 0.1)}\n"
            f"<b>tag_enabled</b>: {cfg.get('tag_enabled', True)}\n"
            f"<b>cooldown_seconds</b>: {cfg.get('cooldown_seconds', 10.0)}\n"
            f"<b>dm_notify_enabled</b>: {cfg.get('dm_notify_enabled', True)}\n"
            f"<b>start_reply</b>:\n<pre>{cfg.get('start_reply','')}</pre>\n"
            f"<b>welcome.text</b>:\n<pre>{cfg.get('welcome',{}).get('text','')}</pre>\n"
            f"<b>welcome.photo_path</b>: {cfg.get('welcome',{}).get('photo_path','') or '(không)'}"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=build_panel(cfg))

    elif data == "CLEAR_WELCOMES":
        chat = update.effective_chat
        if chat and chat.type in ("group", "supergroup"):
            await purge_old_messages(chat.id, context)
            await q.edit_message_text("🧹 Đã xoá các lời chào cũ (nếu còn).", reply_markup=build_panel(cfg))
        else:
            await q.edit_message_text("Dùng trong nhóm.", reply_markup=build_panel(cfg))

    elif data == "SHOW_STATS":
        st = load_state()
        total = st.get("stats", {}).get("total_messages_sent", 0)
        groups = st.get("groups", [])
        chat = update.effective_chat
        mc = None
        if chat and chat.type in ("group", "supergroup"):
            mc = await _members_count(context, chat.id)
        text = (
            f"📊 <b>Thống kê</b>\n"
            f"- Tổng lời chào đã gửi: <b>{total}</b>\n"
            f"- Số nhóm đã tham gia: <b>{len(groups)}</b>\n"
            f"- Số thành viên nhóm hiện tại: <b>{mc if mc is not None else 'n/a'}</b>"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=build_panel(cfg))

    elif data == "PREVIEW":
        text_tpl = cfg.get("welcome", {}).get("text", "Xin chào {tag}!")
        tag_on = bool(cfg.get("tag_enabled", True))
        dummy_user = update.effective_user
        txt = format_text(text_tpl, "Preview Chat", dummy_user, tag_on)
        photo_path = cfg.get("welcome", {}).get("photo_path", "").strip()
        try:
            if photo_path:
                if photo_path.startswith("http://") or photo_path.startswith("https://"):
                    await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo_path, caption=txt, parse_mode=ParseMode.HTML)
                else:
                    with open(photo_path, "rb") as f:
                        await context.bot.send_photo(chat_id=ADMIN_ID, photo=f, caption=txt, parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_message(chat_id=ADMIN_ID, text=txt, parse_mode=ParseMode.HTML)
        except Exception:
            await context.bot.send_message(chat_id=ADMIN_ID, text="(Không thể gửi ảnh preview — thử đường dẫn/URL khác)")
        await q.edit_message_text("✅ Đã gửi preview vào private chat.", reply_markup=build_panel(cfg))

# ========== Xử lý input admin (và tự hiện lại panel) ==========
async def _return_panel(context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text="⚙️ Panel quản trị:", reply_markup=build_panel(cfg))
    except Exception:
        pass

async def on_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    action = pending_action.get(ADMIN_ID)
    if not action:
        return
    cfg = load_config()

    if action == "SET_TEXT":
        if update.message and update.message.text:
            cfg.setdefault("welcome", {})["text"] = update.message.text.strip()
            save_config(cfg)
            await update.message.reply_text("✅ Đã cập nhật nội dung chào.")
            pending_action.pop(ADMIN_ID, None)
            await _return_panel(context)

    elif action == "SET_REPLYTEXT":
        if update.message and update.message.text:
            cfg["start_reply"] = update.message.text.strip()
            save_config(cfg)
            await update.message.reply_text("✅ Đã cập nhật nội dung trả lời /start.")
            pending_action.pop(ADMIN_ID, None)
            await _return_panel(context)

    elif action == "SET_PHOTO":
        if update.message.photo:
            file = await update.message.photo[-1].get_file()
            local_path = os.path.join(APP_DIR, "welcome.jpg")
            await file.download_to_drive(local_path)
            cfg.setdefault("welcome", {})["photo_path"] = local_path
            save_config(cfg)
            await update.message.reply_text(f"✅ Đã cập nhật ảnh chào: {local_path}")
            pending_action.pop(ADMIN_ID, None)
            await _return_panel(context)
        elif update.message.text:
            url = update.message.text.strip()
            if url.startswith("http://") or url.startswith("https://"):
                cfg.setdefault("welcome", {})["photo_path"] = url
                save_config(cfg)
                await update.message.reply_text(f"✅ Đã cập nhật ảnh chào (URL): {url}")
                pending_action.pop(ADMIN_ID, None)
                await _return_panel(context)
            else:
                await update.message.reply_text("❌ Không hợp lệ. Gửi ảnh hoặc URL http/https.")
        else:
            await update.message.reply_text("❌ Vui lòng gửi ảnh hoặc URL ảnh.")

    elif action == "SET_DELAY":
        if update.message and update.message.text:
            try:
                seconds = float(update.message.text.strip())
                if seconds < 0:
                    await update.message.reply_text("❌ Không âm. Ví dụ 0.1")
                    return
                cfg["delete_after_seconds"] = seconds
                save_config(cfg)
                await update.message.reply_text(f"✅ Đã cập nhật thời gian tự xoá: {seconds}s")
                pending_action.pop(ADMIN_ID, None)
                await _return_panel(context)
            except ValueError:
                await update.message.reply_text("❌ Không phải số. Ví dụ 0.1")

    elif action == "SET_COOLDOWN":
        if update.message and update.message.text:
            try:
                seconds = float(update.message.text.strip())
                if seconds < 0:
                    await update.message.reply_text("❌ Không âm. Ví dụ 10")
                    return
                cfg["cooldown_seconds"] = seconds
                save_config(cfg)
                await update.message.reply_text(f"✅ Đã cập nhật cooldown: {seconds:.1f}s")
                pending_action.pop(ADMIN_ID, None)
                await _return_panel(context)
            except ValueError:
                await update.message.reply_text("❌ Không phải số. Ví dụ 10")

# ========== Sự kiện: thành viên mới ==========
async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return

    chat = update.effective_chat
    chat_id = chat.id
    chat_title = (chat.title or chat.full_name or "")
    _state_groups_add(chat_id)

    # Ghi nhận nhóm gần nhất cho tất cả người vừa tham gia
    for u in update.message.new_chat_members:
        _set_user_last_group(u.id, chat_id, chat_title)

    # Chỉ chào NGƯỜI MỚI NHẤT trong event này + throttle
    latest_user = update.message.new_chat_members[-1]
    cfg = load_config()
    cooldown = float(cfg.get("cooldown_seconds", 10.0))

    lock = _lock_for_chat(chat_id)
    async with lock:
        if not _allowed_to_send_now(chat_id, cooldown):
            return
        await send_and_schedule_delete(chat_id, chat_title, latest_user, context)

## NEW: Notify ADMIN khi non-admin nhắn riêng (DÙNG TAG)
async def notify_admin_of_dm(user: User, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    if not cfg.get("dm_notify_enabled", True):
        return
    last_group = _get_user_last_group(user.id)
    group_title = last_group["chat_title"] if last_group else "(chưa xác định)"
    # DÙNG TAG chắc chắn bằng tg://user?id=...
    name_html = build_mention_html(user)
    text = f"🔔 Có người: {name_html} ở nhóm: <b>{group_title}</b> đã nhắn với bot"
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


# Bắt mọi private message từ non-admin (kể cả /start, text, sticker, ảnh...)
async def on_private_message_from_non_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if update.effective_user and update.effective_user.id == ADMIN_ID:
        return
    await notify_admin_of_dm(update.effective_user, context)

# ========== Lệnh cơ bản ==========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and user.id == ADMIN_ID:
        await update.message.reply_text("✅ Bot chạy OK. Gõ /panel để mở menu quản trị.")
        return

    # Non-admin: trả lời theo cấu hình + notify admin
    cfg = load_config()
    reply = cfg.get("start_reply", "👋 Xin chào!")
    try:
        await update.message.reply_text(reply)
    except Exception:
        pass
    # thông báo admin
    await notify_admin_of_dm(user, context)

async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    cfg = load_config()
    await update.message.reply_text("⚙️ Panel quản trị:", reply_markup=build_panel(cfg))

async def cmd_clearwelcomes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        await purge_old_messages(chat.id, context)
        await update.message.reply_text("🧹 Đã xoá các lời chào cũ (nếu còn).")
    else:
        await update.message.reply_text("Lệnh này dùng trong nhóm.")

# ========== MAIN ==========
def main():
    cfg = load_config()
    token = (cfg.get("bot_token") or BOT_TOKEN).strip()
    if not token or token == "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE":
        raise SystemExit("❌ Hãy đặt BOT_TOKEN ở đầu file hoặc trong config.json")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(CommandHandler("clearwelcomes", cmd_clearwelcomes))

    # Input từ admin (PRIVATE): set text/photo/delay/cooldown/replytext
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.User(user_id=ADMIN_ID) & (filters.TEXT | filters.PHOTO),
        on_admin_message
    ))

    # Private messages từ người KHÔNG phải admin -> báo admin
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.User(user_id=ADMIN_ID),
        on_private_message_from_non_admin
    ))

    # Thành viên mới — áp dụng throttle 10s / group
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))

    print("🤖 Bot started. Ctrl+C to stop.")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
