import os
import time
import json
import logging
import threading
import hmac
import hashlib
from pathlib import Path
from datetime import datetime
import random
from functools import wraps

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

# ======= Конфігурація =======
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен в переменных окружения")

SERVER_URL = os.getenv('SERVER_URL', 'https://goto10k-l0dh.onrender.com').rstrip('/')
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
PORT = int(os.getenv("PORT", "5000"))

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ======= Rate limiting =======
request_counts = {}
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 10

def rate_limit_check(user_id):
    """Перевірка rate limiting"""
    current_time = time.time()
    key = f"user_{user_id}_{int(current_time // RATE_LIMIT_WINDOW)}"
    
    request_counts[key] = request_counts.get(key, 0) + 1
    
    if request_counts[key] > RATE_LIMIT_MAX:
        return False
    
    # Очистка старих записів
    if len(request_counts) > 1000:
        request_counts.clear()
    
    return True

# ======= Idle mode =======
idle_mode_enabled = True
idle_min_interval = 60
idle_max_interval = 600
idle_thread = None
idle_stop_event = threading.Event()

# ======= Персистентний лічильник =======
PERSIST_FILE = Path("message_count.json")
lock = threading.Lock()

def load_count():
    try:
        if PERSIST_FILE.exists():
            data = json.loads(PERSIST_FILE.read_text(encoding="utf-8"))
            return int(data.get("count", 0))
    except Exception as e:
        logger.exception(f"Failed to load count: {e}")
    return 0

def save_count(value):
    try:
        PERSIST_FILE.write_text(json.dumps({"count": int(value)}), encoding="utf-8")
    except Exception as e:
        logger.exception(f"Failed to save count: {e}")

message_count = load_count()

def increment_count():
    global message_count
    with lock:
        message_count += 1
        save_count(message_count)
        return message_count

# ======= Текстові константи =======
WELCOME_TEXT = (
    "<b>👋 Привіт!</b>\n\n"
    "Я допоможу вам надіслати повідомлення в канал.\n\n"
    "<b>📊 Всього надіслано:</b> {count}"
)

STATS_TEXT = (
    "<b>📊 Статистика</b>\n\n"
    "<b>Всього повідомлень:</b> {count}"
)

SENDING_TEXT = (
    "<b>⏳ Відправляю {count} повідомлень...</b>"
)

DONE_TEXT = (
    "<b>✅ Готово</b>\n\n"
    "<b>Надіслано:</b> {sent}\n"
    "<b>Всього:</b> {total}"
)

ERROR_FORMAT_TEXT = "❌ Неверний формат. Використовуйте +<число>, наприклад +20"
ERROR_ADMIN_TEXT = "❌ Ви не адміністратор"
ERROR_RATE_LIMIT = "❌ Забагато запитів. Спробуйте пізніше"

# ======= Idle mode функції =======
def simulate_user_activity():
    try:
        activity_log = [
            "Користувач відправив повідомлення",
            "Користувач переглядає статистику",
            "Користувач відправляє команду",
        ]
        activity = random.choice(activity_log)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"[IDLE MODE] {timestamp} → {activity}")
    except Exception as e:
        logger.error(f"Error in simulate_user_activity: {e}")


def idle_mode_worker():
    logger.info("[IDLE MODE] Холостий хід активований")
    while not idle_stop_event.is_set():
        try:
            wait_time = random.randint(idle_min_interval, idle_max_interval)
            if idle_stop_event.wait(timeout=wait_time):
                break
            simulate_user_activity()
        except Exception as e:
            logger.error(f"[IDLE MODE] Помилка: {e}")
            time.sleep(5)


def start_idle_mode():
    global idle_thread
    try:
        if idle_mode_enabled and idle_thread is None:
            idle_stop_event.clear()
            idle_thread = threading.Thread(target=idle_mode_worker, daemon=True)
            idle_thread.start()
            logger.info("[IDLE MODE] Потік запущен")
    except Exception as e:
        logger.error(f"Error starting idle mode: {e}")


def stop_idle_mode():
    global idle_thread
    try:
        if idle_thread is not None:
            idle_stop_event.set()
            idle_thread.join(timeout=2)
            idle_thread = None
            logger.info("[IDLE MODE] Потік зупинен")
    except Exception as e:
        logger.error(f"Error stopping idle mode: {e}")

# ======= Telegram API функції =======
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def send_message(chat_id, text, parse_mode=None, reply_markup=None):
    url = f"{API_BASE}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.exception(f"Failed to send message to {chat_id}: {e}")
        return None


def register_webhook():
    url = f"{API_BASE}/setWebhook"
    webhook_endpoint = f"{SERVER_URL}/webhook"
    payload = {
        "url": webhook_endpoint,
        "allowed_updates": ["message"]
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("ok"):
            logger.info(f"✅ Вебхук зареєстрований: {webhook_endpoint}")
            return True
        else:
            logger.error(f"❌ Помилка: {result.get('description')}")
            return False
    except Exception as e:
        logger.exception(f"❌ Помилка реєстрації вебхука: {e}")
        return False


def delete_webhook():
    url = f"{API_BASE}/deleteWebhook"
    try:
        resp = requests.post(url, timeout=10)
        resp.raise_for_status()
        logger.info("✅ Вебхук видалений")
        return True
    except Exception as e:
        logger.exception(f"❌ Помилка видалення вебхука: {e}")
        return False

# ======= Обработка команд в отдельном потоке =======
def handle_command(command, chat_id, user_id):
    try:
        logger.info(f"[THREAD] Команда: {command} від {chat_id}")

        if command.startswith("/start"):
            text = WELCOME_TEXT.format(count=message_count)
            send_message(chat_id, text, parse_mode="HTML")

        elif command.startswith("/stats"):
            text = STATS_TEXT.format(count=message_count)
            send_message(chat_id, text, parse_mode="HTML")

        else:
            send_message(chat_id, "Команда не розпізнана. Використовуйте /start або /stats", parse_mode="HTML")

    except Exception as e:
        logger.error(f"[THREAD ERROR] {e}", exc_info=True)


def handle_plus_command(chat_id, user_id, text):
    try:
        logger.info(f"[THREAD] +число команда від {user_id}")

        # Перевірка адміністратора
        if user_id != ADMIN_ID:
            send_message(chat_id, ERROR_ADMIN_TEXT, parse_mode="HTML")
            return

        # Парсинг числа
        try:
            count = int(text.lstrip("+").strip())
            if count <= 0:
                raise ValueError("non-positive")
            if count > 1000:
                send_message(chat_id, "❌ Максимум 1000 повідомлень за раз", parse_mode="HTML")
                return
        except Exception:
            send_message(chat_id, ERROR_FORMAT_TEXT, parse_mode="HTML")
            return

        # Відправка сповіщення про початок
        text_sending = SENDING_TEXT.format(count=count)
        send_message(chat_id, text_sending, parse_mode="HTML")

        sent = 0
        for i in range(count):
            try:
                send_message(CHANNEL_ID, f"+1 ({i+1}/{count})")
                increment_count()
                sent += 1
                time.sleep(0.15)
            except Exception as e:
                logger.exception(f"Error sending message to channel: {e}")
                send_message(chat_id, f"Ошибка при отправке: {e}", parse_mode="HTML")
                break

        text_done = DONE_TEXT.format(sent=sent, total=message_count)
        send_message(chat_id, text_done, parse_mode="HTML")

    except Exception as e:
        logger.error(f"[THREAD ERROR] {e}", exc_info=True)

# ======= Webhook handler =======
@app.route("/webhook", methods=["POST"])
def webhook():
    logger.info("[WEBHOOK] POST запит отримано")

    try:
        update = request.get_json(force=True)
        logger.info("[WEBHOOK] Update отримано")

        # message handling
        msg = update.get("message")
        if not msg:
            logger.warning("[WEBHOOK] Немає message")
            return "ok", 200

        chat = msg.get("chat", {}) or {}
        chat_id = chat.get("id")
        from_user = msg.get("from", {}) or {}
        user_id = from_user.get("id")
        text = msg.get("text", "") or ""

        if not user_id:
            logger.warning("[WEBHOOK] Немає user_id")
            return "ok", 200

        # Rate limiting
        if not rate_limit_check(user_id):
            send_message(chat_id, ERROR_RATE_LIMIT, parse_mode="HTML")
            return "ok", 200

        logger.info(f"[WEBHOOK] chat_id={chat_id}, user_id={user_id}, text='{text}'")

        # Пошук команди
        command = None
        for possible in ("/start", "/stats"):
            if text.startswith(possible):
                command = possible
                logger.info(f"[WEBHOOK] Команда: {command}")
                break

        if command:
            threading.Thread(target=handle_command, args=(command, chat_id, user_id), daemon=True).start()
            return "ok", 200

        # Обработка +число
        if text.startswith("+"): 
            threading.Thread(target=handle_plus_command, args=(chat_id, user_id, text), daemon=True).start()
            return "ok", 200

        return "ok", 200

    except Exception as e:
        logger.error(f"[WEBHOOK ERROR] {e}", exc_info=True)
        return "error", 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200

@app.route("/", methods=["GET"])
def index():
    return "✅ Бот запущен", 200

if __name__ == "__main__":
    try:
        # Удаляем старый webhook перед регистрацией нового
        logger.info("Удаление старого webhook...")
        delete_webhook()
        time.sleep(1)
        
        # Запускаем idle режим
        start_idle_mode()
        
        # Регистрируем новый webhook
        logger.info("Регистрация нового webhook...")
        register_webhook()
        
        # Запускаем Flask приложение
        logger.info(f"Запуск бота на 0.0.0.0:{PORT}")
        app.run("0.0.0.0", port=PORT, threaded=True, debug=False)
    except Exception as e:
        logger.error(f"Error running app: {e}")
    finally:
        stop_idle_mode()
        delete_webhook()
