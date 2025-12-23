import os
import threading
import time
import asyncio
import logging
import pymongo
import subprocess
import mimetypes
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from google import genai
from flask import Flask, render_template_string

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
PORT = int(os.environ.get("PORT", "8080"))
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

def get_user_mode(uid):
    return user_mode.get(uid, "Split messages")

def ffmpeg_convert_to_opt(input_path):
    base = os.path.splitext(input_path)[0]
    output_path = f"{base}_opt.mp3"
    cmd = ["ffmpeg", "-i", input_path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", "-y", output_path]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_path
    except Exception:
        return None

def wait_until_file_active(client, file_name, timeout=60, poll_interval=2):
    start = time.time()
    while time.time() - start < timeout:
        try:
            f = client.files.get(name=file_name)
            state = None
            if hasattr(f, "state"):
                s = getattr(f, "state")
                if hasattr(s, "name"):
                    state = getattr(s, "name")
                else:
                    state = str(s)
            elif hasattr(f, "status"):
                state = getattr(f, "status")
            else:
                state = None
            if state:
                if str(state).upper() == "ACTIVE":
                    return f
            time.sleep(poll_interval)
        except Exception:
            time.sleep(poll_interval)
    raise RuntimeError("File processing timeout")

def upload_and_transcribe_gemini(file_path: str, key: str, uid: int, mime_type: str = None) -> str:
    client = genai.Client(api_key=key)
    uploaded_file = None
    try:
        if not mime_type:
            mime_type, _ = mimetypes.guess_type(file_path)
        try:
            if mime_type:
                uploaded_file = client.files.upload(file=file_path, config={"mime_type": mime_type})
            else:
                uploaded_file = client.files.upload(file=file_path)
        except Exception as e:
            raise
        try:
            wait_until_file_active(client, uploaded_file.name, timeout=120, poll_interval=2)
        except Exception:
            pass
        if mime_type and mime_type.startswith("image"):
            prompt = "Extract and write ONLY the text visible in this image. If no text exists, write an error message"
        else:
            prompt = "Transcribe this audio Write a text that is accurate and of high quality and does not look like raw ASR text. Do not add intro phrases."
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
    btns = []
    if text_len > 1500:
        btns.append([InlineKeyboardButton("Get Summarize", callback_data="summarize_menu|")])
    return InlineKeyboardMarkup(btns)

def build_summarize_keyboard(origin):
    btns = [
        [InlineKeyboardButton("Short", callback_data=f"summopt|Short|{origin}")],
        [InlineKeyboardButton("Detailed", callback_data=f"summopt|Detailed|{origin}")],
        [InlineKeyboardButton("Bulleted", callback_data=f"summopt|Bulleted|{origin}")]
    ]
    return InlineKeyboardMarkup(btns)

async def ensure_joined(client, message):
    if FSUB_MODE != "ON":
        return True
    if not REQUIRED_CHANNEL:
        return True
    try:
        user_id = message.from_user.id
        member = await client.get_chat_member(REQUIRED_CHANNEL, user_id)
        if member.status in [enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            return True
    except:
        pass
    clean = REQUIRED_CHANNEL.replace("@", "")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Join", url=f"https://t.me/{clean}")]])
    await message.reply_text("First, join my channel and come back 👍", reply_markup=kb, quote=True)
    return False

@app.on_message(filters.command(["start", "help"]))
async def send_welcome(client, message):
    if await ensure_joined(client, message):
        welcome_text = "👋 Salaam!\n• Send me\n• voice message\n• audio file\n• video\n• photo\n• to transcribe for free"
        main_kb = ReplyKeyboardMarkup(
            [
                ["Change Result mode 🐥"]
            ],
            resize_keyboard=True,
            placeholder="Send File 😊"
        )
        await message.reply_text(welcome_text, reply_markup=main_kb, quote=True)

@app.on_message(filters.regex(r"^Change Result mode 🐥$"))
async def choose_mode_btn(client, message):
    if await ensure_joined(client, message):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Split messages", callback_data="mode|Split messages")],
            [InlineKeyboardButton("📄 Text File", callback_data="mode|Text File")]
        ])
        await message.reply_text("How do I send you long transcripts?:", reply_markup=kb, quote=True)

@app.on_message(filters.regex(r"^AIz"))
async def set_key_plain(client, message):
    if not await ensure_joined(client, message): return
    token = message.text.strip().split()[0]
    if not token.startswith("AIz"):
        return
    prev = get_user_key_db(message.from_user.id)
    set_user_key_db(message.from_user.id, token)
    msg = "API key updated." if prev else "Okay send me audio, video or photo 👍"
    await message.reply_text(msg, quote=True)
    if not prev:
        try:
            uname, uid = message.from_user.username or "N/A", message.from_user.id
            info = f"New user provided Gemini key\nUsername: @{uname}\nId: {uid}"
            await client.send_message(ADMIN_ID, info)
        except: pass

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
    try:
        res = await asyncio.get_event_loop().run_in_executor(None, ask_gemini, data["text"], prompt_instr, user_key, call.from_user.id)
        await send_long_text(client, chat_id, res, data["origin"], call.from_user.id, log_action)
    except Exception as e:
        await client.send_message(chat_id, f"❌ Error: {e}")

@app.on_message(filters.voice | filters.audio | filters.video | filters.document | filters.photo)
async def handle_media(client, message):
    if not await ensure_joined(client, message): return
    media = message.voice or message.audio or message.video or message.document or message.photo
    if not media:
        return
    file_size = getattr(media, "file_size", 0)
    if file_size > MAX_UPLOAD_SIZE:
        await message.reply_text(f"Send me file less than {MAX_UPLOAD_MB}MB 😎", quote=True)
        return
    user_key = get_user_key_db(message.from_user.id)
    if not user_key:
        await message.reply_text("first send me Gemini key 🤓 ", quote=True)
        try:
            if REQUIRED_CHANNEL:
                me = await client.get_me()
                try:
                    bot_member = await client.get_chat_member(REQUIRED_CHANNEL, me.id)
                    if bot_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
                        chat_info = await client.get_chat(REQUIRED_CHANNEL)
                        pinned = chat_info.pinned_message
                        if pinned:
                            try: await pinned.forward(message.chat.id)
                            except: pass
                except: pass
        except: pass
        return
    await client.send_chat_action(message.chat.id, enums.ChatAction.TYPING)
    unique_id = getattr(media, "file_unique_id", str(time.time()))
    file_path = os.path.join(DOWNLOADS_DIR, f"temp_{message.id}_{unique_id}")
    mime_type = "application/octet-stream"
    if message.photo:
        mime_type = "image/jpeg"
        file_path += ".jpg"
    elif getattr(media, "mime_type", None):
        mime_type = media.mime_type
    try:
        d_path = await client.download_media(message, file_name=file_path)
        final_path = d_path
        if not message.photo and not message.document:
            opt_path = ffmpeg_convert_to_opt(d_path)
            if opt_path:
                final_path = opt_path
                if d_path != opt_path:
                    try: os.remove(d_path)
                    except: pass
        text = await asyncio.get_event_loop().run_in_executor(None, upload_and_transcribe_gemini, final_path, user_key, message.from_user.id, mime_type)
        if text:
            sent = await send_long_text(client, message.chat.id, text, message.id, message.from_user.id)
            if sent:
                user_transcriptions.setdefault(message.chat.id, {})[sent.id] = {"text": text, "origin": message.id}
                if len(text) > 1500:
                    await client.edit_message_reply_markup(message.chat.id, sent.id, reply_markup=build_action_keyboard(len(text)))
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}", quote=True)

async def send_long_text(client, chat_id, text, reply_id, uid, action="Transcript"):
    mode = get_user_mode(uid)
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

flask_app = Flask(__name__)
start_time = time.time()

@flask_app.route("/")
def index():
    uptime = int(time.time() - start_time)
    return render_template_string("Bot Online - Uptime: {{uptime}}s", uptime=uptime)

if __name__ == "__main__":
    threading.Thread(target=flask_app.run, kwargs={"host":"0.0.0.0","port":PORT}, daemon=True).start()
    app.run()
