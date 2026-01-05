import os
import time
import threading
import logging
import requests
import pymongo
import random
from concurrent.futures import ThreadPoolExecutor
from google import genai
from google.genai.errors import APIError
from flask import Flask, request, abort
from telebot import TeleBot, types

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
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
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", f"/{BOT_TOKEN}")

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

bot = TeleBot(BOT_TOKEN)
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

def ask_gemini(text, instruction, key, uid):
    client = genai.Client(api_key=key)
    prompt = f"{instruction}\n\n{text}"
    current_model = get_current_model(uid)
    response = client.models.generate_content(model=current_model, contents=[prompt])
    return response.text

def build_action_keyboard(text_len):
    markup = types.InlineKeyboardMarkup()
    if text_len > 2000:
        markup.add(types.InlineKeyboardButton("Get Summarize", callback_data="summarize_menu"))
    return markup

def build_summarize_keyboard(origin_chat_id, origin_msg_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Short", callback_data=f"summopt|Short|{origin_chat_id}|{origin_msg_id}"))
    markup.add(types.InlineKeyboardButton("Detailed", callback_data=f"summopt|Detailed|{origin_chat_id}|{origin_msg_id}"))
    markup.add(types.InlineKeyboardButton("Bulleted", callback_data=f"summopt|Bulleted|{origin_chat_id}|{origin_msg_id}"))
    return markup

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

def send_long_text_sync(chat_id, text, reply_to_message_id, uid, action="Transcript"):
    mode = user_mode.get(uid, "Split messages")
    if len(text) > MAX_MESSAGE_CHUNK:
        if mode == "Split messages":
            sent_id = None
            for i in range(0, len(text), MAX_MESSAGE_CHUNK):
                msg = bot.send_message(chat_id, text[i:i+MAX_MESSAGE_CHUNK], reply_to_message_id=reply_to_message_id)
                sent_id = msg.message_id
            return sent_id
        else:
            fname = os.path.join(DOWNLOADS_DIR, f"{action}.txt")
            with open(fname, "w", encoding="utf-8") as f:
                f.write(text)
            sent = bot.send_document(chat_id, open(fname, "rb"), caption="Open this file and copy the text inside 👍", reply_to_message_id=reply_to_message_id)
            try:
                os.remove(fname)
            except Exception:
                pass
            return sent.message_id
    msg = bot.send_message(chat_id, text, reply_to_message_id=reply_to_message_id)
    return msg.message_id

@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    welcome_text = "👋 Salaam!\n• Send me\n• voice message\n• audio file\n• video\n• to transcribe for free for any problem report https://t.me/orlaki"
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    kb.row("Change Result mode 🐥")
    bot.reply_to(message, welcome_text, reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "Change Result mode 🐥")
def choose_mode_btn(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💬 Split messages", callback_data="mode|Split messages"))
    markup.add(types.InlineKeyboardButton("📄 Text File", callback_data="mode|Text File"))
    bot.reply_to(message, "How do I send you long transcripts?:", reply_markup=markup)

@bot.message_handler(func=lambda m: isinstance(m.text, str) and m.text.strip().split()[0].startswith("AIz"))
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

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("mode|"))
def mode_cb(call):
    mode = call.data.split("|", 1)[1]
    user_mode[call.from_user.id] = mode
    try:
        bot.edit_message_text(f"you choosed: {mode}", call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.answer_callback_query(call.id, f"Mode set to: {mode} ☑️")

@bot.callback_query_handler(func=lambda call: call.data == "summarize_menu")
def summarize_menu_cb(call):
    origin_chat_id = call.message.chat.id
    origin_msg_id = call.message.message_id
    try:
        bot.edit_message_reply_markup(origin_chat_id, origin_msg_id, reply_markup=build_summarize_keyboard(origin_chat_id, origin_msg_id))
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("summopt|"))
def summopt_cb(call):
    try:
        _, style, chat_id_str, msg_id_str = call.data.split("|")
        origin_chat_id = int(chat_id_str)
        origin_msg_id = int(msg_id_str)
    except Exception:
        bot.answer_callback_query(call.id, "Invalid data", show_alert=True)
        return
    data = user_transcriptions.get(origin_chat_id, {}).get(origin_msg_id)
    if not data:
        bot.answer_callback_query(call.id, "Data expired. Resend file.", show_alert=True)
        return
    user_key = get_user_key_db(call.from_user.id)
    if not user_key:
        bot.answer_callback_query(call.id, "No API key found", show_alert=True)
        return
    prompts = {
        "Short": "Summarize this text in the original language in which it is written in 1-2 concise sentences.",
        "Detailed": "Summarize this text in the original language in which it is written in a detailed paragraph.",
        "Bulleted": "Summarize this text in the original language in which it is written as a bulleted list."
    }
    prompt_instr = prompts.get(style)
    bot.answer_callback_query(call.id, "Processing...")
    def ask_and_send():
        try:
            res = ask_gemini(data["text"], prompt_instr, user_key, call.from_user.id)
            send_long_text_sync(call.message.chat.id, res, data.get("origin", origin_msg_id), call.from_user.id, f"Summarize ({style})")
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Error: {e}")
    threading.Thread(target=ask_and_send, daemon=True).start()

@bot.message_handler(content_types=['voice', 'audio', 'document', 'video'])
def handle_media(message):
    media = None
    file_size = 0
    file_id = None
    if message.voice:
        media = message.voice
    elif message.audio:
        media = message.audio
    elif message.video:
        media = message.video
    elif message.document:
        media = message.document
    if not media:
        return
    file_size = getattr(media, "file_size", 0) or 0
    if file_size > MAX_UPLOAD_SIZE:
        bot.reply_to(message, f"Send me file less than {MAX_UPLOAD_MB}MB 😎")
        return
    user_key = get_user_key_db(message.from_user.id)
    if not user_key:
        bot.reply_to(message, "first send me Gemini key 🤓")
        if REQUIRED_CHANNEL:
            try:
                bot.forward_message(message.chat.id, REQUIRED_CHANNEL, message.message_id)
            except Exception:
                pass
        return
    bot.send_chat_action(message.chat.id, "typing")
    file_id = media.file_id
    file_info = bot.get_file(file_id)
    file_path_local = os.path.join(DOWNLOADS_DIR, f"temp_{message.message_id}_{file_id.replace('/', '_')}")
    try:
        downloaded = bot.download_file(file_info.file_path)
        with open(file_path_local, "wb") as f:
            f.write(downloaded)
    except Exception as e:
        bot.reply_to(message, f"❌ Error downloading file: {e}")
        try:
            if os.path.exists(file_path_local):
                os.remove(file_path_local)
        except Exception:
            pass
        return
    def process_and_reply():
        try:
            msg = bot.send_message(message.chat.id, "Transcribing...")
            try:
                text = upload_and_transcribe_gemini(file_path_local, user_key, message.from_user.id)
            except APIError as e:
                bot.edit_message_text(f"❌ API Error: {e}", message.chat.id, msg.message_id)
                return
            except Exception as e:
                bot.edit_message_text(f"❌ Error: {e}", message.chat.id, msg.message_id)
                return
            if text:
                sent_id = send_long_text_sync(message.chat.id, text, message.message_id, message.from_user.id)
                if sent_id:
                    user_transcriptions.setdefault(message.chat.id, {})[sent_id] = {"text": text, "origin": message.message_id, "ts": time.time()}
                    if len(text) > 2000:
                        try:
                            bot.edit_message_reply_markup(message.chat.id, sent_id, reply_markup=build_action_keyboard(len(text)))
                        except Exception:
                            pass
            try:
                bot.delete_message(message.chat.id, msg.message_id)
            except Exception:
                pass
        except Exception as e:
            try:
                bot.send_message(message.chat.id, f"❌ Error: {e}")
            except Exception:
                pass
    threading.Thread(target=process_and_reply, daemon=True).start()

flask_app = Flask(__name__)
@flask_app.route("/", methods=["GET"])
def index_route():
    return "Bot Running", 200

@flask_app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        data = request.get_data().decode('utf-8')
        try:
            update = types.Update.de_json(data)
            threading.Thread(target=bot.process_new_updates, args=([update],), daemon=True).start()
            return '', 200
        except Exception:
            return '', 200
    abort(403)

if __name__ == "__main__":
    _start_cleanup_thread()
    if WEBHOOK_URL:
        try:
            bot.remove_webhook()
        except Exception:
            pass
        time.sleep(0.5)
        bot.set_webhook(url=WEBHOOK_URL)
        flask_app.run(host="0.0.0.0", port=PORT)
    else:
        print("Webhook URL not set, exiting.")
