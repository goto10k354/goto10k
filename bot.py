import os
import time
import json
import logging
import threading
from pathlib import Path
from flask import Flask, request, jsonify
import telebot
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(name)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

PERSIST_FILE = "message_count.json"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(name)

lock = threading.Lock()
path = Path(PERSIST_FILE)


# ---------- счетчик ----------

def load_count():
    if path.exists():
        try:
            return json.loads(path.read_text())["count"]
        except:
            pass
    return 0


def save_count(count):
    path.write_text(json.dumps({"count": count}))


message_count = load_count()


def increment():
    global message_count
    with lock:
        message_count += 1
        save_count(message_count)


# ---------- webhook ----------

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():

    log.info("Webhook received")

    try:
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
    except Exception as e:
        log.exception("Webhook error")
        return "error", 500

    return "ok", 200


@app.route("/health")
def health():
    return jsonify({"ok": True})


# ---------- команды ----------

@bot.message_handler(commands=["start"])
def start(message):

    bot.reply_to(
        message,
        f"""👋 Привет!

Напиши команду:

+число

Например:
+20

📊 Всего отправлено: {message_count}
"""
    )


@bot.message_handler(commands=["stats"])
def stats(message):

    bot.reply_to(
        message,
        f"""📊 Статистика

Всего сообщений: {message_count}
"""
    )


# ---------- +число ----------

@bot.message_handler(func=lambda m: m.text and m.text.startswith("+"))
def plus(message):

    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "❌ Вы не админ")
        return

    try:
        count = int(message.text[1:])
    except:
        bot.reply_to(message, "❌ Неверный формат")
        return

    bot.reply_to(message, f"⏳ Отправляю {count} сообщений")

    sent = 0

    for i in range(count):

        try:
            bot.send_message(CHANNEL_ID, f"+1 ({i+1}/{count})")
            increment()
            sent += 1
            time.sleep(0.05)

        except Exception as e:
            bot.send_message(message.chat.id, f"Ошибка: {e}")
            break

    bot.send_message(
        message.chat.id,
        f"✅ Готово\nОтправлено: {sent}\nВсего: {message_count}"
    )


# ---------- webhook setup ----------

def setup_webhook():

    if not WEBHOOK_URL:
        log.warning("WEBHOOK_URL not set")
        return

    url = WEBHOOK_URL + "/" + BOT_TOKEN

    bot.remove_webhook()
    time.sleep(1)

    bot.set_webhook(url=url)

    log.info(f"Webhook set: {url}")


setup_webhook()


# ---------- запуск ----------

if name == "main":

    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port
    )
