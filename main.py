import telebot
import logging
import os
from datetime import datetime
from flask import Flask, request

BOT_TOKEN = "8391234863:AAHo5_ykvUlnW_iV6vPtd0yUZ5FJaXH8NGI"
WEBHOOK_URL_BASE = "https://media-to-text-bot-81tt.onrender.com"
WEBHOOK_URL_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = WEBHOOK_URL_BASE + WEBHOOK_URL_PATH
PORT = int(os.environ.get("PORT", 8443))

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)
bot_start_time = None

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def set_bot_info_and_startup():
    global bot_start_time
    bot_start_time = datetime.now()
    descriptions = {
        "en": {
            "description": "This bot can Transcribe and Summarize (Voice messages Audio files or Videos) for free\n\n🔥Enjoy unlimited free usage Get start!👌🏻",
            "short": "This bot can Transcribe and Summarize (Voice messages Audio files or Videos) for free"
        },
        "ru": {
            "description": "Этот бот может транскрибировать и резюмировать голосовые сообщения, аудиофайлы и видео бесплатно\n\n🔥Используйте без ограничений!👌🏻",
            "short": "Бот транскрибирует и резюмирует голосовые сообщения, аудио и видео бесплатно"
        },
        "es": {
            "description": "Este bot puede transcribir y resumir mensajes de voz, archivos de audio y videos gratis\n\n🔥¡Disfruta uso ilimitado gratis!👌🏻",
            "short": "Transcribe y resume mensajes de voz, audio y video gratis"
        },
        "pt": {
            "description": "Este bot pode transcrever e resumir mensagens de voz, arquivos de áudio ou vídeos gratuitamente\n\n🔥Aproveite uso ilimitado gratuito!👌🏻",
            "short": "Transcreve e resume voz, áudio e vídeo gratuitamente"
        },
        "tr": {
            "description": "Bu bot sesli mesajları, ses dosyalarını ve videoları ücretsiz olarak yazıya dökebilir ve özetleyebilir\n\n🔥Sınırsız ücretsiz kullanımın tadını çıkarın!👌🏻",
            "short": "Sesli mesajları, ses ve videoları ücretsiz yazıya dökme ve özetleme"
        },
        "id": {
            "description": "Bot ini dapat menyalin (transcribe) dan meringkas pesan suara, file audio, atau video secara gratis\n\n🔥Nikmati penggunaan gratis tanpa batas!👌🏻",
            "short": "Menyalin dan meringkas pesan suara, audio, dan video gratis"
        },
        "fr": {
            "description": "Ce bot peut transcrire et résumer les messages vocaux, fichiers audio ou vidéos gratuitement\n\n🔥Profitez d'une utilisation illimitée et gratuite !👌🏻",
            "short": "Transcrit et résume messages vocaux, audio et vidéo gratuitement"
        },
        "ar": {
            "description": "يمكن لهذا البوت نسخ وتلخيص الرسائل الصوتية وملفات الصوت والفيديو مجانًا\n\n🔥استمتع بالاستخدام المجاني غير المحدود!👌🏻",
            "short": "ينسخ ويلخص الرسائل الصوتية والصوت والفيديو مجانًا"
        },
        "fa": {
            "description": "این ربات می‌تواند پیام‌های صوتی، فایل‌های صوتی و ویدئوها را به‌صورت رایگان رونویسی و خلاصه کند\n\n🔥از استفاده نامحدود رایگان لذت ببرید!👌🏻",
            "short": "رونویسی و خلاصه‌سازی پیام‌های صوتی، صوت و ویدئو به‌صورت رایگان"
        },
        "hi": {
            "description": "यह बॉट वॉइस संदेशों, ऑडियो फाइलों और वीडियो का ट्रांसक्रिप्शन और सारांश मुफ्त में कर सकता है\n\n🔥असीमित मुफ्त उपयोग का आनंद लें!👌🏻",
            "short": "वॉइस, ऑडियो और वीडियो का ट्रांसक्राइब और सारांश मुफ्त में"
        }
    }
    try:
        default = descriptions.get("en")
        if default:
            bot.set_my_description(default["description"])
            bot.set_my_short_description(default["short"])
        for code, texts in descriptions.items():
            try:
                bot.set_my_description(texts["description"], language_code=code)
                bot.set_my_short_description(texts["short"], language_code=code)
                logging.info(f"Set descriptions for language {code}")
            except Exception as inner_e:
                logging.error(f"Failed to set descriptions for {code}: {inner_e}")
        bot.delete_my_commands()
        logging.info("Bot info updated for multiple languages.")
    except Exception as e:
        logging.error(f"Failed to set bot info: {e}")

@bot.message_handler(content_types=["text"])
def default_handler(message):
    bot.reply_to(
        message,
        "👋 Send me any text and I will convert it into speech using Microsoft Edge TTS."
    )

@bot.message_handler(content_types=["voice", "audio", "video"])
def media_handler(message):
    bot.reply_to(message, "⏳ Processing your media...")
    text = fake_tts()
    bot.send_message(message.chat.id, text)

def fake_tts():
    return "🔊 (Here is where the generated speech/audio will be returned — add TTS engine later)."

@app.route(WEBHOOK_URL_PATH, methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return "", 200
    else:
        return "Bad Request", 403

if __name__ == "__main__":
    set_bot_info_and_startup()
    try:
        bot.remove_webhook()
        logging.info("Webhook removed successfully.")
    except Exception as e:
        logging.error(f"Failed to remove webhook: {e}")
    try:
        bot.set_webhook(url=WEBHOOK_URL)
        logging.info(f"Webhook set successfully to URL: {WEBHOOK_URL}")
    except Exception as e:
        logging.error(f"Failed to set webhook: {e}")
    app.run(host="0.0.0.0", port=PORT)
