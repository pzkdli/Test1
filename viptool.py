#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Manager Bot — Quản lý bot con (1 file, Termux/Ubuntu friendly)
- Admin chính: 7550813603
- Tính năng:
  * Sub-admin theo NGÀY (0 = vĩnh viễn), quota số bot
  * Tạo bot con → sinh folder + welcome_bot_single.py + join.py + config.json
  * Tự chạy 2 tiến trình con & auto-restart (supervisor bằng thread + subprocess)
  * Khởi động -> bootstrap lại bot con còn hạn; enforcer định kỳ stop/start theo hạn
  * Panel bán (toggle & soạn nội dung)
  * /id (in nghiêng), /vps (CPU/RAM/Disk), /huongdan
  * Log rõ ràng để debug khi bot phụ không chạy
"""

import os, sys, json, time, shutil, subprocess, importlib.util, signal, threading
from typing import Dict, Optional, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ======================== CẤU HÌNH ========================
BOT_TOKEN = "8442522633:AAHuI8o-OQXQHlvH2sGRQzRzPItESX9KoNc"  # BOT QUẢN LÝ
MAIN_ADMIN_ID = 7550813603                                     # ADMIN CHÍNH

APP_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(APP_DIR, "manager_data")
BOTS_DIR  = os.path.join(APP_DIR, "bots")
STATE_FP  = os.path.join(DATA_DIR, "manager_state.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BOTS_DIR, exist_ok=True)

DEFAULT_STATE = {
    "sub_admins": {},   # "uid_str": {"expires_at": epoch|0, "quota": 1}
    "bots": [],         # {"id": label, "owner_id": int, "folder": str, "token_masked": str, "created_at": ts}
    "sale": {"enabled": False, "text": "Vui lòng liên hệ admin để mua key/bot."},
    "non_admin_reply": "Xin chào! Đây là bot quản lý. Nhấn nút bên dưới nếu bạn muốn mua key/bot."
}

# ======================== TEMPLATE BOT CON ========================
WELCOME_BOT_TEMPLATE = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sub Bot — welcome + panel + throttle + auto-delete + notify owner + /id
"""
import os, json, time, asyncio
from typing import Dict
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, User
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

BOT_TOKEN = "__BOT_TOKEN__"
ADMIN_ID  = __ADMIN_ID__          # chủ bot phụ
MAIN_ADMIN_ID = __MAIN_ADMIN_ID__ # admin chính (cũng có quyền)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
STATE_PATH  = os.path.join(APP_DIR, "state.json")

DEFAULT_CONFIG = {
    "bot_token": "__BOT_TOKEN__",
    "admin_id": __ADMIN_ID__,
    "enabled": True,
    "delete_after_seconds": 0.1,
    "tag_enabled": True,
    "cooldown_seconds": 10.0,
    "dm_notify_enabled": True,
    "start_reply": "👋 Xin chào!",
    "welcome": {"text": "Xin chào {tag} 👋\nChào mừng bạn đến với <b>{chat_title}</b>!", "photo_path": ""}
}
pending_action: Dict[int, str] = {}
last_sent_at: Dict[int, float] = {}
chat_locks: Dict[int, asyncio.Lock] = {}

def _is_owner(uid: int) -> bool:
    return uid in (ADMIN_ID, MAIN_ADMIN_ID)

def ensure_files():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
    if not os.path.exists(STATE_PATH):
        save_state({"welcome_messages": {}, "stats": {"total_messages_sent": 0}, "groups": [], "last_group_by_user": {}})

def load_config() -> dict:
    ensure_files()
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)
def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
def load_state() -> dict:
    ensure_files()
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)
def save_state(st: dict):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

async def notify_owner(context: ContextTypes.DEFAULT_TYPE, text_html: str):
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text_html, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        pass

def mention(u: 'User') -> str:
    name = (getattr(u, "full_name", None) or u.first_name or "bạn")
    return f'<a href="tg://user?id={u.id}">{name}</a>'

def tag_or_name(u: 'User', on: bool) -> str:
    return mention(u) if on else (u.first_name or "bạn")

def fmt_text(tpl: str, chat_title: str, u: 'User', tag_on: bool) -> str:
    return (tpl
        .replace("{first_name}", u.first_name or "")
        .replace("{last_name}", u.last_name or "")
        .replace("{mention}", mention(u))
        .replace("{tag}", tag_or_name(u, tag_on))
        .replace("{chat_title}", chat_title or ""))

def lock_for_chat(cid: int) -> asyncio.Lock:
    if cid not in chat_locks:
        chat_locks[cid] = asyncio.Lock()
    return chat_locks[cid]

async def purge_old(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    st = load_state()
    arr = st.get("welcome_messages", {}).get(str(chat_id), [])
    if not arr: return
    keep = []
    for mid in arr:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            keep.append(mid)
    if keep:
        st["welcome_messages"][str(chat_id)] = keep[-10:]
    else:
        st["welcome_messages"].pop(str(chat_id), None)
    save_state(st)

async def track_msg(chat_id: int, mid: int):
    st = load_state()
    arr = st.get("welcome_messages", {}).get(str(chat_id), [])
    arr.append(mid)
    st.setdefault("welcome_messages", {})[str(chat_id)] = arr[-20:]
    save_state(st)

def allowed_now(chat_id: int, cooldown: float) -> bool:
    now = time.monotonic()
    last = last_sent_at.get(chat_id, 0.0)
    if now - last >= cooldown:
        last_sent_at[chat_id] = now
        return True
    return False

async def send_and_autodel(chat_id: int, chat_title: str, user: 'User', ctx: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    if not cfg.get("enabled", True): return
    text_tpl   = cfg["welcome"]["text"]
    photo_path = cfg["welcome"]["photo_path"].strip()
    delay      = float(cfg["delete_after_seconds"])
    tag_on     = bool(cfg["tag_enabled"])

    await purge_old(chat_id, ctx)
    text = fmt_text(text_tpl, chat_title, user, tag_on)

    try:
        if photo_path:
            if photo_path.startswith("http"):
                msg = await ctx.bot.send_photo(chat_id=chat_id, photo=photo_path, caption=text, parse_mode=ParseMode.HTML)
            else:
                with open(photo_path, "rb") as f:
                    msg = await ctx.bot.send_photo(chat_id=chat_id, photo=f, caption=text, parse_mode=ParseMode.HTML)
        else:
            msg = await ctx.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await notify_owner(ctx, f"⚠️ Lỗi gửi chào ở <b>{chat_title}</b>: <code>{e}</code>")
        return

    await track_msg(chat_id, msg.message_id)
    await notify_owner(ctx, f"🆕 Vừa chào {mention(user)} tại nhóm <b>{chat_title or chat_id}</b>.")

    async def _del():
        try:
            await asyncio.sleep(delay)
            await ctx.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
        except Exception:
            pass
    asyncio.create_task(_del())

async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members: return
    chat   = update.effective_chat
    title  = (chat.title or chat.full_name or "")
    cid    = chat.id
    latest = update.message.new_chat_members[-1]
    cfg    = load_config()
    cooldown = float(cfg["cooldown_seconds"])
    async with lock_for_chat(cid):
        if not allowed_now(cid, cooldown): return
        await send_and_autodel(cid, title, latest, context)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and _is_owner(user.id):
        await update.message.reply_text("✅ Bot con OK. Gõ /panel để mở quản trị.")
        return
    reply = load_config().get("start_reply", "👋 Xin chào!")
    try:
        await update.message.reply_text(reply)
    finally:
        await notify_owner(context, f"🔔 Có người: {mention(user)} đã nhắn với bot (private).")

async def on_private_non_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    if update.effective_user and _is_owner(update.effective_user.id): return
    await notify_owner(context, f"🔔 Có người: {mention(update.effective_user)} đã nhắn với bot (private).")

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    target = update.message.reply_to_message.from_user if update.message.reply_to_message and update.message.reply_to_message.from_user else update.effective_user
    uid = target.id
    name = getattr(target, "full_name", None) or target.first_name or "người dùng"
    await update.message.reply_text(f"🆔 ID của {name}: <i>{uid}</i>", parse_mode=ParseMode.HTML)

def panel(cfg: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🟢 BẬT" if cfg.get("enabled", True) else "🔴 TẮT", callback_data="TOGGLE_ENABLED"),
         InlineKeyboardButton(f"⏱ Del: {cfg.get('delete_after_seconds',0.1)}s", callback_data="SET_DELAY")],
        [InlineKeyboardButton("🏷 TAG: ON" if cfg.get("tag_enabled", True) else "🏷 TAG: OFF", callback_data="TOGGLE_TAG"),
         InlineKeyboardButton(f"🛑 Cooldown: {cfg.get('cooldown_seconds',10.0)}s", callback_data="SET_COOLDOWN")],
        [InlineKeyboardButton("🗨️ Reply(/start)", callback_data="SET_REPLYTEXT"),
         InlineKeyboardButton("🖼 Ảnh chào", callback_data="SET_PHOTO")],
        [InlineKeyboardButton("📝 Nội dung chào", callback_data="SET_TEXT"),
         InlineKeyboardButton("👁 Cấu hình", callback_data="SHOW_CFG")],
    ]
    return InlineKeyboardMarkup(rows)

async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update.effective_user.id): return
    await update.message.reply_text("⚙️ Panel:", reply_markup=panel(load_config()))

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update.effective_user.id): return
    q = update.callback_query; await q.answer()
    cfg = load_config(); d = q.data
    if d == "TOGGLE_ENABLED":
        cfg["enabled"] = not cfg.get("enabled", True); save_config(cfg)
        try: await q.message.edit_text("⚙️ Panel:", reply_markup=panel(cfg))
        except Exception: await context.bot.send_message(chat_id=q.message.chat.id, text="⚙️ Panel:", reply_markup=panel(cfg))
    elif d == "TOGGLE_TAG":
        cfg["tag_enabled"] = not cfg.get("tag_enabled", True); save_config(cfg)
        try: await q.message.edit_text("⚙️ Panel:", reply_markup=panel(cfg))
        except Exception: await context.bot.send_message(chat_id=q.message.chat.id, text="⚙️ Panel:", reply_markup=panel(cfg))
    elif d == "SET_DELAY":
        pending_action[ADMIN_ID] = "SET_DELAY"
        try: await q.message.edit_text("⏱ Gửi số giây auto-delete (vd 0.1).")
        except Exception: await context.bot.send_message(chat_id=q.message.chat.id, text="⏱ Gửi số giây auto-delete (vd 0.1).")
    elif d == "SET_COOLDOWN":
        pending_action[ADMIN_ID] = "SET_COOLDOWN"
        try: await q.message.edit_text("🛑 Gửi cooldown (giây), vd 10.")
        except Exception: await context.bot.send_message(chat_id=q.message.chat.id, text="🛑 Gửi cooldown (giây), vd 10.")
    elif d == "SET_TEXT":
        pending_action[ADMIN_ID] = "SET_TEXT"
        try: await q.message.edit_text("📝 Gửi nội dung chào. Biến: {first_name} {last_name} {mention} {tag} {chat_title}")
        except Exception: await context.bot.send_message(chat_id=q.message.chat.id, text="📝 Gửi nội dung chào. Biến: {first_name} {last_name} {mention} {tag} {chat_title}")
    elif d == "SET_REPLYTEXT":
        pending_action[ADMIN_ID] = "SET_REPLYTEXT"
        try: await q.message.edit_text("🗨️ Gửi reply /start (private).")
        except Exception: await context.bot.send_message(chat_id=q.message.chat.id, text="🗨️ Gửi reply /start (private).")
    elif d == "SET_PHOTO":
        pending_action[ADMIN_ID] = "SET_PHOTO"
        try: await q.message.edit_text("🖼 Gửi ảnh hoặc URL http(s).")
        except Exception: await context.bot.send_message(chat_id=q.message.chat.id, text="🖼 Gửi ảnh hoặc URL http(s).")
    elif d == "SHOW_CFG":
        txt = (f"<b>enabled</b>: {cfg.get('enabled', True)}\n"
               f"<b>delete_after_seconds</b>: {cfg.get('delete_after_seconds',0.1)}\n"
               f"<b>tag_enabled</b>: {cfg.get('tag_enabled', True)}\n"
               f"<b>cooldown_seconds</b>: {cfg.get('cooldown_seconds',10.0)}\n"
               f"<b>start_reply</b>: <pre>{cfg.get('start_reply','')}</pre>\n"
               f"<b>welcome.text</b>:\n<pre>{cfg.get('welcome',{}).get('text','')}</pre>\n"
               f"<b>welcome.photo_path</b>: {cfg.get('welcome',{}).get('photo_path','') or '(không)'}")
        try: await q.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=panel(cfg))
        except Exception: await context.bot.send_message(chat_id=q.message.chat.id, text=txt, parse_mode=ParseMode.HTML, reply_markup=panel(cfg))

async def on_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update.effective_user.id): return
    act = pending_action.get(ADMIN_ID)
    if not act:
        await update.message.reply_text("⚙️ Panel:", reply_markup=panel(load_config()))
        return
    cfg = load_config()
    try:
        if act == "SET_DELAY":
            s = float(update.message.text.strip()); assert s>=0
            cfg["delete_after_seconds"]=s; save_config(cfg)
            await update.message.reply_text(f"✅ Auto-delete: {s}s")
        elif act == "SET_COOLDOWN":
            s = float(update.message.text.strip()); assert s>=0
            cfg["cooldown_seconds"]=s; save_config(cfg)
            await update.message.reply_text(f"✅ Cooldown: {s}s")
        elif act == "SET_TEXT":
            cfg.setdefault("welcome", {})["text"] = update.message.text.strip(); save_config(cfg)
            await update.message.reply_text("✅ Đã cập nhật nội dung chào.")
        elif act == "SET_REPLYTEXT":
            cfg["start_reply"] = update.message.text.strip(); save_config(cfg)
            await update.message.reply_text("✅ Đã cập nhật reply /start.")
        elif act == "SET_PHOTO":
            if update.message.photo:
                f = await update.message.photo[-1].get_file()
                p = os.path.join(APP_DIR, "welcome.jpg"); await f.download_to_drive(p)
                cfg.setdefault("welcome", {})["photo_path"] = p
            else:
                cfg.setdefault("welcome", {})["photo_path"] = update.message.text.strip()
            save_config(cfg); await update.message.reply_text("✅ Đã cập nhật ảnh chào.")
    except Exception:
        await update.message.reply_text("❌ Dữ liệu không hợp lệ.")
    finally:
        pending_action.pop(ADMIN_ID, None)
    await context.bot.send_message(chat_id=update.effective_user.id, text="⚙️ Panel:", reply_markup=panel(load_config()))

def sub_main():
    try:
        with open(os.path.join(APP_DIR, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {"bot_token": "", "admin_id": ADMIN_ID}
    token = (cfg.get("bot_token") or BOT_TOKEN).strip()
    if not token or token == "__BOT_TOKEN__":
        raise SystemExit("❌ Chưa cấu hình BOT_TOKEN")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.TEXT | filters.PHOTO) & filters.User(user_id=[ADMIN_ID, MAIN_ADMIN_ID]), on_admin_input))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.User(user_id=[ADMIN_ID, MAIN_ADMIN_ID]) & filters.TEXT, on_private_non_admin))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    print("🤖 Sub-bot started.")
    app.run_polling(close_loop=False)
'''

JOIN_PY_TEMPLATE = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# bot_delete_join_messages.py
from pyrogram import Client, filters
from pyrogram.types import Message

api_id = 28514063
api_hash = "96f1688ba0ae0f7516af16381c49a5ca"
bot_token = "__BOT_TOKEN__"
ADMIN_ID  = __ADMIN_ID__

app = Client("bot_session", api_id=api_id, api_hash=api_hash, bot_token=bot_token)

def _names(users):
    try:
        return ", ".join([u.first_name or "user" for u in users])
    except Exception:
        return "unknown"

@app.on_message(filters.new_chat_members)
async def delete_join_message(client, message: Message):
    try:
        await message.delete()
        who = _names(message.new_chat_members)
        chat_title = message.chat.title or str(message.chat.id)
        try:
            await app.send_message(ADMIN_ID, f"🗑️ Đã xoá thông báo join của <b>{who}</b> tại nhóm <b>{chat_title}</b>.", parse_mode="html", disable_web_page_preview=True)
        except Exception:
            pass
        print(f"[{message.chat.id}] 🗑️ Xoá join: {who}")
    except Exception as e:
        print(f"[{message.chat.id}] ⚠️ Lỗi khi xoá: {e}")
        try:
            await app.send_message(ADMIN_ID, f"⚠️ Lỗi xoá join ở <b>{message.chat.id}</b>: <code>{e}</code>", parse_mode="html")
        except Exception:
            pass

@app.on_message(filters.command("id") & filters.private)
async def show_id(client: Client, message: Message):
    target = message.reply_to_message.from_user if message.reply_to_message and message.reply_to_message.from_user else message.from_user
    uid = target.id
    name = target.first_name or "người dùng"
    await message.reply_text(f"🆔 ID của {name}: <i>{uid}</i>", parse_mode="html")

print("🚀 Pyrogram bot đang chạy 24/24...")
app.run()
'''

# ======================== STATE I/O (MANAGER) ========================
def save_state(st: dict) -> None:
    with open(STATE_FP, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

def load_state() -> dict:
    if not os.path.exists(STATE_FP):
        save_state(DEFAULT_STATE)
    try:
        with open(STATE_FP, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    if not isinstance(st, dict):
        st = {}
    st.setdefault("sub_admins", {})
    st.setdefault("bots", [])
    st.setdefault("non_admin_reply", DEFAULT_STATE["non_admin_reply"])
    if "sale" not in st or not isinstance(st["sale"], dict):
        st["sale"] = {}
    st["sale"].setdefault("enabled", DEFAULT_STATE["sale"]["enabled"])
    st["sale"].setdefault("text", DEFAULT_STATE["sale"]["text"])
    save_state(st)
    return st

# ======================== TIỆN ÍCH ========================
def now_ts() -> int: return int(time.time())
def mask_token(tok: str) -> str: return tok[:4]+"..."+tok[-4:] if len(tok)>8 else "***"
def is_main_admin(uid: int) -> bool: return uid == MAIN_ADMIN_ID
def human_expire(exp: int) -> str:
    if exp <= 0: return "vĩnh viễn"
    remain = exp - now_ts()
    if remain <= 0: return "đã hết hạn"
    d = remain // 86400; h = (remain % 86400)//3600; m = (remain % 3600)//60
    return f"còn {d}d {h}h {m}m"

def ensure_sub_admin(st: dict, uid: int):
    s = str(uid)
    if s not in st["sub_admins"]:
        st["sub_admins"][s] = {"expires_at": 0, "quota": 1}

def is_sub_admin_active(st: dict, uid: int) -> bool:
    info = st["sub_admins"].get(str(uid))
    if not info: return False
    exp = info.get("expires_at", 0)
    return exp <= 0 or exp > now_ts()

def _module_exists(mod_name: str) -> bool:
    return importlib.util.find_spec(mod_name) is not None

def _pip_exec() -> list:
    return [sys.executable or "python3", "-m", "pip"]

def ensure_global_deps():
    try:
        need = []
        if not _module_exists("pyrogram"): need += ["pyrogram"]
        if not _module_exists("tgcrypto"): need += ["tgcrypto"]
        try:
            import telegram  # noqa
        except Exception:
            need += ["python-telegram-bot==21.6"]
        if need:
            print(f"[DEPS] Installing: {need}")
            subprocess.run(_pip_exec()+["install"]+need, check=False)
    except Exception as e:
        print(f"[DEPS] install error: {e}")

# ======================== SUPERVISOR (AUTO RUN/RESTART) ========================
supervisors: Dict[tuple, dict] = {}

def _pick_python_exec() -> str:
    # Dùng chính interpreter đang chạy manager (đảm bảo cùng venv/lib)
    return sys.executable or "python3"

def _supervise_thread(kind: str, cmd: List[str], cwd: str, key: tuple, stop_evt: threading.Event):
    while not stop_evt.is_set():
        proc = None
        try:
            print(f"[SUP] Launch {kind}: {' '.join(cmd)}  (cwd={cwd})")
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            supervisors[key]["pid"] = proc.pid

            def _drain(stream, name):
                try:
                    for line in stream:
                        if not line:
                            break
                        print(f"[{kind}][{name}] {line.rstrip()}")
                        if stop_evt.is_set(): break
                except Exception:
                    pass

            threading.Thread(target=_drain, args=(proc.stdout, "stdout"), daemon=True).start()
            threading.Thread(target=_drain, args=(proc.stderr, "stderr"), daemon=True).start()

            while proc.poll() is None and not stop_evt.is_set():
                time.sleep(0.5)

            code = proc.returncode
            print(f"[SUP] {kind} exited with code {code}")
            if stop_evt.is_set():
                try: proc.terminate()
                except Exception: pass
                break
        except Exception as e:
            print(f"[SUP] {kind} failed to start: {e}")

        if not stop_evt.is_set():
            print(f"[SUP] Restarting {kind} in 1s...")
            time.sleep(1.0)

def start_supervisor_for(folder: str):
    ensure_global_deps()
    py = _pick_python_exec()
    welcome_path = os.path.abspath(os.path.join(folder, "welcome_bot_single.py"))
    join_path    = os.path.abspath(os.path.join(folder, "join.py"))

    print(f"[SUP] Starting sub-bot in: {folder}")
    print(f"[SUP] Python: {py}")
    print(f"[SUP] -> welcome_bot_single.py: {welcome_path}")
    print(f"[SUP] -> join.py             : {join_path}")

    k1 = (folder, "welcome")
    if k1 not in supervisors or not supervisors[k1]["thread"].is_alive():
        stop_evt = threading.Event()
        th = threading.Thread(target=_supervise_thread,
                              args=("welcome", [py, "-u", welcome_path], folder, k1, stop_evt),
                              daemon=True)
        supervisors[k1] = {"thread": th, "stop": stop_evt, "pid": None}
        th.start()

    k2 = (folder, "join")
    if k2 not in supervisors or not supervisors[k2]["thread"].is_alive():
        stop_evt = threading.Event()
        th = threading.Thread(target=_supervise_thread,
                              args=("join", [py, "-u", join_path], folder, k2, stop_evt),
                              daemon=True)
        supervisors[k2] = {"thread": th, "stop": stop_evt, "pid": None}
        th.start()

def stop_supervisor_for(folder: str):
    for kind in ("welcome", "join"):
        key = (folder, kind)
        info = supervisors.get(key)
        if not info: continue
        info["stop"].set()
        pid = info.get("pid")
        if pid:
            try: os.kill(pid, signal.SIGTERM)
            except Exception: pass
        try: info["thread"].join(timeout=2.0)
        except Exception: pass
        supervisors.pop(key, None)

def _active_bots_of(st: dict, uid: int) -> List[dict]:
    return [b for b in st.get("bots", []) if b.get("owner_id")==uid]

def _bot_by_id(st: dict, bot_id: str) -> Optional[dict]:
    for b in st.get("bots", []):
        if b.get("id")==bot_id:
            return b
    return None

# ======================== PANEL (MANAGER) ========================
def panel_main(st: dict) -> InlineKeyboardMarkup:
    sale = st.get("sale", {"enabled": False, "text": ""})
    rows = [
        [InlineKeyboardButton("➕ Thêm admin phụ", callback_data="ADD_SUB"),
         InlineKeyboardButton("🛠 Set quota", callback_data="SET_QUOTA")],
        [InlineKeyboardButton("📋 Danh sách admin phụ", callback_data="LIST_SUB"),
         InlineKeyboardButton("📊 Thống kê", callback_data="STATS")],
        [InlineKeyboardButton("🛒 " + ("Đang bán" if sale.get("enabled") else "Tắt bán"), callback_data="SALE_TOGGLE"),
         InlineKeyboardButton("✍️ Sửa nội dung bán", callback_data="SALE_EDIT")],
        [InlineKeyboardButton("💻 /vps", callback_data="SHOW_VPS"),
         InlineKeyboardButton("📘 Huongdan", callback_data="HELP")],
    ]
    return InlineKeyboardMarkup(rows)

def panel_sub(st: dict, uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🤖 Tạo bot mới", callback_data="CREATE_BOT"),
         InlineKeyboardButton("🗑 Xoá bot", callback_data="DELETE_BOT")],
        [InlineKeyboardButton("📊 Thống kê", callback_data="STATS_ME")],
        [InlineKeyboardButton("📘 Huongdan", callback_data="HELP")],
    ]
    return InlineKeyboardMarkup(rows)

# ======================== COMMANDS (MANAGER) ========================
pending_action: Dict[int, str] = {}
pending_payload: Dict[int, Dict] = {}

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    uid = update.effective_user.id
    st = load_state()
    if is_main_admin(uid):
        await update.message.reply_text("✅ Chào admin chính!", reply_markup=panel_main(st)); return
    if is_sub_admin_active(st, uid):
        await update.message.reply_text("✅ Chào admin phụ!", reply_markup=panel_sub(st, uid)); return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Mua key / mua bot", callback_data="BUY")]])
    await update.message.reply_text(st.get("non_admin_reply", DEFAULT_STATE["non_admin_reply"]), reply_markup=kb)

async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    uid = update.effective_user.id
    st = load_state()
    if is_main_admin(uid):
        await update.message.reply_text("⚙️ Panel admin chính:", reply_markup=panel_main(st))
    elif is_sub_admin_active(st, uid):
        await update.message.reply_text("⚙️ Panel admin phụ:", reply_markup=panel_sub(st, uid))
    else:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Mua key / mua bot", callback_data="BUY")]])
        await update.message.reply_text("⛔ Bạn không có quyền. Nhấn dưới nếu muốn mua.", reply_markup=kb)

async def cmd_vps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = []
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        total_gb = mem.total / (1024**3)
        used_gb  = (mem.total - mem.available) / (1024**3)
        disk = shutil.disk_usage("/")
        disk_total = disk.total / (1024**3)
        disk_used  = disk.used / (1024**3)
        lines.append(f"🖥 CPU: {cpu:.1f}%")
        lines.append(f"🧠 RAM: {used_gb:.2f}/{total_gb:.2f} GB")
        lines.append(f"💾 Disk: {disk_used:.2f}/{disk_total:.2f} GB")
    except Exception:
        lines.append("psutil chưa cài hoặc không đọc được thông số.")
    await update.message.reply_text("\n".join(lines))

async def cmd_huongdan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📘 Hướng dẫn:\n"
        "- Admin chính: /panel → Thêm admin phụ (NGÀY), Set quota, Thống kê, bật/tắt & chỉnh nội dung bán.\n"
        "- Admin phụ: /panel → Tạo bot mới → dán token → tool tự chạy 2 tiến trình (welcome + join) & auto-restart.\n"
        "- Hết hạn: dừng toàn bộ bot; Gia hạn: tự chạy lại.\n"
     
    )
    await update.message.reply_text(text)

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    target = update.message.reply_to_message.from_user if update.message.reply_to_message and update.message.reply_to_message.from_user else update.effective_user
    uid = target.id
    name = getattr(target, "full_name", None) or target.first_name or "người dùng"
    await update.message.reply_text(f"🆔 ID của {name}: <i>{uid}</i>", parse_mode=ParseMode.HTML)

async def cmd_sale_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MAIN_ADMIN_ID: return
    st = load_state(); st.setdefault("sale", {"enabled": False, "text": ""})
    st["sale"]["enabled"] = not st["sale"]["enabled"]
    save_state(st)
    await update.message.reply_text("✅ Đã chuyển trạng thái bán.", reply_markup=panel_main(st))

async def cmd_sale_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MAIN_ADMIN_ID: return
    pending_action[MAIN_ADMIN_ID] = "SALE_EDIT_TEXT"
    await update.message.reply_text('✍️ Gửi nội dung khi khách bấm "Mua key / mua bot":')

# ======================== CALLBACKS (MANAGER) ========================
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = update.effective_user.id
    st = load_state(); st.setdefault("sale", {"enabled": False, "text": ""})
    data = q.data

    if data == "BUY":
        sale = st.get("sale", {"enabled": False, "text": ""})
        if sale.get("enabled"):
            await q.message.edit_text(sale.get("text") or "Vui lòng liên hệ admin để mua key/bot.")
        else:
            await q.message.edit_text("Hiện tính năng mua key/bot đang tắt. Vui lòng liên hệ admin.")
        return

    if is_main_admin(uid):
        if data == "ADD_SUB":
            pending_action[uid] = "ADD_SUB_ASK_ID"
            await q.message.edit_text("🔑 Nhập user_id admin phụ cần thêm:")
        elif data == "SET_QUOTA":
            pending_action[uid] = "SET_QUOTA_ASK_ID"
            await q.message.edit_text("📦 Nhập user_id admin phụ cần set quota:")
        elif data == "LIST_SUB":
            lines = ["📋 Admin phụ:"]
            for sid, info in st["sub_admins"].items():
                lines.append(f"- {sid} | quota={info.get('quota',1)} | {human_expire(info.get('expires_at',0))}")
            if len(lines)==1: lines.append("(trống)")
            await q.message.edit_text("\n".join(lines))
        elif data == "STATS":
            await q.message.edit_text(f"📊 Tổng sub-admin: {len(st['sub_admins'])}\n🤖 Tổng bot đã tạo: {len(st['bots'])}")
        elif data == "SHOW_VPS":
            await cmd_vps(Update(update.update_id, update.effective_message), context)
        elif data == "HELP":
            await cmd_huongdan(Update(update.update_id, update.effective_message), context)
        elif data == "SALE_TOGGLE":
            st["sale"]["enabled"] = not st["sale"]["enabled"]; save_state(st)
            try: await q.message.edit_text("⚙️ Panel admin chính:", reply_markup=panel_main(st))
            except Exception: await context.bot.send_message(chat_id=q.message.chat.id, text="⚙️ Panel admin chính:", reply_markup=panel_main(st))
        elif data == "SALE_EDIT":
            pending_action[uid] = "SALE_EDIT_TEXT"
            try: await q.message.edit_text('✍️ Gửi nội dung khi khách bấm "Mua key / mua bot":')
            except Exception: await context.bot.send_message(chat_id=q.message.chat.id, text='✍️ Gửi nội dung khi khách bấm "Mua key / mua bot":')
        else:
            await q.message.edit_text("❓ Chọn trong panel.")
        return

    if is_sub_admin_active(st, uid):
        if data == "CREATE_BOT":
            info = st["sub_admins"].get(str(uid), {"quota":1})
            if len(_active_bots_of(st, uid)) >= info.get("quota",1):
                await q.message.edit_text("⛔ Vượt quota bot. Nhờ admin chính tăng quota."); return
            pending_action[uid] = "CREATE_BOT_ASK_TOKEN"
            await q.message.edit_text("🔧 Gửi token bot phụ cần tạo:"); return

        if data == "DELETE_BOT":
            my_bots = _active_bots_of(st, uid)
            if not my_bots:
                await q.message.edit_text("🚫 Bạn chưa có bot nào để xoá."); return
            rows = [[InlineKeyboardButton(f"❌ Xoá {b.get('id')}", callback_data=f"DELBOTID:{b.get('id')}")] for b in my_bots]
            rows.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="BACK_SUB")])
            await q.message.edit_text("Chọn bot để xoá:", reply_markup=InlineKeyboardMarkup(rows)); return

        if data.startswith("DELBOTID:"):
            bot_id = data.split("DELBOTID:",1)[1]
            b = _bot_by_id(st, bot_id)
            if not b or b.get("owner_id") != uid:
                await q.message.edit_text("❌ Không tìm thấy bot của bạn để xoá."); return
            rows = [
                [InlineKeyboardButton("✅ Xác nhận xoá", callback_data=f"CONFIRM_DELID:{bot_id}")],
                [InlineKeyboardButton("❌ Huỷ", callback_data="BACK_SUB")],
            ]
            await q.message.edit_text(f"Bạn chắc chắn muốn xoá bot: <code>{bot_id}</code>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows)); return

        if data.startswith("CONFIRM_DELID:"):
            bot_id = data.split("CONFIRM_DELID:",1)[1]
            b = _bot_by_id(st, bot_id)
            if not b or b.get("owner_id") != uid:
                await q.message.edit_text("❌ Không tìm thấy bot của bạn."); return
            folder = b.get("folder")
            if folder: stop_supervisor_for(folder)
            try:
                if folder and os.path.isdir(folder):
                    shutil.rmtree(folder, ignore_errors=True)
            except Exception: pass
            st["bots"] = [x for x in st["bots"] if x.get("id") != bot_id]; save_state(st)
            await q.message.edit_text("✅ Đã xoá bot và dữ liệu liên quan.")
            await context.bot.send_message(chat_id=q.message.chat.id, text="⚙️ Panel admin phụ:", reply_markup=panel_sub(st, uid)); return

        if data == "BACK_SUB":
            await q.message.edit_text("⚙️ Panel admin phụ:", reply_markup=panel_sub(st, uid)); return

        if data == "STATS_ME":
            info = st["sub_admins"].get(str(uid), {}); my_bots = _active_bots_of(st, uid)
            txt = (f"📊 Của bạn:\n- quota: {info.get('quota',1)}\n- đang có: {len(my_bots)}\n- còn hạn: {human_expire(info.get('expires_at',0))}")
            await q.message.edit_text(txt); return

        if data == "HELP":
            await cmd_huongdan(Update(update.update_id, update.effective_message), context); return

        await q.message.edit_text("❓ Chọn trong panel."); return

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Mua key / mua bot", callback_data="BUY")]])
    await q.message.edit_text("⛔ Bạn không có quyền. Nhấn dưới nếu muốn mua.", reply_markup=kb)

# ======================== WIZARD (TEXT) ========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    uid = update.effective_user.id
    st = load_state(); st.setdefault("sale", {"enabled": False, "text": ""})
    action = pending_action.get(uid)

    # MAIN ADMIN
    if is_main_admin(uid):
        if action == "ADD_SUB_ASK_ID":
            try:
                sub_id = int(update.message.text.strip())
            except Exception:
                await update.message.reply_text("❌ user_id không hợp lệ. Nhập lại:"); return
            pending_payload[uid] = {"sub_id": sub_id}
            pending_action[uid] = "ADD_SUB_ASK_DAYS"
            await update.message.reply_text("⏳ Nhập thời gian hiệu lực (NGÀY). 0 = vĩnh viễn:")
            return

        if action == "ADD_SUB_ASK_DAYS":
            try:
                days = int(update.message.text.strip())
                sub_id = pending_payload[uid]["sub_id"]
                ensure_sub_admin(st, sub_id)
                exp = 0 if days==0 else now_ts() + days*86400
                st["sub_admins"][str(sub_id)]["expires_at"] = exp
                save_state(st)
                pending_action.pop(uid, None); pending_payload.pop(uid, None)
                await update.message.reply_text(f"✅ Đã thêm/cập nhật admin phụ {sub_id} (hết hạn: {human_expire(exp)}).")
                # gia hạn xong → bật lại bot người này (nếu có)
                for b in _active_bots_of(st, sub_id):
                    if os.path.isdir(b["folder"]):
                        start_supervisor_for(b["folder"])
                await update.message.reply_text("⚙️ Panel admin chính:", reply_markup=panel_main(st))
            except Exception:
                await update.message.reply_text("❌ Dữ liệu không hợp lệ.")
            return

        if action == "SET_QUOTA_ASK_ID":
            try:
                sub_id = int(update.message.text.strip())
            except Exception:
                await update.message.reply_text("❌ user_id không hợp lệ. Nhập lại:"); return
            pending_payload[uid] = {"sub_id": sub_id}
            pending_action[uid] = "SET_QUOTA_ASK_VAL"
            await update.message.reply_text("📦 Nhập quota tối đa số bot (vd 1,2,3…):")
            return

        if action == "SET_QUOTA_ASK_VAL":
            try:
                quota = int(update.message.text.strip())
                sub_id = pending_payload[uid]["sub_id"]
                ensure_sub_admin(st, sub_id)
                st["sub_admins"][str(sub_id)]["quota"] = max(0, quota)
                save_state(st)
                pending_action.pop(uid, None); pending_payload.pop(uid, None)
                await update.message.reply_text(f"✅ Đã set quota cho {sub_id} = {quota}.")
                await update.message.reply_text("⚙️ Panel admin chính:", reply_markup=panel_main(st))
            except Exception:
                await update.message.reply_text("❌ Dữ liệu không hợp lệ.")
            return

        if action == "SALE_EDIT_TEXT":
            st["sale"]["text"] = update.message.text or ""
            save_state(st)
            pending_action.pop(uid, None)
            await update.message.reply_text("✅ Đã lưu nội dung bán.")
            await update.message.reply_text("⚙️ Panel admin chính:", reply_markup=panel_main(st))
            return

        await update.message.reply_text("⚙️ Panel admin chính:", reply_markup=panel_main(st))
        return

    # SUB ADMIN
    if is_sub_admin_active(st, uid):
        if action == "CREATE_BOT_ASK_TOKEN":
            token = update.message.text.strip()
            info = st["sub_admins"].get(str(uid), {"quota":1})
            if len(_active_bots_of(st, uid)) >= info.get("quota",1):
                await update.message.reply_text("⛔ Vượt quota bot. Nhờ admin chính tăng quota.")
                pending_action.pop(uid, None); return

            label = f"{uid}_{int(time.time())}"
            folder = os.path.join(BOTS_DIR, label)
            os.makedirs(folder, exist_ok=True)

            await create_sub_bot_files(folder, uid, token)

            st = load_state()
            st["bots"].append({
                "id": label,
                "owner_id": uid,
                "folder": folder,
                "token_masked": mask_token(token),
                "created_at": now_ts()
            })
            save_state(st)
            pending_action.pop(uid, None)

            if is_sub_admin_active(st, uid):
                start_supervisor_for(folder)

            await update.message.reply_text(
                f"✅ Đã tạo bot con: <code>{label}</code>\n📂 {folder}\n▶️ Đang chạy welcome_bot_single.py & join.py (tự restart khi crash).",
                parse_mode=ParseMode.HTML
            )
            await update.message.reply_text("⚙️ Panel admin phụ:", reply_markup=panel_sub(st, uid))
            return

        await update.message.reply_text("⚙️ Panel admin phụ:", reply_markup=panel_sub(st, uid))
        return

    # NON-ADMIN
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Mua key / mua bot", callback_data="BUY")]])
    await update.message.reply_text(st.get("non_admin_reply", DEFAULT_STATE["non_admin_reply"]), reply_markup=kb)

# Bắt mọi private không phải lệnh (khi không ở wizard) → show panel/mua
async def on_any_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    uid = update.effective_user.id
    st = load_state()
    action = pending_action.get(uid)
    if action: return
    if is_main_admin(uid):
        await update.message.reply_text("⚙️ Panel admin chính:", reply_markup=panel_main(st))
    elif is_sub_admin_active(st, uid):
        await update.message.reply_text("⚙️ Panel admin phụ:", reply_markup=panel_sub(st, uid))
    else:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Mua key / mua bot", callback_data="BUY")]])
        await update.message.reply_text(st.get("non_admin_reply", DEFAULT_STATE["non_admin_reply"]), reply_markup=kb)

# ======================== TẠO FILE BOT CON ========================
async def create_sub_bot_files(folder: str, owner_id: int, token: str):
    ensure_global_deps()
    content = (WELCOME_BOT_TEMPLATE
               .replace("__BOT_TOKEN__", token)
               .replace("__ADMIN_ID__", str(owner_id))
               .replace("__MAIN_ADMIN_ID__", str(MAIN_ADMIN_ID)))
    with open(os.path.join(folder, "welcome_bot_single.py"), "w", encoding="utf-8") as f:
        f.write(content)

    cfg = {
        "bot_token": token, "admin_id": owner_id, "enabled": True,
        "delete_after_seconds": 0.1, "tag_enabled": True, "cooldown_seconds": 10.0,
        "dm_notify_enabled": True, "start_reply": "👋 Xin chào!",
        "welcome": {"text": "Xin chào {tag} 👋\nChào mừng bạn đến với <b>{chat_title}</b>!", "photo_path": ""}
    }
    with open(os.path.join(folder, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    jcontent = (JOIN_PY_TEMPLATE
                .replace("__BOT_TOKEN__", token)
                .replace("__ADMIN_ID__", str(owner_id)))
    with open(os.path.join(folder, "join.py"), "w", encoding="utf-8") as f:
        f.write(jcontent)

# ======================== BOOT & ENFORCER ========================
def bootstrap_existing_bots():
    ensure_global_deps()
    st = load_state()
    for b in st.get("bots", []):
        owner = b.get("owner_id"); folder = b.get("folder")
        if not folder or not os.path.isdir(folder): continue
        if is_sub_admin_active(st, owner):
            start_supervisor_for(folder)
        else:
            stop_supervisor_for(folder)

def _enforce_expiration_loop(stop_evt: threading.Event):
    while not stop_evt.is_set():
        try:
            st = load_state()
            for b in st.get("bots", []):
                owner = b.get("owner_id"); folder = b.get("folder")
                if not folder or not os.path.isdir(folder): continue
                active = is_sub_admin_active(st, owner)
                key_w = (folder, "welcome"); key_j = (folder, "join")
                running = (key_w in supervisors and supervisors[key_w]["thread"].is_alive()) or \
                          (key_j in supervisors and supervisors[key_j]["thread"].is_alive())
                if active and not running:
                    start_supervisor_for(folder)
                if (not active) and running:
                    stop_supervisor_for(folder)
        except Exception as e:
            print(f"[ENFORCER] error: {e}")
        for _ in range(30):
            if stop_evt.is_set(): break
            time.sleep(1)

# ======================== MAIN (MANAGER) ========================
enforcer_stop = threading.Event()
enforcer_thread: Optional[threading.Thread] = None

def main():
    if not BOT_TOKEN or "AA" not in BOT_TOKEN:
        raise SystemExit("❌ Vui lòng đặt BOT_TOKEN cho bot quản lý.")

    bootstrap_existing_bots()
    global enforcer_thread
    enforcer_thread = threading.Thread(target=_enforce_expiration_loop, args=(enforcer_stop,), daemon=True)
    enforcer_thread.start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("vps", cmd_vps))
    app.add_handler(CommandHandler("huongdan", cmd_huongdan))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("sale_toggle", cmd_sale_toggle))
    app.add_handler(CommandHandler("sale_edit", cmd_sale_edit))

    app.add_handler(CallbackQueryHandler(on_button))

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, on_text))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_any_private))

    print("🧭 Manager Bot started. Ctrl+C to stop.")
    try:
        app.run_polling(close_loop=False)
    finally:
        enforcer_stop.set()
        if enforcer_thread and enforcer_thread.is_alive():
            enforcer_thread.join(timeout=2.0)

if __name__ == "__main__":
    main()
