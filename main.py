import os
import threading
import time
import asyncio
import logging
import requests
import pymongo
import subprocess
import re
from concurrent.futures import ThreadPoolExecutor
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from google import genai
from google.genai.errors import APIError
from flask import Flask, render_template_string, jsonify

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
PORT = int(os.environ.get("PORT", "8080"))
REQUEST_TIMEOUT_GEMINI = int(os.environ.get("REQUEST_TIMEOUT_GEMINI", "60"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHUNK = 4095
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite")
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6964068910"))
DB_USER = os.environ.get("DB_USER", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_APPNAME = os.environ.get("DB_APPNAME", "SpeechBot")
MONGO_URI = os.environ.get("MONGO_URI") or f"mongodb+srv://{DB_USER}:{DB_PASSWORD}@cluster0.n4hdlxk.mongodb.net/{DB_APPNAME}?retryWrites=true&w=majority&appName={DB_APPNAME}"

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

user_gemini_keys = {}
user_mode = {}
user_transcriptions = {}
user_model_usage = {}
MAX_USAGE_COUNT = 18
PRIMARY_MODEL = GEMINI_MODEL
FALLBACK_MODEL = GEMINI_FALLBACK_MODEL

EXECUTOR = ThreadPoolExecutor(max_workers=2)
SEMAPHORE = asyncio.Semaphore(2)

app = Client("bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
client_mongo = None
db = None
users_col = None

try:
    client_mongo = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client_mongo.admin.command("ping")
    if client_mongo:
        db = client_mongo[DB_APPNAME] if DB_APPNAME else client_mongo.get_default_database()
    if db is not None:
        users_col = db.get_collection("users")
        users_col.create_index("user_id", unique=True)
        cursor = users_col.find({}, {"user_id": 1, "gemini_key": 1})
        for doc in cursor:
            user_gemini_keys[int(doc["user_id"])] = doc.get("gemini_key")
except Exception as e:
    logging.warning("MongoDB connection failed: %s", e)

def get_current_model(uid):
    current_usage = user_model_usage.get(uid, {"primary_count": 0, "fallback_count": 0, "current_model": PRIMARY_MODEL})
    if current_usage["current_model"] == PRIMARY_MODEL:
        if current_usage["primary_count"] < MAX_USAGE_COUNT:
            current_usage["primary_count"] += 1
            user_model_usage[uid] = current_usage
            return PRIMARY_MODEL
        else:
            current_usage["current_model"] = FALLBACK_MODEL
            current_usage["primary_count"] = 0
            current_usage["fallback_count"] = 1
            user_model_usage[uid] = current_usage
            return FALLBACK_MODEL
    elif current_usage["current_model"] == FALLBACK_MODEL:
        if current_usage["fallback_count"] < MAX_USAGE_COUNT:
            current_usage["fallback_count"] += 1
            user_model_usage[uid] = current_usage
            return FALLBACK_MODEL
        else:
            current_usage["current_model"] = PRIMARY_MODEL
            current_usage["fallback_count"] = 0
            current_usage["primary_count"] = 1
            user_model_usage[uid] = current_usage
            return PRIMARY_MODEL
    return PRIMARY_MODEL

def set_user_key_db(uid, key):
    try:
        if users_col is not None:
            users_col.update_one({"user_id": uid}, {"$set": {"gemini_key": key, "updated_at": time.time()}}, upsert=True)
        user_gemini_keys[uid] = key
    except Exception as e:
        user_gemini_keys[uid] = key

def get_user_key_db(uid):
    if uid in user_gemini_keys:
        return user_gemini_keys[uid]
    try:
        if users_col is not None:
            doc = users_col.find_one({"user_id": uid})
            if doc:
                key = doc.get("gemini_key")
                user_gemini_keys[uid] = key
                return key
    except Exception as e:
        pass
    return user_gemini_keys.get(uid)

def _ffmpeg_sync(input_path):
    base = os.path.splitext(input_path)[0]
    output_path = f"{base}_opt.mp3"
    cmd = ["ffmpeg", "-i", input_path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", "-y", output_path]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_path
    except Exception:
        return None

async def ffmpeg_convert_to_opt(input_path):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(EXECUTOR, _ffmpeg_sync, input_path)

def upload_and_transcribe_gemini(file_path: str, key: str, uid: int) -> str:
    client = genai.Client(api_key=key)
    uploaded_file = None
    try:
        uploaded_file = client.files.upload(file=file_path)
        prompt = "Transcribe the audio in this file Provide a clean text that does not look like raw STT. Return ONLY the transcription text, no preamble or extra commentary"
        current_model = get_current_model(uid)
        response = client.models.generate_content(model=current_model, contents=[prompt, uploaded_file])
        return response.text
    finally:
        if uploaded_file:
            try: client.files.delete(name=uploaded_file.name)
            except: pass
        if os.path.exists(file_path):
            try: os.remove(file_path)
            except: pass

def ask_gemini(text, instruction, key, uid):
    client = genai.Client(api_key=key)
    prompt = f"{instruction}\n\n{text}"
    current_model = get_current_model(uid)
    response = client.models.generate_content(model=current_model, contents=[prompt])
    return response.text

def build_action_keyboard(text_len):
    btns = []
    if text_len > 2000:
        btns.append([InlineKeyboardButton("Get Summarize", callback_data="summarize_menu|")])
    return InlineKeyboardMarkup(btns)

def build_summarize_keyboard(origin):
    btns = [
        [InlineKeyboardButton("Short", callback_data=f"summopt|Short|{origin}")],
        [InlineKeyboardButton("Detailed", callback_data=f"summopt|Detailed|{origin}")],
        [InlineKeyboardButton("Bulleted", callback_data=f"summopt|Bulleted|{origin}")]
    ]
    return InlineKeyboardMarkup(btns)

progress_controls = {}

async def _progress_updater(client, chat_id, progress_msg_id, finished_event, stop_event):
    percent = 0
    while not finished_event.is_set() and not stop_event.is_set():
        if percent < 90:
            percent += 4
        else:
            percent += 1
        if percent > 95:
            percent = 95
        try:
            await client.edit_message_text(chat_id, progress_msg_id, f"Processing {percent}% 🔄", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=f"progress_cancel|{progress_msg_id}")]]))
        except Exception:
            pass
        await asyncio.sleep(1)
    if stop_event.is_set():
        try:
            await client.edit_message_text(chat_id, progress_msg_id, "Processing cancelled ❌")
        except Exception:
            pass
    else:
        try:
            await client.edit_message_text(chat_id, progress_msg_id, "Processing 100% ✅")
        except Exception:
            pass
    progress_controls.pop(f"{chat_id}:{progress_msg_id}", None)

async def start_progress_ui(client, chat_id, reply_to):
    msg = await client.send_message(chat_id, "Processing 0% 🔄", reply_to_message_id=reply_to, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="progress_cancel|start")]]))
    finished_event = asyncio.Event()
    stop_event = asyncio.Event()
    task = asyncio.create_task(_progress_updater(client, chat_id, msg.id, finished_event, stop_event))
    progress_controls[f"{chat_id}:{msg.id}"] = {"finished_event": finished_event, "stop_event": stop_event, "task": task}
    return msg, finished_event, stop_event

@app.on_callback_query(filters.regex(r"^progress_cancel\|"))
async def progress_cancel_cb(client, call):
    parts = call.data.split("|")
    target = parts[1] if len(parts) > 1 else ""
    if target == "start":
        controllers = [v for k, v in progress_controls.items() if k.startswith(f"{call.message.chat.id}:")]
        for c in controllers:
            c["stop_event"].set()
        await call.answer("Cancelling...")
        return
    try:
        progress_msg_id = int(target)
    except:
        await call.answer("Unable to cancel", show_alert=True)
        return
    key = f"{call.message.chat.id}:{progress_msg_id}"
    ctrl = progress_controls.get(key)
    if ctrl:
        ctrl["stop_event"].set()
        await call.answer("Cancelled")
    else:
        await call.answer("No active task")

@app.on_message(filters.command(["start", "help"]))
async def send_welcome(client, message):
    welcome_text = "👋 Salaam!\n• Send me\n• voice message\n• audio file\n• video\n• to transcribe for free for any problem report https://t.me/osiy3"
    main_kb = ReplyKeyboardMarkup([["Change Result mode 🐥"]], resize_keyboard=True, placeholder="Message")
    await message.reply_text(welcome_text, reply_markup=main_kb, quote=True)

@app.on_message(filters.regex(r"^Change Result mode 🐥$"))
async def choose_mode_btn(client, message):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Split messages", callback_data="mode|Split messages")],
        [InlineKeyboardButton("📄 Text File", callback_data="mode|Text File")]
    ])
    await message.reply_text("How do I send you long transcripts?:", reply_markup=kb, quote=True)

@app.on_message(filters.regex(r"^AIz"))
async def set_key_plain(client, message):
    token = message.text.strip().split()[0]
    if not token.startswith("AIz"):
        return
    prev = get_user_key_db(message.from_user.id)
    set_user_key_db(message.from_user.id, token)
    msg = "API key updated." if prev else "Okay send me audio or video 👍"
    await message.reply_text(msg, quote=True)

@app.on_callback_query(filters.regex(r"^mode\|"))
async def mode_cb(client, call):
    mode = call.data.split("|")[1]
    user_mode[call.from_user.id] = mode
    try: await call.edit_message_text(f"you choosed: {mode}")
    except: pass
    await call.answer(f"Mode set to: {mode} ☑️")

@app.on_callback_query(filters.regex(r"^summarize_menu\|"))
async def summarize_menu_cb(client, call):
    try: await call.edit_message_reply_markup(reply_markup=build_summarize_keyboard(call.message.id))
    except: pass

@app.on_callback_query(filters.regex(r"^summopt\|"))
async def summopt_cb(client, call):
    try:
        _, style, origin = call.data.split("|")
        await call.edit_message_reply_markup(reply_markup=None)
    except: return
    prompts = {
        "Short": "Summarize this text in the original language in which it is written in 1-2 concise sentences.",
        "Detailed": "Summarize this text in the original language in which it is written in a detailed paragraph.",
        "Bulleted": "Summarize this text in the original language in which it is written as a bulleted list."
    }
    await process_text_action(client, call, origin, f"Summarize ({style})", prompts.get(style))

@app.on_message(filters.voice | filters.audio | filters.video | filters.document)
async def handle_media(client, message):
    media = message.voice or message.audio or message.video or message.document
    if not media or getattr(media, "file_size", 0) > MAX_UPLOAD_SIZE:
        if media: await message.reply_text(f"Send me file less than {MAX_UPLOAD_MB}MB 😎", quote=True)
        return
    user_key = get_user_key_db(message.from_user.id)
    if not user_key:
        await message.reply_text("first send me Gemini key 🤓", quote=True)
        if REQUIRED_CHANNEL:
            try:
                chat_info = await client.get_chat(REQUIRED_CHANNEL)
                pinned = chat_info.pinned_message
                if pinned:
                    await pinned.forward(message.chat.id)
            except Exception:
                pass
        return

    await client.send_chat_action(message.chat.id, enums.ChatAction.TYPING)
    file_path = os.path.join(DOWNLOADS_DIR, f"temp_{message.id}_{media.file_unique_id}")
    progress_msg = None
    finished_event = None
    stop_event = None
    try:
        progress_msg, finished_event, stop_event = await start_progress_ui(client, message.chat.id, message.id)
        d_path = await client.download_media(message, file_name=file_path)
        async with SEMAPHORE:
            opt_path = await ffmpeg_convert_to_opt(d_path)
            final_path = opt_path if opt_path else d_path
            if opt_path and d_path != opt_path:
                try: os.remove(d_path)
                except: pass
            loop = asyncio.get_running_loop()
            text_task = loop.run_in_executor(EXECUTOR, upload_and_transcribe_gemini, final_path, user_key, message.from_user.id)
            text = await asyncio.wait_for(text_task, REQUEST_TIMEOUT_GEMINI)
        if finished_event:
            finished_event.set()
        if stop_event and stop_event.is_set():
            await message.reply_text("Processing was cancelled.", quote=True)
            return
        if text:
            sent = await send_long_text(client, message.chat.id, text, message.id, message.from_user.id)
            if sent:
                user_transcriptions.setdefault(message.chat.id, {})[sent.id] = {"text": text, "origin": message.id, "ts": time.time()}
                if len(text) > 2000:
                    await client.edit_message_reply_markup(message.chat.id, sent.id, reply_markup=build_action_keyboard(len(text)))
    except Exception as e:
        if finished_event:
            finished_event.set()
        if progress_msg:
            try:
                await client.edit_message_text(message.chat.id, progress_msg.id, f"❌ Error: {e}")
            except Exception:
                pass
        await message.reply_text(f"❌ Error: {e}", quote=True)

async def process_text_action(client, call, origin_msg_id, log_action, prompt_instr):
    chat_id = call.message.chat.id
    try: origin_id = int(origin_msg_id)
    except: origin_id = call.message.id
    data = user_transcriptions.get(chat_id, {}).get(origin_id)
    if not data and call.message.reply_to_message:
        data = user_transcriptions.get(chat_id, {}).get(call.message.reply_to_message.id)
    if not data:
        await call.answer("Data expired. Resend file.", show_alert=True)
        return
    user_key = get_user_key_db(call.from_user.id)
    if not user_key: return
    await call.answer("Processing...")
    await client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    progress_msg = None
    finished_event = None
    stop_event = None
    try:
        progress_msg, finished_event, stop_event = await start_progress_ui(client, chat_id, data.get("origin", call.message.id))
        async with SEMAPHORE:
            loop = asyncio.get_running_loop()
            ask_task = loop.run_in_executor(EXECUTOR, ask_gemini, data["text"], prompt_instr, user_key, call.from_user.id)
            res = await asyncio.wait_for(ask_task, REQUEST_TIMEOUT_GEMINI)
        if finished_event:
            finished_event.set()
        if stop_event and stop_event.is_set():
            await call.message.reply_text("Processing was cancelled.", quote=True)
            return
        await send_long_text(client, chat_id, res, data["origin"], call.from_user.id, log_action)
    except Exception as e:
        if finished_event:
            finished_event.set()
        try:
            await client.send_message(chat_id, f"❌ Error: {e}")
        except Exception:
            pass

async def send_long_text(client, chat_id, text, reply_id, uid, action="Transcript"):
    mode = user_mode.get(uid, "Split messages")
    if len(text) > MAX_MESSAGE_CHUNK:
        if mode == "Split messages":
            sent = None
            for i in range(0, len(text), MAX_MESSAGE_CHUNK):
                sent = await client.send_message(chat_id, text[i:i+MAX_MESSAGE_CHUNK], reply_to_message_id=reply_id)
            return sent
        else:
            fname = os.path.join(DOWNLOADS_DIR, f"{action}.txt")
            with open(fname, "w", encoding="utf-8") as f: f.write(text)
            sent = await client.send_document(chat_id, fname, caption="Open this file and copy the text inside 👍", reply_to_message_id=reply_id)
            try: os.remove(fname)
            except: pass
            return sent
    return await client.send_message(chat_id, text, reply_to_message_id=reply_id)

def _start_cleanup_thread():
    def cleaner():
        while True:
            try:
                now = time.time()
                cutoff = now - (15 * 60)
                for chat_id, msgs in list(user_transcriptions.items()):
                    to_del = [mid for mid, meta in list(msgs.items()) if meta.get("ts", 0) < cutoff]
                    for mid in to_del:
                        try: del msgs[mid]
                        except: pass
                    if not msgs:
                        try: del user_transcriptions[chat_id]
                        except: pass
            except Exception: pass
            time.sleep(60)
    threading.Thread(target=cleaner, daemon=True).start()

flask_app = Flask(__name__)
@flask_app.route("/")
def index():
    return "Bot Online"

if __name__ == "__main__":
    _start_cleanup_thread()
    threading.Thread(target=flask_app.run, kwargs={"host":"0.0.0.0","port":PORT}, daemon=True).start()
    app.run()
