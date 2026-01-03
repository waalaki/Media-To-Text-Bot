import os
import threading
import time
import logging
import requests
import base64
import mimetypes
import pymongo
from flask import Flask, request, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Update

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL_BASE = os.environ.get("WEBHOOK_URL_BASE", "")
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook/")
WEBHOOK_URL = WEBHOOK_URL_BASE.rstrip('/') + WEBHOOK_PATH if WEBHOOK_URL_BASE else ""
REQUEST_TIMEOUT_GEMINI = int(os.environ.get("REQUEST_TIMEOUT_GEMINI", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "80"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHUNK = 4095
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
PRIMARY_MODEL = os.environ.get("PRIMARY_MODEL", GEMINI_MODEL)
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL", os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite"))
MAX_USAGE_COUNT = int(os.environ.get("MAX_USAGE_COUNT", "18"))
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")
DB_USER = os.environ.get("DB_USER", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_APPNAME = os.environ.get("DB_APPNAME", "SpeechBot")
MONGO_URI = os.environ.get("MONGO_URI") or (f"mongodb+srv://{DB_USER}:{DB_PASSWORD}@cluster0.n4hdlxk.mongodb.net/{DB_APPNAME}?retryWrites=true&w=majority&appName={DB_APPNAME}" if DB_USER and DB_PASSWORD else "")

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
flask_app = Flask(__name__)

user_mode = {}
user_transcriptions = {}
user_selected_lang = {}
pending_files = {}
user_keys = {}
user_model_usage = {}

client_mongo = None
db = None
users_col = None

if MONGO_URI:
    try:
        client_mongo = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client_mongo.admin.command("ping")
        db = client_mongo[DB_APPNAME] if DB_APPNAME else client_mongo.get_default_database()
        users_col = db.get_collection("users")
        users_col.create_index("user_id", unique=True)
        cursor = users_col.find({}, {"user_id": 1, "gemini_key": 1})
        for doc in cursor:
            user_keys[int(doc["user_id"])] = doc.get("gemini_key")
    except Exception as e:
        logging.warning("mongo connection failed %s", e)

LANGS = [
("🇬🇧 English","en"), ("🇸🇦 العربية","ar"), ("🇪🇸 Español","es"), ("🇫🇷 Français","fr"),
("🇷🇺 Русский","ru"), ("🇩🇪 Deutsch","de"), ("🇮🇳 हिन्दी","hi"), ("🇮🇷 فارسی","fa"),
("🇮🇩 Indonesia","id"), ("🇺🇦 Українська","uk"), ("🇦🇿 Azərbaycan","az"), ("🇮🇹 Italiano","it"),
("🇹🇷 Türkçe","tr"), ("🇧🇬 Български","bg"), ("🇷🇸 Srpski","sr"), ("🇵🇰 اردو","ur"),
("🇹🇭 ไทย","th"), ("🇻🇳 Tiếng Việt","vi"), ("🇯🇵 日本語","ja"), ("🇰🇷 한국어","ko"),
("🇨🇳 中文","zh"), ("🇳🇱 Nederlands","nl"), ("🇸🇪 Svenska","sv"), ("🇳🇴 Norsk","no"),
("🇮🇱 עברית","he"), ("🇩🇰 Dansk","da"), ("🇪🇹 አማርኛ","am"), ("🇫🇮 Suomi","fi"),
("🇧🇩 বাংলা","bn"), ("🇰🇪 Kiswahili","sw"), ("🇪🇹 Oromo","om"), ("🇳🇵 नेपाली","ne"),
("🇵🇱 Polski","pl"), ("🇬🇷 Ελληνικά","el"), ("🇨🇿 Čeština","cs"), ("🇮🇸 Íslenska","is"),
("🇱🇹 Lietuvių","lt"), ("🇱🇻 Latviešu","lv"), ("🇭🇷 Hrvatski","hr"), ("🇷🇸 Bosanski","bs"),
("🇭🇺 Magyar","hu"), ("🇷🇴 Română","ro"), ("🇸🇴 Somali","so"), ("🇲🇾 Melayu","ms"),
("🇺🇿 O'zbekcha","uz"), ("🇵🇭 Tagalog","tl"), ("🇵🇹 Português","pt")
]

def set_user_key_db(uid, key):
    user_keys[uid] = key
    try:
        if users_col is not None:
            users_col.update_one({"user_id": uid}, {"$set": {"gemini_key": key, "updated_at": time.time()}}, upsert=True)
    except Exception as e:
        logging.warning("failed to save key to db %s", e)

def get_user_key_db(uid):
    if uid in user_keys:
        return user_keys[uid]
    try:
        if users_col is not None:
            doc = users_col.find_one({"user_id": uid})
            if doc:
                k = doc.get("gemini_key")
                user_keys[uid] = k
                return k
    except Exception as e:
        logging.warning("failed to read key from db %s", e)
    return None

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
    user_model_usage[uid] = current_usage
    return PRIMARY_MODEL

def build_lang_keyboard(origin):
    btns = []
    row = []
    for i, (lbl, code) in enumerate(LANGS, 1):
        row.append(InlineKeyboardButton(lbl, callback_data=f"lang|{code}|{lbl}|{origin}"))
        if i % 3 == 0:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    return InlineKeyboardMarkup(btns)

def build_action_keyboard(text_len):
    btns = []
    if text_len > 1000:
        btns.append([InlineKeyboardButton("Get Summarize", callback_data="summarize_menu|")])
    return InlineKeyboardMarkup(btns)

def build_summarize_keyboard(origin):
    btns = [
        [InlineKeyboardButton("Short", callback_data=f"summopt|Short|{origin}")],
        [InlineKeyboardButton("Detailed", callback_data=f"summopt|Detailed|{origin}")],
        [InlineKeyboardButton("Bulleted", callback_data=f"summopt|Bulleted|{origin}")]
    ]
    return InlineKeyboardMarkup(btns)

def ensure_joined(message):
    if not REQUIRED_CHANNEL:
        return True
    try:
        if bot.get_chat_member(REQUIRED_CHANNEL, message.from_user.id).status in ['member', 'administrator', 'creator']:
            return True
    except Exception:
        pass
    clean = REQUIRED_CHANNEL.replace("@", "")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Join", url=f"https://t.me/{clean}")]])
    bot.reply_to(message, "First, join my channel and come back 👍", reply_markup=kb)
    return False

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if ensure_joined(message):
        welcome_text = "👋 Salaam!\n• Send me\n• voice message\n• audio file\n• video\n• to transcribe for free for any problem report https://t.me/osiy3"
        kb = build_lang_keyboard("file")
        bot.reply_to(message, welcome_text, reply_markup=kb)

@bot.message_handler(func=lambda m: isinstance(m.text, str) and m.text.strip().startswith("AIz"))
def set_key_plain(message):
    token = message.text.strip().split()[0]
    if not token:
        return
    prev = get_user_key_db(message.from_user.id)
    set_user_key_db(message.from_user.id, token)
    msg = "API key updated." if prev else "Key saved send audio or video"
    bot.reply_to(message, msg)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith('lang|'))
def lang_cb(call):
    _, code, lbl, origin = call.data.split("|")
    if origin != "file":
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except:
            pass
        process_text_action(call, origin, f"Translate to {lbl}", f"Translate this text into language {lbl}. No extra text ONLY return the translated text.")
        return
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except:
            pass
    chat_id = call.message.chat.id
    user_selected_lang[chat_id] = code
    bot.answer_callback_query(call.id, f"Language set: {lbl} ☑️")
    pending = pending_files.pop(chat_id, None)
    if not pending:
        return
    file_url = pending.get("url")
    mime_type = pending.get("mime")
    orig_msg = pending.get("message")
    bot.send_chat_action(chat_id, 'typing')
    try:
        text = transcribe_media_gemini(file_url, mime_type, code, orig_msg.from_user.id)
        if not text:
            raise ValueError("Empty transcription")
        sent = send_long_text(chat_id, text, orig_msg.id, orig_msg.from_user.id)
        if sent:
            user_transcriptions.setdefault(chat_id, {})[sent.message_id] = {"text": text, "origin": orig_msg.id}
            if len(text) > 0:
                try:
                    bot.edit_message_reply_markup(chat_id, sent.message_id, reply_markup=build_action_keyboard(len(text)))
                except:
                    pass
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {e}")

def gemini_api_call(endpoint, payload, key):
    url = f"https://generativelanguage.googleapis.com/v1beta/{endpoint}?key={key}"
    headers = {"Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_GEMINI)
    resp.raise_for_status()
    return resp.json()

def transcribe_media_gemini(file_url, mime_type, language_code, uid):
    key = get_user_key_db(uid)
    if not key:
        raise RuntimeError("first send me Gemini key 🤓")
    file_content = requests.get(file_url, timeout=REQUEST_TIMEOUT_GEMINI).content
    b64_data = base64.b64encode(file_content).decode('utf-8')
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
    model_to_use = get_current_model(uid)
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": b64_data
                    }
                }
            ]
        }],
        "modality": "text"
    }
    data = gemini_api_call(f"models/{model_to_use}:generateContent", payload, key)
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        raise RuntimeError(f"Unexpected Gemini response {e}")

def ask_gemini(text, instruction, uid):
    key = get_user_key_db(uid)
    if not key:
        raise RuntimeError("No Gemini key configured. Send your key starting with AIz")
    model_to_use = get_current_model(uid)
    payload = {"contents": [{"parts": [{"text": f"{instruction}\n\n{text}"}]}]}
    data = gemini_api_call(f"models/{model_to_use}:generateContent", payload, key)
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        raise RuntimeError("Unexpected Gemini response")

def process_text_action(call, origin_msg_id, log_action, prompt_instr):
    chat_id = call.message.chat.id
    try:
        origin_id = int(origin_msg_id)
    except:
        origin_id = call.message.message_id
    data = user_transcriptions.get(chat_id, {}).get(origin_id)
    if not data:
        if call.message.reply_to_message:
            data = user_transcriptions.get(chat_id, {}).get(call.message.reply_to_message.message_id)
    if not data:
        try:
            bot.answer_callback_query(call.id, "Data not found (expired). Resend file.", show_alert=True)
        except:
            pass
        return
    text = data["text"]
    try:
        bot.answer_callback_query(call.id, "Processing...")
    except:
        pass
    bot.send_chat_action(chat_id, 'typing')
    try:
        res = ask_gemini(text, prompt_instr, call.from_user.id)
        send_long_text(chat_id, res, data["origin"], call.from_user.id, log_action)
    except Exception as e:
        bot.send_message(chat_id, f"Error: {e}")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith('summarize_menu|'))
def action_cb(call):
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=build_summarize_keyboard(call.message.message_id))
    except:
        try:
            bot.answer_callback_query(call.id, "Opening summarize options...")
        except:
            pass

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith('summopt|'))
def summopt_cb(call):
    try:
        _, style, origin = call.data.split("|")
    except:
        try:
            bot.answer_callback_query(call.id, "Invalid option", show_alert=True)
        except:
            pass
        return
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass
    prompt = ""
    if style == "Short":
        prompt = "Summarize this text in 1-2 concise sentences. No extra text — return only the summary."
    elif style == "Detailed":
        prompt = "Summarize this text in a detailed paragraph preserving key points. No extra text — return only the summary."
    else:
        prompt = "Summarize this text as a bulleted list of main points. No extra text — return only the summary."
    process_text_action(call, origin, f"Summarize ({style})", prompt)

@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
def handle_media(message):
    if not ensure_joined(message):
        return
    media = message.voice or message.audio or message.video or message.document
    if not media:
        return
    if getattr(media, 'file_size', 0) > MAX_UPLOAD_SIZE:
        bot.reply_to(message, f"File too large. Limit is {MAX_UPLOAD_MB}MB.")
        return
    mime_type = "audio/mp3"
    if message.voice:
        mime_type = "audio/ogg"
    elif message.audio:
        mime_type = message.audio.mime_type or "audio/mp3"
    elif message.video:
        mime_type = message.video.mime_type or "video/mp4"
    elif message.document:
        mime_type = message.document.mime_type or mimetypes.guess_type(message.document.file_name)[0] or "application/octet-stream"
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        file_info = bot.get_file(media.file_id)
        telegram_file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        lang = user_selected_lang.get(message.chat.id)
        if not lang:
            pending_files[message.chat.id] = {"url": telegram_file_url, "mime": mime_type, "message": message}
            kb = build_lang_keyboard("file")
            bot.reply_to(message, "Select the language spoken in your audio or video:", reply_markup=kb)
            return
        text = transcribe_media_gemini(telegram_file_url, mime_type, lang, message.from_user.id)
        if not text:
            raise ValueError("Empty response")
        sent = send_long_text(message.chat.id, text, message.id, message.from_user.id)
        if sent:
            user_transcriptions.setdefault(message.chat.id, {})[sent.message_id] = {"text": text, "origin": message.id}
            if len(text) > 0:
                try:
                    bot.edit_message_reply_markup(message.chat.id, sent.message_id, reply_markup=build_action_keyboard(len(text)))
                except:
                    pass
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

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
            sent = bot.send_document(chat_id, open(fname, 'rb'), caption="Open this file and copy the text inside 👍", reply_to_message_id=reply_id)
            try:
                os.remove(fname)
            except:
                pass
            return sent
    return bot.send_message(chat_id, text, reply_to_message_id=reply_id)

def _process_webhook_update(raw):
    try:
        upd = Update.de_json(raw.decode('utf-8'))
        bot.process_new_updates([upd])
    except Exception as e:
        logging.exception("error processing update %s", e)

@flask_app.route("/", methods=["GET"])
def index():
    return "Bot Running", 200

@flask_app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        data = request.get_data()
        threading.Thread(target=_process_webhook_update, args=(data,), daemon=True).start()
        return '', 200
    abort(403)

if __name__ == "__main__":
    if WEBHOOK_URL:
        bot.remove_webhook()
        time.sleep(0.5)
        bot.set_webhook(url=WEBHOOK_URL)
        flask_app.run(host="0.0.0.0", port=PORT)
    else:
        print("Webhook URL not set, exiting.")
