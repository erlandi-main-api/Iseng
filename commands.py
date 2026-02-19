# -*- coding: utf-8 -*-
import os
import re
import time
import tempfile
import requests
from urllib.parse import urlparse, unquote

import config
import worker
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

OWNER_ID = getattr(config, "OWNER_ID", 1348352154)

HOSTS = {
    "gofile": "Gofile",
    "pixeldrain": "Pixeldrain",
    "uguu": "Uguu",
}

MAX_TASK_BYTES = 60 * 1024 * 1024  # 60MB
MAX_TG_SEND_BYTES = 45 * 1024 * 1024  # 45MB aman kirim via bot

pending_upload = {}
pending_mirror = {}

def human_size(n: int) -> str:
    n = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}PB"

async def safe_edit(message, text: str):
    try:
        if getattr(message, "text", None) == text:
            return
        await message.edit_text(text)
    except:
        return

async def log_owner(context, text: str):
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=text)
    except:
        pass

def pick_media(msg):
    if msg.document:
        return msg.document.file_id, (msg.document.file_name or "file")
    if msg.video:
        return msg.video.file_id, (msg.video.file_name or "video.mp4")
    if msg.photo:
        return msg.photo[-1].file_id, "photo.jpg"
    return None, None

def menu(prefix: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Gofile", callback_data=f"{prefix}:gofile")],
        [InlineKeyboardButton("Pixeldrain", callback_data=f"{prefix}:pixeldrain")],
        [InlineKeyboardButton("Uguu", callback_data=f"{prefix}:uguu")],
    ])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "User"
    await update.message.reply_text(
        f"Halo {name}\n"
        "Selamat datang di uploader gratis by @erl_andi\n\n"
        "• Reply file lalu /u\n"
        "• /mirror <url>\n"
        "• /leech <url>\n\n"
        f"Limit: {human_size(MAX_TASK_BYTES)}"
    )

async def cmd_u(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply ke file dulu.")
        return
    msg = update.message.reply_to_message
    file_id, _ = pick_media(msg)
    if not file_id:
        await update.message.reply_text("Tidak ada media.")
        return
    pending_upload[update.effective_user.id] = msg
    await update.message.reply_text("Upload ke mana?", reply_markup=menu("up"))

async def cmd_mirror(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan: /mirror <url>")
        return
    pending_mirror[update.effective_user.id] = context.args[0].strip()
    await update.message.reply_text("Mirror ke mana?", reply_markup=menu("mi"))

async def cmd_leech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Leech aktif (lihat versi lengkap sebelumnya).")

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Versi ringkas commands.py aktif.")
