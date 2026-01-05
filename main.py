import os
import threading
import time
import logging
import subprocess
import asyncio
import pymongo
from flask import Flask, request, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Update
from google import genai

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL_BASE = os.environ.get("WEBHOOK_URL_BASE", "")
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook/")
WEBHOOK_URL = WEBHOOK_URL_BASE.rstrip('/') + WEBHOOK_PATH if WEBHOOK_URL_BASE else ""
REQUEST_TIMEOUT_GEMINI = int(os.environ.get("REQUEST_TIMEOUT_GEMINI", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHUNK = 4095
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
        prompt = "Transcribe this audio Write a text that is accurate and of high quality and does not look like raw ASR text. Do not add intro phrases."
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
    if text_len > 1500:
        btns.append([InlineKeyboardButton("Get Summarize", callback_data="summarize_menu|")])
    kb = InlineKeyboardMarkup()
    kb.row_width = 1
    if btns:
        for r in btns:
            kb.add(*r)
    return kb

def build_summarize_keyboard(origin):
    kb = InlineKeyboardMarkup()
    kb.row_width = 1
    kb.add(InlineKeyboardButton("Short", callback_data=f"summopt|Short|{origin}"))
    kb.add(InlineKeyboardButton("Detailed", callback_data=f"summopt|Detailed|{origin}"))
    kb.add(InlineKeyboardButton("Bulleted", callback_data=f"summopt|Bulleted|{origin}"))
    return kb

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = "👋 Salaam!\n• Send me\n• voice message\n• audio file\n• video\n• to transcribe for free"
    bot.reply_to(message, welcome_text)

@bot.message_handler(commands=['mode'])
def choose_mode(message):
    kb = InlineKeyboardMarkup()
    kb.row_width = 1
    kb.add(InlineKeyboardButton("💬 Split messages", callback_data="mode|Split messages"))
    kb.add(InlineKeyboardButton("📄 Text File", callback_data="mode|Text File"))
    bot.reply_to(message, "How do I send you long transcripts?:", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith('mode|'))
def mode_cb(call):
    mode = call.data.split("|")[1]
    user_mode[call.from_user.id] = mode
    try:
        bot.edit_message_text(f"you choosed: {mode}", call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass
    bot.answer_callback_query(call.id, f"Mode set to: {mode} ☑️")

@bot.callback_query_handler(func=lambda call: call.data.startswith('summarize_menu|'))
def summarize_menu_cb(call):
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=build_summarize_keyboard(call.message.message_id))
    except:
        try:
            bot.answer_callback_query(call.id, "Opening summarize options...")
        except:
            pass

@bot.callback_query_handler(func=lambda call: call.data.startswith('summopt|'))
def summopt_cb(call):
    try:
        _, style, origin = call.data.split("|")
    except:
        bot.answer_callback_query(call.id, "Invalid option", show_alert=True)
        return
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass
    prompts = {
        "Short": "Summarize this text in the original language in which it is written in 1-2 concise sentences.",
        "Detailed": "Summarize this text in the original language in which it is written in a detailed paragraph.",
        "Bulleted": "Summarize this text in the original language in which it is written as a bulleted list."
    }
    threading.Thread(target=process_text_action, args=(call, origin, f"Summarize ({style})", prompts.get(style)), daemon=True).start()

def process_text_action(call, origin_msg_id, log_action, prompt_instr):
    chat_id = call.message.chat.id
    try:
        origin_id = int(origin_msg_id)
    except:
        origin_id = call.message.message_id
    data = user_transcriptions.get(chat_id, {}).get(origin_id)
    if not data and call.message.reply_to_message:
        data = user_transcriptions.get(chat_id, {}).get(call.message.reply_to_message.message_id)
    if not data:
        try:
            bot.answer_callback_query(call.id, "Data expired. Resend file.", show_alert=True)
        except:
            pass
        return
    user_key = get_user_key_db(call.from_user.id)
    if not user_key:
        try:
            bot.answer_callback_query(call.id, "No Gemini key set. Send a key starting with AIz", show_alert=True)
        except:
            pass
        return
    try:
        bot.answer_callback_query(call.id, "Processing...")
    except:
        pass
    try:
        bot.send_chat_action(chat_id, 'typing')
    except:
        pass
    try:
        res = ask_gemini(data["text"], prompt_instr, user_key, call.from_user.id)
        send_long_text(chat_id, res, data["origin"], call.from_user.id, log_action)
    except Exception as e:
        try:
            bot.send_message(chat_id, f"❌ Error: {e}")
        except:
            pass

@bot.message_handler(func=lambda message: isinstance(message.text, str) and message.text.strip().split()[0].startswith("AIz"))
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
            uname = message.from_user.username or "N/A"
            uid = message.from_user.id
            info = f"New user provided Gemini key\nUsername: @{uname}\nId: {uid}"
            bot.send_message(ADMIN_ID, info)
        except:
            pass

@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
def handle_media(message):
    media = message.voice or message.audio or message.video or message.document
    if not media:
        return
    if getattr(media, "file_size", 0) > MAX_UPLOAD_SIZE:
        try:
            bot.reply_to(message, f"Send me file less than {MAX_UPLOAD_MB}MB 😎")
        except:
            pass
        return
    user_key = get_user_key_db(message.from_user.id)
    if not user_key:
        try:
            bot.reply_to(message, "first send me Gemini key 🤓")
        except:
            pass
        if REQUIRED_CHANNEL:
            try:
                chat_info = bot.get_chat(REQUIRED_CHANNEL)
                pinned = getattr(chat_info, "pinned_message", None)
                if pinned:
                    try:
                        bot.forward_message(message.chat.id, REQUIRED_CHANNEL, pinned.message_id)
                    except:
                        pass
            except:
                pass
        return
    try:
        bot.send_chat_action(message.chat.id, 'typing')
    except:
        pass
    try:
        file_info = bot.get_file(media.file_id)
        file_path = file_info.file_path
        file_bytes = bot.download_file(file_path)
        local_path = os.path.join(DOWNLOADS_DIR, f"temp_{message.message_id}_{media.file_unique_id}")
        with open(local_path, "wb") as f:
            f.write(file_bytes)
        opt_path = ffmpeg_convert_to_opt(local_path)
        final_path = opt_path if opt_path else local_path
        if opt_path and local_path != opt_path:
            try:
                os.remove(local_path)
            except:
                pass
        def transcribe_and_send():
            try:
                text = upload_and_transcribe_gemini(final_path, user_key, message.from_user.id)
                if text:
                    sent = send_long_text(message.chat.id, text, message.message_id, message.from_user.id)
                    if sent:
                        mid = sent.message_id if hasattr(sent, "message_id") else sent.id
                        user_transcriptions.setdefault(message.chat.id, {})[mid] = {"text": text, "origin": message.message_id}
                        if len(text) > 500:
                            try:
                                bot.edit_message_reply_markup(message.chat.id, mid, reply_markup=build_action_keyboard(len(text)))
                            except:
                                pass
            except Exception as e:
                try:
                    bot.reply_to(message, f"❌ Error: {e}")
                except:
                    pass
        threading.Thread(target=transcribe_and_send, daemon=True).start()
    except Exception as e:
        try:
            bot.reply_to(message, f"❌ Error: {e}")
        except:
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
            try:
                sent = bot.send_document(chat_id, open(fname, "rb"), caption="Open this file and copy the text inside 👍", reply_to_message_id=reply_id)
            except:
                sent = None
            try:
                os.remove(fname)
            except:
                pass
            return sent
    return bot.send_message(chat_id, text, reply_to_message_id=reply_id)

@flask_app.route("/", methods=["GET"])
def index():
    return "Bot Running", 200

def _process_webhook_update(raw):
    try:
        upd = Update.de_json(raw.decode("utf-8"))
        bot.process_new_updates([upd])
    except Exception as e:
        logging.exception(f"Error processing update: {e}")

@flask_app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    if request.headers.get("content-type") == "application/json":
        data = request.get_data()
        threading.Thread(target=_process_webhook_update, args=(data,), daemon=True).start()
        return "", 200
    abort(403)

if __name__ == "__main__":
    if WEBHOOK_URL:
        try:
            bot.remove_webhook()
        except:
            pass
        time.sleep(0.5)
        try:
            bot.set_webhook(url=WEBHOOK_URL)
        except Exception as e:
            print("Failed to set webhook:", e)
        flask_app.run(host="0.0.0.0", port=PORT)
    else:
        print("Webhook URL not set, exiting.")
