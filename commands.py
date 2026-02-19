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
MAX_TG_SEND_BYTES = 45 * 1024 * 1024  # 45MB aman (bot send)

pending_upload = {}  # uid -> reply_to_message object
pending_mirror = {}  # uid -> url string

def human_size(n: int) -> str:
    n = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}PB"

async def safe_edit(message, text: str):
    try:
        cur = getattr(message, "text", None)
        if cur == text:
            return
        await message.edit_text(text)
    except Exception as e:
        if "Message is not modified" in str(e):
            return
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

def build_headers(url: str):
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
        "Referer": origin + "/",
        "Origin": origin,
        "Connection": "keep-alive",
    }

def sanitize_filename(name: str, fallback="file") -> str:
    name = (name or "").strip()
    if not name:
        name = fallback
    name = re.sub(r"[\r\n\t\0]+", "", name)
    if len(name) > 180:
        root, ext = os.path.splitext(name)
        name = root[:160] + ext[:20]
    return name

def extract_filename(headers, url, fallback="file"):
    cd = headers.get("content-disposition") or headers.get("Content-Disposition") or ""
    cd_low = cd.lower()

    if "filename*=" in cd_low:
        m = re.search(r"filename\*=(?:utf-8''|UTF-8'')?([^;]+)", cd, flags=re.IGNORECASE)
        if m:
            val = m.group(1).strip().strip('"').strip()
            try:
                val = unquote(val)
            except:
                pass
            return sanitize_filename(val, fallback=fallback)

    m2 = re.search(r'filename="?([^";]+)"?', cd)
    if m2:
        return sanitize_filename(m2.group(1), fallback=fallback)

    path = urlparse(url).path
    base = os.path.basename(path) or fallback
    return sanitize_filename(base, fallback=fallback)

def pixeldrain_id_from_url(url: str):
    if "pixeldrain.com/u/" in url:
        return url.split("/u/")[-1].split("?")[0].strip()
    if "pixeldrain.com/api/file/" in url:
        return url.split("/api/file/")[-1].split("?")[0].strip()
    return None

def pixeldrain_download_url(file_id: str):
    return f"https://pixeldrain.com/api/file/{file_id}"

def pixeldrain_info_url(file_id: str):
    return f"https://pixeldrain.com/api/file/{file_id}/info"

def try_get_pixeldrain_filename(file_id: str):
    try:
        r = requests.get(pixeldrain_info_url(file_id), timeout=30)
        if r.status_code != 200:
            return None
        j = r.json()
        for key in ["name", "filename", "file_name"]:
            if key in j and isinstance(j[key], str) and j[key].strip():
                return j[key].strip()
    except:
        return None
    return None

def menu(prefix: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Gofile", callback_data=f"{prefix}:gofile")],
        [InlineKeyboardButton("Pixeldrain", callback_data=f"{prefix}:pixeldrain")],
        [InlineKeyboardButton("Uguu", callback_data=f"{prefix}:uguu")],
    ])

async def download_to_tmp(url: str, status_msg, filename_hint: str, max_bytes: int):
    headers = build_headers(url)
    r = requests.get(url, stream=True, timeout=600, allow_redirects=True, headers=headers)

    if r.status_code == 451:
        headers2 = headers.copy()
        headers2.pop("Origin", None)
        headers2.pop("Referer", None)
        r = requests.get(url, stream=True, timeout=600, allow_redirects=True, headers=headers2)

    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code}")

    total = int(r.headers.get("content-length") or 0)
    if total > 0 and total > max_bytes:
        raise Exception(f"File terlalu besar: {human_size(total)} > limit {human_size(max_bytes)}")

    filename = sanitize_filename(extract_filename(r.headers, url, fallback=filename_hint), fallback=filename_hint)

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = tmp.name

    downloaded = 0
    last_update = 0.0
    last_data_time = time.time()
    last_shown = ""

    try:
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    if time.time() - last_data_time > 60:
                        raise Exception("Timeout: tidak ada data masuk selama 60 detik.")
                    continue

                f.write(chunk)
                downloaded += len(chunk)
                last_data_time = time.time()

                if downloaded > max_bytes:
                    raise Exception(f"File terlalu besar: > limit {human_size(max_bytes)}")

                now = time.time()
                if now - last_update >= 5:
                    last_update = now
                    if total > 0:
                        percent = int(downloaded * 100 / total)
                        text = f"Download: {percent}% ({human_size(downloaded)}/{human_size(total)})"
                    else:
                        text = f"Download: {human_size(downloaded)}"
                    if text != last_shown:
                        last_shown = text
                        await safe_edit(status_msg, text)

        try:
            r.close()
        except:
            pass

        size = os.path.getsize(tmp_path)
        if size > max_bytes:
            raise Exception(f"File terlalu besar: {human_size(size)} > limit {human_size(max_bytes)}")

        await safe_edit(status_msg, f"Download selesai: {human_size(size)}")
        return tmp_path, filename, size

    except:
        try:
            r.close()
        except:
            pass
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except:
            pass
        raise

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "User"
    await update.message.reply_text(
        f"Halo {name}
"
        "Selamat datang di uploader gratis by @erl_andi

"
        "• Reply file lalu /u (upload pilih tombol)
"
        "• /mirror <url> (mirror URL pilih tombol)
"
        "• /leech <url> (download URL lalu kirim ke Telegram jika memungkinkan)

"
        f"Limit leech/mirror: {human_size(MAX_TASK_BYTES)}"
    )

async def cmd_u(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply ke file dulu.")
        return

    msg = update.message.reply_to_message
    file_id, _ = pick_media(msg)
    if not file_id:
        await update.message.reply_text("Pesan tidak berisi media (document/video/photo).")
        return

    pending_upload[update.effective_user.id] = msg
    await update.message.reply_text("Upload ke mana?", reply_markup=menu("up"))

async def cmd_mirror(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan: /mirror <url>")
        return

    url = context.args[0].strip()
    pending_mirror[update.effective_user.id] = url
    await update.message.reply_text("Mirror ke mana?", reply_markup=menu("mi"))

async def cmd_leech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan: /leech <url>")
        return

    raw_url = context.args[0].strip()
    status = await update.message.reply_text("Memulai download...")

    tmp_path = None
    try:
        pd_id = pixeldrain_id_from_url(raw_url)
        if pd_id:
            url = pixeldrain_download_url(pd_id)
            name_hint = try_get_pixeldrain_filename(pd_id) or "file"
        else:
            url = raw_url
            name_hint = "file"

        tmp_path, filename, size = await download_to_tmp(url, status, name_hint, MAX_TASK_BYTES)

        if size > MAX_TG_SEND_BYTES:
            await safe_edit(
                status,
                "❌ File terlalu besar untuk dikirim oleh bot ke Telegram.
"
                f"Ukuran: {human_size(size)} | Batas aman bot: {human_size(MAX_TG_SEND_BYTES)}

"
                "Solusi:
"
                "• Gunakan /mirror <url> lalu pilih host untuk dapat link.
"
                "• Atau gunakan file versi lebih kecil."
            )
            await log_owner(context, f"Leech ditolak (TG limit) oleh {update.effective_user.id}
{raw_url}
Size: {human_size(size)}")
            return

        await safe_edit(status, "Upload ke Telegram... (bisa beberapa menit)")
        with open(tmp_path, "rb") as fp:
            await update.message.reply_document(document=fp, filename=filename)

        await safe_edit(status, "Selesai ✅")
        await log_owner(context, f"Leech oleh {update.effective_user.id}
{raw_url}
Size: {human_size(size)}
File: {filename}")

    except Exception as e:
        await safe_edit(status, f"❌ Error:
{e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except: pass

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    uid = q.from_user.id
    data = q.data or ""

    if data.startswith("up:"):
        host = data.split(":", 1)[1]
        if uid not in pending_upload:
            await q.edit_message_text("File tidak ditemukan (ulang /u).")
            return

        msg_obj = pending_upload[uid]
        file_id, filename = pick_media(msg_obj)
        tg_file = await context.bot.get_file(file_id)

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name

        await q.edit_message_text("Download: 0%")
        await tg_file.download_to_drive(custom_path=tmp_path)
        await safe_edit(q.message, "Download: 100%")

        try:
            link = worker.upload_file(tmp_path, filename, host)
        finally:
            try: os.remove(tmp_path)
            except: pass

        await safe_edit(q.message, f"✅ Upload ({HOSTS.get(host, host)}):
{link}")
        await q.message.chat.send_message(f"✅ Upload ({HOSTS.get(host, host)}):
{link}")
        await log_owner(context, f"Upload oleh {uid}
Host: {host}
{link}")

        pending_upload.pop(uid, None)
        return

    if data.startswith("mi:"):
        host = data.split(":", 1)[1]
        if uid not in pending_mirror:
            await q.edit_message_text("URL tidak ditemukan (ulang /mirror).")
            return

        raw_url = pending_mirror[uid]
        await q.edit_message_text(f"Memulai download...
Target: {HOSTS.get(host, host)}")

        tmp_path = None
        try:
            pd_id = pixeldrain_id_from_url(raw_url)
            if pd_id:
                url = pixeldrain_download_url(pd_id)
                name_hint = try_get_pixeldrain_filename(pd_id) or "file"
            else:
                url = raw_url
                name_hint = "file"

            tmp_path, filename, size = await download_to_tmp(url, q.message, name_hint, MAX_TASK_BYTES)

            await safe_edit(q.message, f"Upload ke {HOSTS.get(host, host)}... ({human_size(size)})")
            link = worker.upload_file(tmp_path, filename, host)

            await safe_edit(q.message, f"✅ Mirror ({HOSTS.get(host, host)}) selesai:
{link}")
            await q.message.chat.send_message(
                f"✅ Mirror ({HOSTS.get(host, host)}) selesai
"
                f"Nama: {filename}
"
                f"Ukuran: {human_size(size)}
"
                f"Link: {link}"
            )
            await log_owner(context, f"Mirror oleh {uid}
Host: {host}
Size: {human_size(size)}
{raw_url}
{link}")

        except Exception as e:
            await safe_edit(q.message, f"❌ Error:
{e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except: pass
            pending_mirror.pop(uid, None)
        return

    await q.edit_message_text("Aksi tidak dikenali.")
