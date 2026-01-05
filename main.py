import os
import threading
import time
import logging
import requests
import pymongo
import subprocess
import random
import glob
from concurrent.futures import ThreadPoolExecutor
from telebot import TeleBot, types
from google import genai
from google.genai.errors import APIError
from flask import Flask, request, abort, jsonify

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
REQUEST_TIMEOUT_GEMINI = int(os.environ.get("REQUEST_TIMEOUT_GEMINI", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
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
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

user_gemini_keys = {}
user_mode = {}
user_transcriptions = {}
user_model_usage = {}
MAX_USAGE_COUNT = 18
PRIMARY_MODEL = GEMINI_MODEL
FALLBACK_MODEL = GEMINI_FALLBACK_MODEL

EXECUTOR = ThreadPoolExecutor(max_workers=4)

bot = TeleBot(BOT_TOKEN, threaded=True)
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
    except Exception:
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
    except Exception:
        pass
    return user_gemini_keys.get(uid)

def upload_and_transcribe_gemini(file_path: str, key: str, uid: int) -> str:
    client = genai.Client(api_key=key)
    uploaded_file = None
    try:
        uploaded_file = client.files.upload(file=file_path)
        prompt = """
Transcribe the audio accurately in its original language.

Formatting rules:
- Preserve the original meaning exactly
- Add proper punctuation
- Split the text into short, readable paragraphs
- Each paragraph should represent one clear idea
- Avoid long blocks of text
- Remove filler words only if meaning is unchanged
- Do NOT summarize
- Do NOT add explanations

Return ONLY the final formatted transcription.
"""
        current_model = get_current_model(uid)
        response = client.models.generate_content(model=current_model, contents=[prompt, uploaded_file])
        return response.text
    finally:
        if uploaded_file:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

def transcribe_direct(file_path: str, key: str, uid: int) -> str:
    try:
        return upload_and_transcribe_gemini(file_path, key, uid)
    except Exception:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass
        return ""

def ask_gemini(text, instruction, key, uid):
    client = genai.Client(api_key=key)
    prompt = f"{instruction}\n\n{text}"
    current_model = get_current_model(uid)
    response = client.models.generate_content(model=current_model, contents=[prompt])
    return response.text

def build_action_keyboard(text_len):
    markup = types.InlineKeyboardMarkup()
    if text_len > 2000:
        markup.add(types.InlineKeyboardButton("Get Summarize", callback_data="summarize_menu|"))
    return markup

def build_summarize_keyboard(origin):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Short", callback_data=f"summopt|Short|{origin}"))
    markup.add(types.InlineKeyboardButton("Detailed", callback_data=f"summopt|Detailed|{origin}"))
    markup.add(types.InlineKeyboardButton("Bulleted", callback_data=f"summopt|Bulleted|{origin}"))
    return markup

def progress_thread_fn(chat_id, reply_to_message_id, stop_event, label="Processing"):
    percent = 0
    bars = 12
    bar_empty = "░"
    bar_full = "█"
    try:
        progress_msg = bot.send_message(chat_id, f"{label}: 0% [{bar_empty * bars}]", reply_to_message_id=reply_to_message_id)
    except Exception:
        progress_msg = None
    try:
        while not stop_event.is_set():
            increment = random.randint(6, 14)
            percent = min(95, percent + increment)
            filled = int(percent * bars / 100)
            bar = bar_full * filled + bar_empty * (bars - filled)
            text = f"{label}: {percent}% [{bar}]"
            if progress_msg:
                try:
                    bot.edit_message_text(text, chat_id, progress_msg.message_id)
                except Exception:
                    pass
            time.sleep(0.8)
        time.sleep(0.2)
        percent = 100
        bar = bar_full * bars
        final_text = f"{label}: {percent}% [{bar}] ✅"
        if progress_msg:
            try:
                bot.edit_message_text(final_text, chat_id, progress_msg.message_id)
            except Exception:
                pass
            try:
                bot.delete_message(chat_id, progress_msg.message_id)
            except Exception:
                pass
    except Exception:
        pass

@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    welcome_text = "👋 Salaam!\n• Send me\n• voice message\n• audio file\n• video\n• to transcribe for free for any problem report https://t.me/orlaki"
    main_kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    main_kb.add("Change Result mode 🐥")
    bot.reply_to(message, welcome_text, reply_markup=main_kb)

@bot.message_handler(func=lambda m: m.text == "Change Result mode 🐥")
def choose_mode_btn(message):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💬 Split messages", callback_data="mode|Split messages"))
    kb.add(types.InlineKeyboardButton("📄 Text File", callback_data="mode|Text File"))
    bot.reply_to(message, "How do I send you long transcripts?:", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text and m.text.strip().startswith("AIz"))
def set_key_plain(message):
    token = message.text.strip().split()[0]
    if not token.startswith("AIz"):
        return
    prev = get_user_key_db(message.from_user.id)
    set_user_key_db(message.from_user.id, token)
    msg = "API key updated." if prev else "Okay send me audio or video 👍"
    bot.reply_to(message, msg)
    if not prev:
        try:
            uname = message.from_user.username or ""
            fname = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip()
            admin_msg = f"New user provided Gemini key\nuser_id: {message.from_user.id}\nusername: {uname}\nname: {fname}"
            bot.send_message(ADMIN_ID, admin_msg)
        except Exception:
            pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("mode|"))
def mode_cb(call):
    mode = call.data.split("|")[1]
    user_mode[call.from_user.id] = mode
    try:
        bot.edit_message_text(f"you choosed: {mode}", call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    try:
        bot.answer_callback_query(call.id, f"Mode set to: {mode} ☑️")
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("summarize_menu|"))
def summarize_menu_cb(call):
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=build_summarize_keyboard(call.message.message_id))
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("summopt|"))
def summopt_cb(call):
    try:
        _, style, origin = call.data.split("|")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        return
    prompts = {
        "Short": "Summarize this text in the original language in which it is written in 1-2 concise sentences.",
        "Detailed": "Summarize this text in the original language in which it is written in a detailed paragraph.",
        "Bulleted": "Summarize this text in the original language in which it is written as a bulleted list."
    }
    threading.Thread(target=process_text_action, args=(call, origin, f"Summarize ({style})", prompts.get(style)), daemon=True).start()

def handle_file_download(file_info, dest_path):
    file_path = file_info.file_path
    file_bytes = bot.download_file(file_path)
    with open(dest_path, "wb") as f:
        f.write(file_bytes)
    return dest_path

@bot.message_handler(func=lambda m: m.voice or m.audio or m.video or m.document)
def handle_media(message):
    media = message.voice or message.audio or message.video or message.document
    file_size = getattr(media, "file_size", 0)
    if not media or file_size > MAX_UPLOAD_SIZE:
        if media:
            bot.reply_to(message, f"Send me file less than {MAX_UPLOAD_MB}MB 😎")
        return
    user_key = get_user_key_db(message.from_user.id)
    if not user_key:
        bot.reply_to(message, "first send me Gemini key 🤓")
        if REQUIRED_CHANNEL:
            try:
                bot.forward_message(message.chat.id, REQUIRED_CHANNEL, 0)
            except Exception:
                pass
        return
    try:
        bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass
    file_id = media.file_id
    temp_name = f"temp_{message.message_id}_{int(time.time())}"
    d_path = os.path.join(DOWNLOADS_DIR, temp_name)
    try:
        file_info = bot.get_file(file_id)
        d_file = handle_file_download(file_info, d_path)
        stop_event = threading.Event()
        progress_thread = threading.Thread(target=progress_thread_fn, args=(message.chat.id, message.message_id, stop_event, "Transcribing"), daemon=True)
        progress_thread.start()
        future = EXECUTOR.submit(transcribe_direct, d_file, user_key, message.from_user.id)
        try:
            text = future.result(timeout=REQUEST_TIMEOUT_GEMINI)
        except Exception as e:
            text = ""
        stop_event.set()
        if text:
            sent = send_long_text(message.chat.id, text, message.message_id, message.from_user.id)
            if sent:
                user_transcriptions.setdefault(message.chat.id, {})[sent.message_id if hasattr(sent, "message_id") else sent.json().get("message_id")] = {"text": text, "origin": message.message_id, "ts": time.time()}
                if len(text) > 2000:
                    try:
                        bot.edit_message_reply_markup(message.chat.id, sent.message_id, reply_markup=build_action_keyboard(len(text)))
                    except Exception:
                        pass
        else:
            bot.reply_to(message, "❌ Error transcribing file", reply_to_message_id=message.message_id)
    except Exception as e:
        try:
            bot.reply_to(message, f"❌ Error: {e}")
        except Exception:
            pass

def process_text_action(call, origin_msg_id, log_action, prompt_instr):
    chat_id = call.message.chat.id
    try:
        origin_id = int(origin_msg_id)
    except Exception:
        origin_id = call.message.message_id
    data = user_transcriptions.get(chat_id, {}).get(origin_id)
    if not data and call.message.reply_to_message:
        data = user_transcriptions.get(chat_id, {}).get(call.message.reply_to_message.message_id)
    if not data:
        try:
            bot.answer_callback_query(call.id, "Data expired. Resend file.", show_alert=True)
        except Exception:
            pass
        return
    user_key = get_user_key_db(call.from_user.id)
    if not user_key:
        return
    try:
        bot.answer_callback_query(call.id, "Processing...")
    except Exception:
        pass
    try:
        stop_event = threading.Event()
        progress_thread = threading.Thread(target=progress_thread_fn, args=(chat_id, data.get("origin", call.message.message_id), stop_event, log_action), daemon=True)
        progress_thread.start()
        future = EXECUTOR.submit(ask_gemini, data["text"], prompt_instr, user_key, call.from_user.id)
        try:
            res = future.result(timeout=REQUEST_TIMEOUT_GEMINI)
        except Exception as e:
            res = f"❌ Error: {e}"
        stop_event.set()
        send_long_text(chat_id, res, data.get("origin", call.message.message_id), call.from_user.id, log_action)
    except Exception as e:
        try:
            bot.send_message(chat_id, f"❌ Error: {e}")
        except Exception:
            pass

def send_long_text(chat_id, text, reply_id, uid, action="Transcript"):
    mode = user_mode.get(uid, "Split messages")
    if len(text) > MAX_MESSAGE_CHUNK:
        if mode == "Split messages":
            sent = None
            for i in range(0, len(text), MAX_MESSAGE_CHUNK):
                sent = bot.send_message(chat_id, text[i:i+MAX_MESSAGE_CHUNK], reply_to_message_id=reply_id)
            return sent
        else:
            fname = os.path.join(DOWNLOADS_DIR, f"{action}.txt")
            with open(fname, "w", encoding="utf-8") as f:
                f.write(text)
            sent = bot.send_document(chat_id, open(fname, "rb"), caption="Open this file and copy the text inside 👍", reply_to_message_id=reply_id)
            try:
                os.remove(fname)
            except Exception:
                pass
            return sent
    return bot.send_message(chat_id, text, reply_to_message_id=reply_id)

def _start_cleanup_thread():
    def cleaner():
        while True:
            try:
                now = time.time()
                cutoff = now - (15 * 60)
                for chat_id, msgs in list(user_transcriptions.items()):
                    to_del = [mid for mid, meta in list(msgs.items()) if meta.get("ts", 0) < cutoff]
                    for mid in to_del:
                        try:
                            del msgs[mid]
                        except Exception:
                            pass
                    if not msgs:
                        try:
                            del user_transcriptions[chat_id]
                        except Exception:
                            pass
            except Exception:
                pass
            time.sleep(60)
    threading.Thread(target=cleaner, daemon=True).start()

flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def index():
    return "Bot Running", 200

@flask_app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        data = request.get_data().decode("utf-8")
        def _process_webhook_update(data_str):
            try:
                bot.process_new_updates([types.Update.de_json(data_str)])
            except Exception:
                pass
        threading.Thread(target=_process_webhook_update, args=(data,), daemon=True).start()
        return "", 200
    abort(403)

if __name__ == "__main__":
    _start_cleanup_thread()
    if WEBHOOK_URL:
        try:
            bot.remove_webhook()
        except Exception:
            pass
        time.sleep(0.5)
        try:
            bot.set_webhook(url=WEBHOOK_URL)
        except Exception:
            pass
        flask_app.run(host="0.0.0.0", port=PORT)
    else:
        print("Webhook URL not set, exiting.")
