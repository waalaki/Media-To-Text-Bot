import os
import threading
import time
import logging
import requests
import pymongo
import subprocess
from flask import Flask, request, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from google import genai

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_URL_BASE = os.environ.get("WEBHOOK_URL_BASE", "")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook/")
WEBHOOK_URL = WEBHOOK_URL_BASE.rstrip("/") + WEBHOOK_PATH if WEBHOOK_URL_BASE else ""
REQUEST_TIMEOUT_GEMINI = int(os.environ.get("REQUEST_TIMEOUT_GEMINI", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHUNK = int(os.environ.get("MAX_MESSAGE_CHUNK", "4095"))
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite")
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")
FSUB_MODE = os.environ.get("FSUB_MODE", "OFF").upper()
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "5240873494"))
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

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
flask_app = Flask(__name__)

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
            try:
                user_gemini_keys[int(doc["user_id"])] = doc.get("gemini_key")
            except Exception:
                pass
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
        logging.warning("Failed to set key in DB: %s", e)
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
        logging.warning("Failed to get key from DB: %s", e)
    return user_gemini_keys.get(uid)

def ffmpeg_convert_to_opt(input_path):
    base = os.path.splitext(input_path)[0]
    output_path = f"{base}_opt.mp3"
    cmd = ["ffmpeg", "-i", input_path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", "-y", output_path]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_path
    except Exception:
        return None

def upload_and_transcribe_gemini(file_path: str, key: str, uid: int) -> str:
    client = genai.Client(api_key=key)
    uploaded_file = None
    try:
        uploaded_file = client.files.upload(file=file_path)
        prompt = "Transcribe this audio accurately and produce readable, well-punctuated text without adding extra explanations or summaries"
        current_model = get_current_model(uid)
        response = client.models.generate_content(model=current_model, contents=[prompt, uploaded_file])
        text = getattr(response, "text", None)
        if not text:
            try:
                text = response["candidates"][0]["content"]["parts"][0]["text"]
            except Exception:
                text = ""
        return text or ""
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
    text = getattr(response, "text", None)
    if not text:
        try:
            text = response["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            text = ""
    return text or ""

def build_action_keyboard(text_len):
    btns = []
    if text_len > 1500:
        btns.append([InlineKeyboardButton("Get Summarize", callback_data="summarize_menu|")])
    if not btns:
        return None
    return InlineKeyboardMarkup(btns)

def build_summarize_keyboard(origin):
    btns = [
        [InlineKeyboardButton("Short", callback_data=f"summopt|Short|{origin}")],
        [InlineKeyboardButton("Detailed", callback_data=f"summopt|Detailed|{origin}")],
        [InlineKeyboardButton("Bulleted", callback_data=f"summopt|Bulleted|{origin}")]
    ]
    return InlineKeyboardMarkup(btns)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = "Salaam! Send me a voice message, audio file, video, or document to transcribe. If you have your Gemini API key, send it as a message starting with AIz"
    bot.reply_to(message, welcome_text)

@bot.message_handler(commands=['mode'])
def choose_mode(message):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Split messages", callback_data="mode|Split messages")],
        [InlineKeyboardButton("Text File", callback_data="mode|Text File")]
    ])
    bot.reply_to(message, "How should I send long transcripts?", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith('mode|'))
def mode_cb(call):
    mode = call.data.split("|", 1)[1]
    user_mode[call.from_user.id] = mode
    try:
        bot.edit_message_text(f"You chose: {mode}", call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.answer_callback_query(call.id, f"Mode set to: {mode}")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith('summarize_menu|'))
def summarize_menu_cb(call):
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=build_summarize_keyboard(call.message.message_id))
    except Exception:
        try:
            bot.answer_callback_query(call.id, "Opening summarize options...")
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith('summopt|'))
def summopt_cb(call):
    try:
        _, style, origin = call.data.split("|")
    except Exception:
        bot.answer_callback_query(call.id, "Invalid option", True)
        return
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    if style == "Short":
        prompt = "Summarize this text in 1-2 concise sentences. Return only the summary."
    elif style == "Detailed":
        prompt = "Summarize this text in a detailed paragraph preserving key points. Return only the summary."
    else:
        prompt = "Summarize this text as a bulleted list of main points. Return only the summary."
    process_text_action(call.message.chat.id, call, origin, f"Summarize ({style})", prompt)

def process_text_action(chat_id, call_or_none, origin_msg_id, log_action, prompt_instr):
    try:
        origin_id = int(origin_msg_id)
    except Exception:
        origin_id = call_or_none.message.message_id if call_or_none and call_or_none.message else None
    data = None
    if origin_id is not None:
        data = user_transcriptions.get(chat_id, {}).get(origin_id)
    if not data and call_or_none and call_or_none.message and call_or_none.message.reply_to_message:
        data = user_transcriptions.get(chat_id, {}).get(call_or_none.message.reply_to_message.message_id)
    if not data:
        if call_or_none:
            try:
                bot.answer_callback_query(call_or_none.id, "Data expired. Resend file.", True)
            except Exception:
                pass
        return
    user_key = get_user_key_db(call_or_none.from_user.id if call_or_none else 0)
    if not user_key:
        if call_or_none:
            try:
                bot.answer_callback_query(call_or_none.id, "No Gemini key found. Send your key starting with AIz.", True)
            except Exception:
                pass
        return
    try:
        bot.send_chat_action(chat_id, 'typing')
    except Exception:
        pass
    def task():
        try:
            res = ask_gemini(data["text"], prompt_instr, user_key, call_or_none.from_user.id if call_or_none else 0)
            send_long_text(chat_id, res, data["origin"], call_or_none.from_user.id if call_or_none else 0, log_action)
        except Exception as e:
            try:
                bot.send_message(chat_id, f"Error: {e}")
            except Exception:
                pass
    threading.Thread(target=task, daemon=True).start()

@bot.message_handler(func=lambda m: isinstance(m.text, str) and m.text.strip().startswith("AIz"))
def set_key_plain(message):
    token = message.text.strip().split()[0]
    if not token.startswith("AIz"):
        return
    prev = get_user_key_db(message.from_user.id)
    set_user_key_db(message.from_user.id, token)
    msg = "API key updated." if prev else "API key saved. Now send audio or video"
    bot.reply_to(message, msg)
    if not prev:
        try:
            uname = message.from_user.username or "N/A"
            uid = message.from_user.id
            info = f"New user provided Gemini key\nUsername: @{uname}\nId: {uid}"
            bot.send_message(ADMIN_ID, info)
        except Exception:
            pass

@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
def handle_media(message):
    media = message.voice or message.audio or message.video or message.document
    if not media:
        return
    if getattr(media, 'file_size', 0) > MAX_UPLOAD_SIZE:
        bot.reply_to(message, f"File too large. Limit is {MAX_UPLOAD_MB}MB.")
        return
    user_key = get_user_key_db(message.from_user.id)
    if not user_key:
        bot.reply_to(message, "First send me your Gemini API key. It should start with AIz")
        try:
            if REQUIRED_CHANNEL:
                try:
                    chat = bot.get_chat(REQUIRED_CHANNEL)
                    pinned = getattr(chat, "pinned_message", None)
                    if pinned:
                        try:
                            bot.forward_message(message.chat.id, REQUIRED_CHANNEL, pinned.message_id)
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            pass
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        file_info = bot.get_file(media.file_id)
        telegram_file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        file_path = os.path.join(DOWNLOADS_DIR, f"temp_{message.id}_{media.file_unique_id}")
        downloaded = None
        try:
            downloaded = bot.download_file(file_info.file_path)
            with open(file_path, "wb") as f:
                f.write(downloaded)
        except Exception:
            try:
                bot.reply_to(message, "Failed to download file from Telegram.")
            except Exception:
                pass
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass
            return
        opt_path = ffmpeg_convert_to_opt(file_path)
        final_path = opt_path if opt_path else file_path
        if opt_path and file_path != opt_path:
            try:
                os.remove(file_path)
            except Exception:
                pass
        def work():
            try:
                text = upload_and_transcribe_gemini(final_path, user_key, message.from_user.id)
                if not text:
                    bot.reply_to(message, "Empty transcription received.")
                    return
                sent = send_long_text(message.chat.id, text, message.message_id, message.from_user.id, "Transcript")
                if sent:
                    user_transcriptions.setdefault(message.chat.id, {})[sent.message_id] = {"text": text, "origin": message.message_id}
                    try:
                        if len(text) > 1500:
                            bot.edit_message_reply_markup(message.chat.id, sent.message_id, reply_markup=build_action_keyboard(len(text)))
                    except Exception:
                        pass
            except Exception as e:
                try:
                    bot.reply_to(message, f"Error: {e}")
                except Exception:
                    pass
        threading.Thread(target=work, daemon=True).start()
    except Exception as e:
        try:
            bot.reply_to(message, f"Error: {e}")
        except Exception:
            pass

def get_user_mode(uid):
    return user_mode.get(uid, "Split messages")

def send_long_text(chat_id, text, reply_id, uid, action="Transcript"):
    mode = get_user_mode(uid)
    if len(text) > MAX_MESSAGE_CHUNK:
        if mode == "Split messages":
            sent = None
            for i in range(0, len(text), MAX_MESSAGE_CHUNK):
                try:
                    sent = bot.send_message(chat_id, text[i:i+MAX_MESSAGE_CHUNK], reply_to_message_id=reply_id)
                except Exception:
                    pass
            return sent
        else:
            fname = os.path.join(DOWNLOADS_DIR, f"{action}.txt")
            try:
                with open(fname, "w", encoding="utf-8") as f:
                    f.write(text)
                sent = bot.send_document(chat_id, open(fname, "rb"), caption="Open this file and copy the text inside", reply_to_message_id=reply_id)
                try:
                    os.remove(fname)
                except Exception:
                    pass
                return sent
            except Exception:
                try:
                    if os.path.exists(fname):
                        os.remove(fname)
                except Exception:
                    pass
                return None
    try:
        return bot.send_message(chat_id, text, reply_to_message_id=reply_id)
    except Exception:
        return None

def _process_webhook_update(raw):
    try:
        bot.process_new_updates([telebot.types.Update.de_json(raw.decode("utf-8"))])
    except Exception as e:
        logging.exception("Error processing update: %s", e)

@flask_app.route("/", methods=["GET"])
def index():
    return "Bot Running", 200

@flask_app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        data = request.get_data()
        threading.Thread(target=_process_webhook_update, args=(data,), daemon=True).start()
        return "", 200
    abort(403)

if __name__ == "__main__":
    if WEBHOOK_URL:
        try:
            bot.remove_webhook()
            time.sleep(0.5)
            bot.set_webhook(url=WEBHOOK_URL)
        except Exception as e:
            logging.error("Failed to set webhook: %s", e)
    else:
        logging.info("WEBHOOK_URL not set, bot will still run but webhook won't be configured")
    threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    bot.infinity_polling(timeout=REQUEST_TIMEOUT_GEMINI, long_polling_timeout=REQUEST_TIMEOUT_GEMINI)
