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

# ======= Персистентній лічильник та послідовність (МОДЕРНІЗОВАНО) =======
PERSIST_FILE = Path("message_count.json")
SEQUENCE_FILE = Path("message_sequence.json")
HISTORY_FILE = Path("message_history.json")
lock = threading.Lock()

def load_count():
    """Завантаження загального лічильника"""
    try:
        if PERSIST_FILE.exists():
            data = json.loads(PERSIST_FILE.read_text(encoding="utf-8"))
            return int(data.get("count", 0))
    except Exception as e:
        logger.exception(f"Failed to load count: {e}")
    return 0

def save_count(value):
    """Збереження загального лічильника"""
    try:
        PERSIST_FILE.write_text(json.dumps({"count": int(value)}), encoding="utf-8")
    except Exception as e:
        logger.exception(f"Failed to save count: {e}")

def load_sequence():
    """Завантаження глобального лічильника послідовності"""
    try:
        if SEQUENCE_FILE.exists():
            data = json.loads(SEQUENCE_FILE.read_text(encoding="utf-8"))
            return int(data.get("sequence", 0))
    except Exception as e:
        logger.exception(f"Failed to load sequence: {e}")
    return 0

def save_sequence(value):
    """Збереження глобального лічильника послідовності"""
    try:
        SEQUENCE_FILE.write_text(json.dumps({"sequence": int(value)}), encoding="utf-8")
    except Exception as e:
        logger.exception(f"Failed to save sequence: {e}")

def load_history():
    """Завантаження історії повідомлень"""
    try:
        if HISTORY_FILE.exists():
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            return data.get("history", [])
    except Exception as e:
        logger.exception(f"Failed to load history: {e}")
    return []

def save_history(history):
    """Збереження історії повідомлень"""
    try:
        HISTORY_FILE.write_text(json.dumps({"history": history}, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.exception(f"Failed to save history: {e}")

# ======= Ініціалізація глобальних змінних =======
message_count = load_count()
global_sequence = load_sequence()
message_history = load_history()
sending_in_progress = False
sending_lock = threading.Lock()

def increment_count():
    """Збільшення лічильника повідомлень"""
    global message_count
    with lock:
        message_count += 1
        save_count(message_count)
        return message_count


def get_next_sequence():
    """Отримання наступного номеру послідовності (КРИТИЧНО ДЛЯ ФІКСУ)"""
    global global_sequence
    with lock:
        global_sequence += 1
        save_sequence(global_sequence)
        return global_sequence


def add_to_history(batch_id, total_count, sent_count, status, timestamp=None):
    """Додавання запису до історії"""
    global message_history
    if timestamp is None:
        timestamp = datetime.now().isoformat()
    
    record = {
        "batch_id": batch_id,
        "total_requested": total_count,
        "total_sent": sent_count,
        "status": status,
        "timestamp": timestamp
    }
    
    with lock:
        message_history.append(record)
        # Зберігаємо тільки останні 1000 записів
        if len(message_history) > 1000:
            message_history = message_history[-1000:]
        save_history(message_history)
    
    logger.info(f"[HISTORY] Batch {batch_id}: {sent_count}/{total_count} ({status})")
    return record

def get_history_stats():
    """Отримання статистики з історії"""
    with lock:
        successful_batches = len([h for h in message_history if h["status"] == "completed"])
        total_from_history = sum([h["total_sent"] for h in message_history])
        return {
            "successful_batches": successful_batches,
            "total_from_history": total_from_history,
            "total_batches": len(message_history)
        }

# ======= Текстові константи =======
WELCOME_TEXT = (
    "<b>👋 Привіт!</b>\n\n"
    "Я допоможу вам надіслати повідомлення в канал.\n\n"
    "<b>📊 Всього надіслано:</b> {count}\n"
    "<b>🔢 Глобальна послідовність:</b> {sequence}\n"
    "<b>📦 Успішних партій:</b> {batches}"
)

STATS_TEXT = (
    "<b>📊 Статистика</b>\n\n"
    "<b>Всього повідомлень:</b> {count}\n"
    "<b>Глобальна послідовність:</b> {sequence}\n"
    "<b>Успішних партій:</b> {batches}\n"
    "<b>З історії:</b> {history_total}"
)

SENDING_TEXT = (
    "<b>⏳ Відправляю {count} повідомлень...</b>\n"
    "<b>Batch ID:</b> {batch_id}"
)

DONE_TEXT = (
    "<b>✅ Готово</b>\n\n"
    "<b>Batch ID:</b> {batch_id}\n"
    "<b>Запрошено:</b> {requested}\n"
    "<b>Надіслано:</b> {sent}\n"
    "<b>Всього (глобально):</b> {total}\n"
    "<b>Послідовність:</b> {sequence}"
)

SENDING_IN_PROGRESS_TEXT = (
    "⚠️ <b>Відправлення вже в процесі!</b>\n\n"
    "Зачекайте, поки завершиться попереднє відправлення."
)

ERROR_FORMAT_TEXT = "❌ Неверний формат. Використовуйте +<число>, наприклад +20"
ERROR_ADMIN_TEXT = "❌ Ви не адміністратор"
ERROR_RATE_LIMIT = "❌ Забагато запитів. Спробуйте пізніше"
ERROR_SEND_FAILED = "❌ Помилка при відправленні: {error}"

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

def send_message_safe(chat_id, text, parse_mode=None, reply_markup=None, max_retries=3):
    """МОДИФІКОВАНО: Безпечна відправка з повторними спробами"""
    url = f"{API_BASE}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            
            if result.get("ok"):
                logger.info(f"[SEND] Успішно відправлено до {chat_id} (спроба {attempt + 1})")
                return result
            else:
                error_msg = result.get("description", "Unknown error")
                logger.warning(f"[SEND] Telegram API помилка: {error_msg}")
                if attempt < max_retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                continue
        except requests.exceptions.Timeout:
            logger.warning(f"[SEND] Timeout при спробі {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))
            continue
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"[SEND] Помилка з'єднання при спробі {attempt + 1}/{max_retries}: {e}")
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))
            continue
        except Exception as e:
            logger.error(f"[SEND] Непередбачена помилка при спробі {attempt + 1}/{max_retries}: {e}")
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
            continue
    
    logger.error(f"[SEND] Не вдалось відправити повідомлення до {chat_id} після {max_retries} спроб")
    return None


def send_message(chat_id, text, parse_mode=None, reply_markup=None):
    """Обгортка для зворотної сумісності"""
    return send_message_safe(chat_id, text, parse_mode, reply_markup)


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
            hist_stats = get_history_stats()
            text = WELCOME_TEXT.format(
                count=message_count,
                sequence=global_sequence,
                batches=hist_stats["successful_batches"]
            )
            send_message(chat_id, text, parse_mode="HTML")

        elif command.startswith("/stats"):
            hist_stats = get_history_stats()
            text = STATS_TEXT.format(
                count=message_count,
                sequence=global_sequence,
                batches=hist_stats["successful_batches"],
                history_total=hist_stats["total_from_history"]
            )
            send_message(chat_id, text, parse_mode="HTML")

        else:
            send_message(chat_id, "Команда не розпізнана. Використовуйте /start або /stats", parse_mode="HTML")

    except Exception as e:
        logger.error(f"[THREAD ERROR] {e}", exc_info=True)


def handle_plus_command(chat_id, user_id, text):
    global sending_in_progress, message_count, global_sequence    
    try:
        logger.info(f"[THREAD] +число команда від {user_id}")

        # Перевірка адміністратора
        if user_id != ADMIN_ID:
            send_message(chat_id, ERROR_ADMIN_TEXT, parse_mode="HTML")
            return

        # Перевірка, чи вже відправляється (КРИТИЧНО ДЛЯ ФІКСУ)
        with sending_lock:
            if sending_in_progress:
                send_message(chat_id, SENDING_IN_PROGRESS_TEXT, parse_mode="HTML")
                return
            sending_in_progress = True

        # Парсинг числа
        try:
            count = int(text.lstrip("+").strip())
            if count <= 0:
                raise ValueError("non-positive")
            if count > 1000:
                send_message(chat_id, "❌ Максимум 1000 повідомлень за раз", parse_mode="HTML")
                with sending_lock:
                    sending_in_progress = False
                return
        except Exception:
            send_message(chat_id, ERROR_FORMAT_TEXT, parse_mode="HTML")
            with sending_lock:
                sending_in_progress = False
            return

        # Генеруємо унікальний ID для цієї партії
        batch_id = get_next_sequence()
        
        # Відправка сповіщення про початок
        text_sending = SENDING_TEXT.format(count=count, batch_id=batch_id)
        send_message(chat_id, text_sending, parse_mode="HTML")        
        logger.info(f"[BATCH {batch_id}] Початок відправлення {count} повідомлень")

        sent = 0
        failed = 0
        sequence_start = global_sequence
        
        for i in range(count):
            try:
                # МОДИФІКОВАНО: Отримуємо послідовність перед відправкою
                seq_num = get_next_sequence()
                local_num = i + 1
                
                # МОДИФІКОВАНО: Комбінований номер (локальний + глобальний)
                msg_text = f"+{local_num} (Seq: {seq_num})"
                
                # МОДИФІКОВАНО: Відправляємо з перевіркою успішності
                result = send_message_safe(CHANNEL_ID, msg_text, max_retries=3)
                
                if result and result.get("ok"):
                    # МОДИФІКОВАНО: Тільки збільшуємо счетчик при успішній відправці
                    increment_count()
                    sent += 1
                    logger.info(f"[BATCH {batch_id}] Повідомлення {local_num}/{count} успішно (Seq: {seq_num})")
                else:
                    # Якщо не вдалось, пропускаємо і рахуємо як невдачу
                    failed += 1
                    logger.warning(f"[BATCH {batch_id}] Не вдалось відправити повідомлення {local_num} (Seq: {seq_num})")
                
                # МОДИФІКОВАНО: Збільшена затримка з 0.15 до 0.3 секунди
                time.sleep(0.3)
                
            except Exception as e:
                failed += 1
                logger.exception(f"[BATCH {batch_id}] Error sending message {i + 1}: {e}")
                time.sleep(0.3)

        # Додаємо запис до історії
        status = "completed" if sent == count else "partial" if sent > 0 else "failed"
        add_to_history(batch_id, count, sent, status)
        
        text_done = DONE_TEXT.format(
            batch_id=batch_id,
            requested=count,
            sent=sent,
            total=message_count,
            sequence=global_sequence
        )
        
        # Додаємо інформацію про невдачі
        if failed > 0:
            text_done += f"\n<b>⚠️ Помилок:</b> {failed}"
        
        send_message(chat_id, text_done, parse_mode="HTML")        
        logger.info(f"[BATCH {batch_id}] Завершено: {sent}/{count} успішно, {failed} помилок, послідовність: {sequence_start}-{global_sequence}")

    except Exception as e:
        logger.error(f"[THREAD ERROR] {e}", exc_info=True)
    finally:
        with sending_lock:
            sending_in_progress = False

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
    hist_stats = get_history_stats()
    stats = {
        "status": "🟢 Бот запущен",
        "total_messages": message_count,
        "global_sequence": global_sequence,
        "successful_batches": hist_stats["successful_batches"],
        "timestamp": datetime.now().isoformat()
    }
    return jsonify(stats), 200

@app.route("/api/stats", methods=["GET"])
def api_stats():
    """API endpoint для отримання детальної статистики"""
    hist_stats = get_history_stats()
    return jsonify({
        "total_count": message_count,
        "global_sequence": global_sequence,
        "successful_batches": hist_stats["successful_batches"],
        "total_batches": hist_stats["total_batches"],
        "total_from_history": hist_stats["total_from_history"],
        "is_sending": sending_in_progress,
        "timestamp": datetime.now().isoformat()
    }), 200

@app.route("/api/history", methods=["GET"])
def api_history():
    """API endpoint для отримання історії повідомлень"""
    return jsonify({"history": message_history}), 200

if __name__ == "__main__":
    try:
        logger.info("=" * 60)
        logger.info("ЗАПУСК ТЕЛЕГРАМ БОТА З МОДЕРНІЗОВАНОЮ СИСТЕМОЮ")
        logger.info("=" * 60)
        logger.info(f"Загальний лічильник: {message_count}")
        logger.info(f"Глобальна послідовність: {global_sequence}")
        logger.info(f"Записів у історії: {len(message_history)}")
        logger.info("=" * 60)
        
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
