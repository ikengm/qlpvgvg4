#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
====================================================================
     IPms Private Operator v3.2  |  النسخة الكاملة النهائية
====================================================================
- سجل في الذاكرة يُمسح كل 20 ساعة (لا قاعدة بيانات)
- زر "إرسال رسالة" إلزامي قبل أي رسالة
- مؤقت تدريجي: 5 دقائق ↔ 15 دقيقة (لكل عميل على حدة)
- أدمن أول + أدمن ثاني (OPERATOR2_ID)
- زر "تحويل للأدمن الثاني" يظهر فقط إذا كان OPERATOR2_ID مضبوطاً
- الرد يصل للعميل بصيغة << الرد >>
====================================================================
"""

import os, time, asyncio, logging
from datetime import datetime
from html import escape
from typing import Optional, List, Dict

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand,
    BotCommandScopeChat, BotCommandScopeAllPrivateChats,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

# ---------------------------------------------------------------------------
# إعدادات
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8903469723:AAFGYIsf0dc9tzT8pCsEUf1hmhsVeA-8dT8")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN غير مضبوط.")

OPERATOR_ID  = int(os.environ.get("OPERATOR_ID", "8505592972"))
OPERATOR2_ID = int(os.environ.get("OPERATOR2_ID", "6888107255"))

CLEANUP_HOURS = 20
CLEANUP_SECONDS = CLEANUP_HOURS * 60 * 60
COOLDOWN_5_MIN  = 5 * 60
COOLDOWN_15_MIN = 15 * 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("operator")

SYM = {
    "ok": "✓", "fail": "✗", "arrow": "»", "back": "«",
    "bullet": "•", "line": "─", "user": "◈", "msg": "▤",
    "file": "▣", "admin": "◆", "wait": "…", "id": "▸",
    "fwd": "↪", "log": "▦", "clock": "◷",
}

# ===================================================================
# السجل في الذاكرة
# ===================================================================
class MemoryStore:
    def __init__(self):
        self.clients: Dict[int, dict] = {}
        self.messages: Dict[int, dict] = {}
        self.next_msg_id = 1

    def upsert_client(self, user):
        uid = user.id
        now = int(time.time())
        if uid in self.clients:
            self.clients[uid]["username"] = user.username or ""
            self.clients[uid]["full_name"] = user.full_name or ""
            self.clients[uid]["last_seen"] = now
        else:
            self.clients[uid] = {
                "user_id": uid, "username": user.username or "", "full_name": user.full_name or "",
                "first_seen": now, "last_seen": now, "msg_count": 0, "blocked": False,
                "last_sent_at": 0, "send_count": 0, "awaiting": False,
            }

    def get_client(self, uid: int) -> Optional[dict]:
        return self.clients.get(uid)

    def list_clients(self, limit=20, offset=0) -> List[dict]:
        rows = sorted(self.clients.values(), key=lambda c: c["last_seen"], reverse=True)
        return rows[offset:offset+limit]

    def count_clients(self) -> int:
        return len(self.clients)

    def set_blocked(self, uid: int, blocked: bool):
        if uid in self.clients:
            self.clients[uid]["blocked"] = blocked

    def delete_client(self, uid: int):
        self.clients.pop(uid, None)
        self.messages = {k: v for k, v in self.messages.items() if v["user_id"] != uid}

    def log_message(self, uid, direction, kind, content, caption="", file_name="", tg_id=0, owner_op=0) -> int:
        mid = self.next_msg_id
        self.next_msg_id += 1
        now = int(time.time())
        self.messages[mid] = {
            "id": mid, "user_id": uid, "direction": direction, "kind": kind,
            "content": content or "", "caption": caption or "", "file_name": file_name or "",
            "created_at": now, "replied": False, "answered_at": 0,
            "tg_message_id": tg_id, "owner_op": owner_op,
        }
        if direction == "in" and uid in self.clients:
            self.clients[uid]["msg_count"] += 1
            self.clients[uid]["last_seen"] = now
        return mid

    def get_message(self, mid: int) -> Optional[dict]:
        return self.messages.get(mid)

    def get_messages(self, uid: int, limit=15, offset=0) -> List[dict]:
        rows = [m for m in self.messages.values() if m["user_id"] == uid]
        rows.sort(key=lambda m: m["id"], reverse=True)
        return rows[offset:offset+limit]

    def mark_answered(self, mid: int):
        if mid in self.messages:
            self.messages[mid]["replied"] = True
            self.messages[mid]["answered_at"] = int(time.time())

    def clear_all(self):
        n_c, n_m = len(self.clients), len(self.messages)
        self.clients.clear()
        self.messages.clear()
        self.next_msg_id = 1
        return n_c, n_m

    def get_cooldown_remaining(self, uid: int) -> int:
        c = self.clients.get(uid)
        if not c or c["last_sent_at"] == 0:
            return 0
        cooldown = COOLDOWN_5_MIN if c["send_count"] % 2 == 1 else COOLDOWN_15_MIN
        elapsed = int(time.time()) - c["last_sent_at"]
        return max(0, cooldown - elapsed)

    def mark_sent(self, uid: int):
        c = self.clients.get(uid)
        if c:
            c["last_sent_at"] = int(time.time())
            c["send_count"] += 1
            c["awaiting"] = False

    def set_awaiting(self, uid: int, state: bool):
        c = self.clients.get(uid)
        if c:
            c["awaiting"] = state

STORE = MemoryStore()
_pending_reply: Dict[int, int] = {}

# ---------------------------------------------------------------------------
# أدوات
# ---------------------------------------------------------------------------
def fmt_ts(ts: int) -> str:
    if not ts: return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

def kind_label(kind: str) -> str:
    return {"text": "نص", "photo": "صورة", "document": "ملف",
            "voice": "صوت", "video": "فيديو", "audio": "مقطع صوتي",
            "sticker": "ملصق"}.get(kind, kind)

def is_operator(uid: int) -> bool:
    return uid in (OPERATOR_ID, OPERATOR2_ID) and uid != 0

def op_label(uid: int) -> str:
    if uid == OPERATOR_ID: return "الأدمن الأول"
    if uid == OPERATOR2_ID: return "الأدمن الثاني"
    return "—"

def fmt_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"{m} دقيقة و{s} ثانية" if m > 0 else f"{s} ثانية"

def client_card(uid: int) -> str:
    c = STORE.get_client(uid)
    if not c:
        return f"{SYM['fail']} لا يوجد عميل بهذا المعرف."
    remain = STORE.get_cooldown_remaining(uid)
    cooldown_line = f"{SYM['clock']} المؤقت المتبقي: {fmt_duration(remain)}" if remain > 0 else f"{SYM['clock']} لا يوجد مؤقت"
    return "\n".join([
        f"{SYM['user']} <b>بطاقة العميل</b>",
        f"{SYM['line']*20}",
        f"{SYM['id']} المعرف: <code>{c['user_id']}</code>",
        f"{SYM['id']} الاسم: {escape(c['full_name'] or '—')}",
        f"{SYM['id']} اليوزر: {'@'+escape(c['username']) if c['username'] else '—'}",
        f"{SYM['id']} محظور: {'نعم' if c['blocked'] else 'لا'}",
        f"{SYM['line']*20}",
        f"{SYM['id']} أول ظهور: {fmt_ts(c['first_seen'])}",
        f"{SYM['id']} آخر نشاط: {fmt_ts(c['last_seen'])}",
        f"{SYM['id']} عدد الرسائل: {c['msg_count']}",
        f"{cooldown_line}",
    ])

def build_clients_keyboard(page: int, per_page=8) -> InlineKeyboardMarkup:
    total = STORE.count_clients()
    rows = STORE.list_clients(limit=per_page, offset=page*per_page)
    kb = []
    for c in rows:
        name = c["full_name"] or "—"
        if len(name) > 18: name = name[:18] + "…"
        label = f"{SYM['user']} {name}  |  {c['user_id']}"
        if c["blocked"]: label = f"{SYM['fail']} {label}"
        kb.append([InlineKeyboardButton(label, callback_data=f"open:{c['user_id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(f"{SYM['back']} السابق", callback_data=f"page:{page-1}"))
    if (page+1)*per_page < total:
        nav.append(InlineKeyboardButton(f"التالي {SYM['arrow']}", callback_data=f"page:{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton(f"{SYM['bullet']} تحديث", callback_data=f"page:{page}")])
    return InlineKeyboardMarkup(kb)

def build_client_actions(uid: int, viewer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{SYM['msg']} عرض الرسائل", callback_data=f"msgs:{uid}")],
        [InlineKeyboardButton(f"{SYM['log']} سجل العميل", callback_data=f"log:{uid}")],
        [InlineKeyboardButton(f"{SYM['ok']} تفعيل/تعطيل الحظر", callback_data=f"block:{uid}"),
         InlineKeyboardButton(f"{SYM['fail']} حذف العميل", callback_data=f"del:{uid}")],
        [InlineKeyboardButton(f"{SYM['back']} رجوع للقائمة", callback_data="page:0")],
    ])

def build_messages_keyboard(uid: int, page: int, per_page=6) -> InlineKeyboardMarkup:
    msgs = STORE.get_messages(uid, limit=per_page, offset=page*per_page)
    kb = []
    for m in msgs:
        marker = "»" if m["direction"] == "in" else "«"
        status = "✓" if m["replied"] else "…"
        snippet = (m["content"] or m["caption"] or m["file_name"] or "")[:28]
        label = f"{marker} {status} {kind_label(m['kind'])} | {snippet}"
        kb.append([InlineKeyboardButton(label, callback_data=f"msg:{m['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(f"{SYM['back']} السابق", callback_data=f"mspage:{uid}:{page-1}"))
    if len(msgs) == per_page:
        nav.append(InlineKeyboardButton(f"التالي {SYM['arrow']}", callback_data=f"mspage:{uid}:{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton(f"{SYM['back']} رجوع للعميل", callback_data=f"open:{uid}")])
    return InlineKeyboardMarkup(kb)

def build_message_actions(msg_id: int, uid: int, viewer_id: int, owner_op: int, replied: bool) -> InlineKeyboardMarkup:
    kb = []
    if not replied:
        kb.append([InlineKeyboardButton(f"{SYM['ok']} الرد على هذه الرسالة", callback_data=f"reply:{msg_id}")])
        # زر التحويل يظهر فقط للأدمن الأول + إذا كان الأدمن الثاني مضبوطاً
        if viewer_id == OPERATOR_ID and OPERATOR2_ID and owner_op != 2:
            kb.append([InlineKeyboardButton(f"{SYM['fwd']} تحويل للأدمن الثاني", callback_data=f"fwd2:{msg_id}")])
    else:
        kb.append([InlineKeyboardButton(f"{SYM['ok']} (تم الرد)", callback_data=f"msg:{msg_id}")])
    kb.append([InlineKeyboardButton(f"{SYM['back']} رجوع للرسائل", callback_data=f"msgs:{uid}")])
    return InlineKeyboardMarkup(kb)

def build_client_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{SYM['msg']} إرسال رسالة", callback_data="c_send_text")],
        [InlineKeyboardButton(f"{SYM['bullet']} معلوماتي", callback_data="c_info")],
    ])

# ---------------------------------------------------------------------------
# أوامر العميل
# ---------------------------------------------------------------------------
async def c_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    STORE.upsert_client(update.effective_user)
    c = STORE.get_client(update.effective_user.id)
    if c and c["blocked"]:
        await update.message.reply_text(f"{SYM['fail']} تم حظرك من استخدام الخدمة.")
        return
    await update.message.reply_text(
        f"{SYM['user']} مرحباً بك.\n"
        f"{SYM['line']*18}\n"
        f"اختر من القائمة أدناه. لا يمكنك رؤية أي عميل آخر.\n"
        f"{SYM['bullet']} يُحذف السجل تلقائياً كل {CLEANUP_HOURS} ساعة.\n"
        f"{SYM['clock']} بعد كل رسالة: مؤقت 5 دقائق ثم 15 دقيقة (بالتناوب).",
        reply_markup=build_client_main_menu()
    )

async def c_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    c = STORE.get_client(q.from_user.id)
    if not c:
        await q.edit_message_text(f"{SYM['fail']} غير مسجل.")
        return
    remain = STORE.get_cooldown_remaining(q.from_user.id)
    cooldown_line = f"{SYM['clock']} المؤقت المتبقي: {fmt_duration(remain)}" if remain > 0 else f"{SYM['clock']} يمكنك الإرسال الآن"
    try:
        await q.edit_message_text(
            f"{SYM['user']} <b>معلوماتك</b>\n"
            f"{SYM['line']*18}\n"
            f"{SYM['id']} المعرف: <code>{c['user_id']}</code>\n"
            f"{SYM['id']} الاسم: {escape(c['full_name'] or '—')}\n"
            f"{SYM['id']} اليوزر: {'@'+escape(c['username']) if c['username'] else '—'}\n"
            f"{SYM['id']} عدد رسائلك: {c['msg_count']}\n"
            f"{SYM['id']} آخر نشاط: {fmt_ts(c['last_seen'])}\n"
            f"{cooldown_line}\n"
            f"{SYM['line']*18}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['back']} رجوع", callback_data="c_home")]])
        )
    except Exception:
        pass

async def c_send_text_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    remain = STORE.get_cooldown_remaining(uid)
    if remain > 0:
        await q.answer(f"انتظر {fmt_duration(remain)} قبل الإرسال.", show_alert=True)
        return
    STORE.set_awaiting(uid, True)
    try:
        await q.edit_message_text(
            f"{SYM['msg']} أرسل الآن رسالتك (نص، صورة، ملف، صوت، فيديو).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['back']} إلغاء", callback_data="c_home")]])
        )
    except Exception:
        pass

async def c_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    STORE.set_awaiting(q.from_user.id, False)
    try:
        await q.edit_message_text(f"{SYM['user']} القائمة الرئيسية.", reply_markup=build_client_main_menu())
    except Exception:
        pass

# ---------------------------------------------------------------------------
# استقبال رسائل العملاء
# ---------------------------------------------------------------------------
async def c_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if is_operator(update.effective_user.id):
        return
    uid = update.effective_user.id
    STORE.upsert_client(update.effective_user)
    c = STORE.get_client(uid)
    if not c:
        return
    if c["blocked"]:
        await update.message.reply_text(f"{SYM['fail']} تم حظرك.")
        return
    if not c["awaiting"]:
        await update.message.reply_text(
            f"{SYM['fail']} لا يمكنك إرسال رسالة مباشرة.\n"
            f"{SYM['bullet']} اضغط على زر «{SYM['msg']} إرسال رسالة» أولاً.",
            reply_markup=build_client_main_menu()
        )
        return
    remain = STORE.get_cooldown_remaining(uid)
    if remain > 0:
        await update.message.reply_text(f"{SYM['clock']} انتظر {fmt_duration(remain)} قبل الإرسال.")
        return

    msg = update.message
    if msg.text:
        kind, content, caption, fname = "text", msg.text, "", ""
    elif msg.photo:
        kind, content, caption, fname = "photo", msg.photo[-1].file_id, msg.caption or "", ""
    elif msg.document:
        kind, content, caption, fname = "document", msg.document.file_id, msg.caption or "", msg.document.file_name or ""
    elif msg.voice:
        kind, content, caption, fname = "voice", msg.voice.file_id, msg.caption or "", ""
    elif msg.video:
        kind, content, caption, fname = "video", msg.video.file_id, msg.caption or "", ""
    elif msg.audio:
        kind, content, caption, fname = "audio", msg.audio.file_id, msg.caption or "", ""
    elif msg.sticker:
        kind, content, caption, fname = "sticker", msg.sticker.file_id, "", ""
    else:
        await msg.reply_text(f"{SYM['fail']} نوع غير مدعوم.")
        return

    STORE.mark_sent(uid)
    await msg.reply_text(
        f"{SYM['ok']} تم استلام رسالتك. سيتم الرد عليك هنا.\n"
        f"{SYM['clock']} المؤقت القادم: {'5 دقائق' if c['send_count'] % 2 == 0 else '15 دقيقة'}."
    )

    if not OPERATOR_ID:
        return

    header = (
        f"{SYM['msg']} <b>رسالة جديدة</b>\n"
        f"{SYM['line']*18}\n"
        f"{SYM['user']} <a href='tg://user?id={uid}'>{escape(update.effective_user.full_name or '—')}</a>\n"
        f"{SYM['id']} <code>{uid}</code>\n"
        f"{SYM['id']} النوع: {kind_label(kind)}\n"
        f"{SYM['line']*18}"
    )
    try:
        tg_msg_id = 0
        if kind == "text":
            sent = await context.bot.send_message(OPERATOR_ID, header + "\n" + escape(content), parse_mode="HTML")
            tg_msg_id = sent.message_id
        elif kind == "photo":
            sent = await context.bot.send_photo(OPERATOR_ID, content, caption=header + "\n" + escape(caption or ""), parse_mode="HTML")
            tg_msg_id = sent.message_id
        elif kind == "document":
            sent = await context.bot.send_document(OPERATOR_ID, content, caption=header + "\n" + escape(caption or "") + f"\n{SYM['file']} {escape(fname)}", parse_mode="HTML")
            tg_msg_id = sent.message_id
        elif kind == "voice":
            sent = await context.bot.send_voice(OPERATOR_ID, content, caption=header, parse_mode="HTML")
            tg_msg_id = sent.message_id
        elif kind == "video":
            sent = await context.bot.send_video(OPERATOR_ID, content, caption=header + "\n" + escape(caption or ""), parse_mode="HTML")
            tg_msg_id = sent.message_id
        elif kind == "audio":
            sent = await context.bot.send_audio(OPERATOR_ID, content, caption=header, parse_mode="HTML")
            tg_msg_id = sent.message_id
        elif kind == "sticker":
            await context.bot.send_sticker(OPERATOR_ID, content)
            sent = await context.bot.send_message(OPERATOR_ID, header, parse_mode="HTML")
            tg_msg_id = sent.message_id

        mid = STORE.log_message(uid, "in", kind, content, caption, fname, tg_msg_id, owner_op=1)
        kb = build_message_actions(mid, uid, OPERATOR_ID, 1, False)
        await context.bot.send_message(OPERATOR_ID, f"{SYM['bullet']} خيارات الرسالة:", reply_markup=kb)
    except Exception as e:
        log.warning("send to operator1 failed: %s", e)

# ---------------------------------------------------------------------------
# لوحة المشغل
# ---------------------------------------------------------------------------
async def o_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_operator(update.effective_user.id):
        return
    who = op_label(update.effective_user.id)
    extra = ""
    if update.effective_user.id == OPERATOR_ID and not OPERATOR2_ID:
        extra = f"\n{SYM['fail']} تنبيه: OPERATOR2_ID غير مضبوط — زر التحويل لن يظهر."
    await update.message.reply_text(
        f"{SYM['admin']} <b>لوحة {who}</b>\n"
        f"{SYM['line']*20}\n"
        f"{SYM['bullet']} لديك {STORE.count_clients()} عميل.\n"
        f"{SYM['bullet']} السجل في الذاكرة — يُحذف كل {CLEANUP_HOURS} ساعة.\n"
        f"{SYM['clock']} مؤقت العملاء: 5 دقائق ↔ 15 دقيقة."
        f"{extra}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{SYM['user']} قائمة العملاء", callback_data="page:0")],
        ])
    )

async def _safe_edit(q, text, kb=None, parse_mode=None):
    try:
        await q.edit_message_text(text, parse_mode=parse_mode, reply_markup=kb)
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.warning("edit failed: %s", e)

async def o_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_operator(q.from_user.id):
        await q.answer("غير مصرح", show_alert=True)
        return
    data = q.data or ""
    await q.answer()
    viewer = q.from_user.id

    if data.startswith("page:"):
        page = int(data.split(":")[1])
        await _safe_edit(q,
            f"{SYM['admin']} <b>قائمة العملاء</b>\n{SYM['line']*20}\nاضغط على عميل لعرض بطاقته.",
            build_clients_keyboard(page), "HTML")
    elif data.startswith("open:"):
        uid = int(data.split(":")[1])
        await _safe_edit(q, client_card(uid), build_client_actions(uid, viewer), "HTML")
    elif data.startswith("block:"):
        uid = int(data.split(":")[1])
        c = STORE.get_client(uid)
        if c:
            STORE.set_blocked(uid, not c["blocked"])
            await _safe_edit(q, client_card(uid), build_client_actions(uid, viewer), "HTML")
    elif data.startswith("del:"):
        uid = int(data.split(":")[1])
        STORE.delete_client(uid)
        await _safe_edit(q,
            f"{SYM['ok']} تم حذف العميل {uid}.",
            InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['back']} رجوع", callback_data="page:0")]]))
    elif data.startswith("msgs:"):
        uid = int(data.split(":")[1])
        await _safe_edit(q,
            f"{SYM['msg']} <b>رسائل العميل</b> <code>{uid}</code>\n{SYM['line']*20}",
            build_messages_keyboard(uid, 0), "HTML")
    elif data.startswith("mspage:"):
        _, uid_s, page_s = data.split(":")
        uid, page = int(uid_s), int(page_s)
        await _safe_edit(q,
            f"{SYM['msg']} <b>رسائل العميل</b> <code>{uid}</code>\n{SYM['line']*20}",
            build_messages_keyboard(uid, page), "HTML")
    elif data.startswith("msg:"):
        mid = int(data.split(":")[1])
        m = STORE.get_message(mid)
        if not m:
            await _safe_edit(q, f"{SYM['fail']} الرسالة غير موجودة.")
            return
        body = (
            f"{SYM['msg']} <b>تفاصيل الرسالة</b>\n"
            f"{SYM['line']*20}\n"
            f"{SYM['id']} العميل: <code>{m['user_id']}</code>\n"
            f"{SYM['id']} الاتجاه: {'من العميل' if m['direction']=='in' else 'من المشغل'}\n"
            f"{SYM['id']} النوع: {kind_label(m['kind'])}\n"
            f"{SYM['id']} المالك: {op_label(m['owner_op']) if m['owner_op'] else '—'}\n"
            f"{SYM['id']} الحالة: {'تم الرد' if m['replied'] else 'بانتظار الرد'}\n"
            f"{SYM['id']} الوقت: {fmt_ts(m['created_at'])}\n"
            f"{SYM['line']*20}\n"
        )
        if m["kind"] == "text":
            body += escape(m["content"][:3000])
        else:
            body += f"{SYM['file']} معرف الملف: <code>{m['content'][:60]}</code>"
            if m["file_name"]: body += f"\n{SYM['file']} الاسم: {escape(m['file_name'])}"
            if m["caption"]: body += f"\n{SYM['msg']} الوصف: {escape(m['caption'][:1000])}"
        await _safe_edit(q, body,
                         build_message_actions(mid, m["user_id"], viewer, m["owner_op"], m["replied"]), "HTML")
    elif data.startswith("fwd2:"):
        mid = int(data.split(":")[1])
        if viewer != OPERATOR_ID:
            await q.answer("فقط الأدمن الأول يمكنه التحويل.", show_alert=True)
            return
        if not OPERATOR2_ID:
            await q.answer("لم يتم ضبط الأدمن الثاني (OPERATOR2_ID=0).", show_alert=True)
            return
        m = STORE.get_message(mid)
        if not m:
            await q.answer("الرسالة غير موجودة.", show_alert=True)
            return
        m["owner_op"] = 2
        header2 = (
            f"{SYM['fwd']} <b>رسالة محوّلة إليك</b>\n"
            f"{SYM['line']*18}\n"
            f"{SYM['user']} <a href='tg://user?id={m['user_id']}'>{m['user_id']}</a>\n"
            f"{SYM['id']} <code>{m['user_id']}</code>\n"
            f"{SYM['id']} النوع: {kind_label(m['kind'])}\n"
            f"{SYM['line']*18}"
        )
        try:
            if m["kind"] == "text":
                await context.bot.send_message(OPERATOR2_ID, header2 + "\n" + escape(m["content"]), parse_mode="HTML")
            elif m["kind"] == "photo":
                await context.bot.send_photo(OPERATOR2_ID, m["content"], caption=header2 + "\n" + escape(m["caption"]), parse_mode="HTML")
            elif m["kind"] == "document":
                await context.bot.send_document(OPERATOR2_ID, m["content"], caption=header2 + "\n" + escape(m["caption"]) + f"\n{SYM['file']} {escape(m['file_name'])}", parse_mode="HTML")
            elif m["kind"] == "voice":
                await context.bot.send_voice(OPERATOR2_ID, m["content"], caption=header2, parse_mode="HTML")
            elif m["kind"] == "video":
                await context.bot.send_video(OPERATOR2_ID, m["content"], caption=header2 + "\n" + escape(m["caption"]), parse_mode="HTML")
            elif m["kind"] == "audio":
                await context.bot.send_audio(OPERATOR2_ID, m["content"], caption=header2, parse_mode="HTML")
            elif m["kind"] == "sticker":
                await context.bot.send_sticker(OPERATOR2_ID, m["content"])
                await context.bot.send_message(OPERATOR2_ID, header2, parse_mode="HTML")
            kb2 = build_message_actions(mid, m["user_id"], OPERATOR2_ID, 2, False)
            await context.bot.send_message(OPERATOR2_ID, f"{SYM['bullet']} خيارات الرسالة:", reply_markup=kb2)
            await _safe_edit(q, f"{SYM['ok']} تم تحويل الرسالة إلى الأدمن الثاني.")
        except Exception as e:
            await _safe_edit(q, f"{SYM['fail']} فشل التحويل: {e}")
    elif data.startswith("reply:"):
        mid = int(data.split(":")[1])
        m = STORE.get_message(mid)
        if not m:
            await _safe_edit(q, f"{SYM['fail']} الرسالة غير موجودة.")
            return
        if viewer == OPERATOR_ID:
            allowed = True
        elif viewer == OPERATOR2_ID and m["owner_op"] == 2:
            allowed = True
        else:
            await q.answer("ليست لديك صلاحية الرد على هذه الرسالة.", show_alert=True)
            return
        _pending_reply[viewer] = mid
        await _safe_edit(q,
            f"{SYM['ok']} أرسل الآن ردك للعميل <code>{m['user_id']}</code>.\n"
            f"{SYM['bullet']} سيصل بصيغة: << الرد >>",
            InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['back']} إلغاء", callback_data=f"open:{m['user_id']}")]]),
            "HTML")
    elif data.startswith("log:"):
        uid = int(data.split(":")[1])
        c = STORE.get_client(uid)
        if not c:
            await _safe_edit(q, f"{SYM['fail']} لا يوجد عميل.")
            return
        msgs = [m for m in STORE.messages.values() if m["user_id"] == uid]
        total_in = sum(1 for m in msgs if m["direction"] == "in")
        total_out = sum(1 for m in msgs if m["direction"] == "out")
        answered = sum(1 for m in msgs if m["direction"] == "in" and m["replied"])
        unanswered = total_in - answered
        remain = STORE.get_cooldown_remaining(uid)
        cooldown_line = f"{SYM['clock']} المؤقت المتبقي: {fmt_duration(remain)}" if remain > 0 else f"{SYM['clock']} يمكنه الإرسال الآن"
        body = (
            f"{SYM['log']} <b>سجل العميل</b> <code>{uid}</code>\n"
            f"{SYM['line']*20}\n"
            f"{SYM['id']} الاسم: {escape(c['full_name'] or '—')}\n"
            f"{SYM['id']} اليوزر: {'@'+escape(c['username']) if c['username'] else '—'}\n"
            f"{SYM['id']} محظور: {'نعم' if c['blocked'] else 'لا'}\n"
            f"{SYM['line']*20}\n"
            f"{SYM['id']} أول ظهور: {fmt_ts(c['first_seen'])}\n"
            f"{SYM['id']} آخر نشاط: {fmt_ts(c['last_seen'])}\n"
            f"{SYM['line']*20}\n"
            f"{SYM['id']} رسائل واردة: {total_in}\n"
            f"{SYM['id']} رسائل صادرة: {total_out}\n"
            f"{SYM['id']} تم الرد: {answered}\n"
            f"{SYM['id']} بانتظار الرد: {unanswered}\n"
            f"{cooldown_line}\n"
        )
        await _safe_edit(q, body,
                         InlineKeyboardMarkup([[InlineKeyboardButton(f"{SYM['back']} رجوع", callback_data=f"open:{uid}")]]),
                         "HTML")

# ---------------------------------------------------------------------------
# رد المشغل (مع تنسيق << >>)
# ---------------------------------------------------------------------------
async def o_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_operator(update.effective_user.id):
        return
    viewer = update.effective_user.id
    mid = _pending_reply.pop(viewer, None)
    if not mid:
        return
    m = STORE.get_message(mid)
    if not m:
        await update.message.reply_text(f"{SYM['fail']} الرسالة الأصلية غير موجودة.")
        return
    if viewer == OPERATOR_ID:
        allowed = True
    elif viewer == OPERATOR2_ID and m["owner_op"] == 2:
        allowed = True
    else:
        await update.message.reply_text(f"{SYM['fail']} ليست لديك صلاحية الرد على هذه الرسالة.")
        return

    target_uid = m["user_id"]
    msg = update.message
    try:
        if msg.text:
            formatted = f"<< {msg.text} >>"
            sent = await context.bot.send_message(target_uid, formatted)
            STORE.log_message(target_uid, "out", "text", formatted, "", "", sent.message_id, owner_op=1 if viewer==OPERATOR_ID else 2)

        elif msg.photo:
            cap = msg.caption or ""
            formatted_cap = f"<< {cap} >>" if cap else "<< >>"
            sent = await context.bot.send_photo(target_uid, msg.photo[-1].file_id, caption=formatted_cap)
            STORE.log_message(target_uid, "out", "photo", msg.photo[-1].file_id, formatted_cap, "", sent.message_id, owner_op=1 if viewer==OPERATOR_ID else 2)

        elif msg.document:
            cap = msg.caption or ""
            formatted_cap = f"<< {cap} >>" if cap else "<< >>"
            sent = await context.bot.send_document(target_uid, msg.document.file_id, caption=formatted_cap)
            STORE.log_message(target_uid, "out", "document", msg.document.file_id, formatted_cap, msg.document.file_name or "", sent.message_id, owner_op=1 if viewer==OPERATOR_ID else 2)

        elif msg.voice:
            cap = msg.caption or ""
            formatted_cap = f"<< {cap} >>" if cap else "<< >>"
            sent = await context.bot.send_voice(target_uid, msg.voice.file_id, caption=formatted_cap)
            STORE.log_message(target_uid, "out", "voice", msg.voice.file_id, formatted_cap, "", sent.message_id, owner_op=1 if viewer==OPERATOR_ID else 2)

        elif msg.video:
            cap = msg.caption or ""
            formatted_cap = f"<< {cap} >>" if cap else "<< >>"
            sent = await context.bot.send_video(target_uid, msg.video.file_id, caption=formatted_cap)
            STORE.log_message(target_uid, "out", "video", msg.video.file_id, formatted_cap, "", sent.message_id, owner_op=1 if viewer==OPERATOR_ID else 2)

        elif msg.audio:
            cap = msg.caption or ""
            formatted_cap = f"<< {cap} >>" if cap else "<< >>"
            sent = await context.bot.send_audio(target_uid, msg.audio.file_id, caption=formatted_cap)
            STORE.log_message(target_uid, "out", "audio", msg.audio.file_id, formatted_cap, "", sent.message_id, owner_op=1 if viewer==OPERATOR_ID else 2)

        else:
            await update.message.reply_text(f"{SYM['fail']} نوع غير مدعوم.")
            return

        STORE.mark_answered(mid)
        await update.message.reply_text(
            f"{SYM['ok']} تم إرسال الرد للعميل <code>{target_uid}</code>.\n"
            f"{SYM['bullet']} التنسيق: << الرد >>",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"{SYM['fail']} فشل الإرسال: {e}")

# ---------------------------------------------------------------------------
# الحذف التلقائي كل 20 ساعة
# ---------------------------------------------------------------------------
async def cleanup_task(context: ContextTypes.DEFAULT_TYPE):
    n_c, n_m = STORE.clear_all()
    log.info("Memory cleared: %d clients, %d messages (every %d hours).", n_c, n_m, CLEANUP_HOURS)
    for op in (OPERATOR_ID, OPERATOR2_ID):
        if op:
            try:
                await context.bot.send_message(
                    op,
                    f"{SYM['bullet']} تم مسح السجل تلقائياً ({CLEANUP_HOURS} ساعة).\n"
                    f"{SYM['id']} عملاء محذوفون: {n_c}\n"
                    f"{SYM['id']} رسائل محذوفة: {n_m}"
                )
            except Exception:
                pass

# ---------------------------------------------------------------------------
# البناء
# ---------------------------------------------------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", c_start))
    app.add_handler(CallbackQueryHandler(c_info, pattern="^c_info$"))
    app.add_handler(CallbackQueryHandler(c_send_text_prompt, pattern="^c_send_text$"))
    app.add_handler(CallbackQueryHandler(c_home, pattern="^c_home$"))

    app.add_handler(CommandHandler("op", o_start))
    app.add_handler(CallbackQueryHandler(o_callbacks, pattern="^(page|open|block|del|msgs|mspage|msg|reply|fwd2|log):"))

    app.add_handler(MessageHandler(filters.User(OPERATOR_ID) & ~filters.COMMAND, o_message))
    if OPERATOR2_ID:
        app.add_handler(MessageHandler(filters.User(OPERATOR2_ID) & ~filters.COMMAND, o_message))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, c_message))

    async def post_init(app):
        await app.bot.set_my_commands(
            [BotCommand("start", "بدء الاستخدام")],
            scope=BotCommandScopeAllPrivateChats()
        )
        for op_id in (OPERATOR_ID, OPERATOR2_ID):
            if op_id:
                await app.bot.set_my_commands(
                    [BotCommand("op", "لوحة المشغل")],
                    scope=BotCommandScopeChat(chat_id=op_id)
                )
        app.job_queue.run_repeating(cleanup_task, interval=CLEANUP_SECONDS, first=CLEANUP_SECONDS)
        log.info("Bot ready. OPs: %s, %s | Cleanup: %dh | Cooldown: 5/15min",
                 OPERATOR_ID, OPERATOR2_ID, CLEANUP_HOURS)

    app.post_init = post_init
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()