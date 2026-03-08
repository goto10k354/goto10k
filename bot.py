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

# --- Настройка логов ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# --- Конфигурация через окружение ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # например: -100123456789
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # например: https://your-app.onrender.com
PERSIST_FILE = os.getenv("PERSIST_FILE", "message_count.json")
SLEEP_BETWEEN = float(os.getenv("SLEEP_BETWEEN", "0.05"))  # пауза между отправками (сек)
MAX_PER_REQ = int(os.getenv("MAX_PER_REQ", "1000"))  # защитный максимум за один запрос

# --- Валидация обязательных переменных ---
if not BOT_TOKEN:
    log.error("BOT_TOKEN не задан. Установите переменную окружения BOT_TOKEN.")
    raise SystemExit(1)

try:
    ADMIN_ID = int(ADMIN_ID) if ADMIN_ID is not None else None
except Exception:
    log.error("ADMIN_ID должен быть числом (например: 123456789).")
    raise SystemExit(1)

try:
    CHANNEL_ID = int(CHANNEL_ID) if CHANNEL_ID is not None else None
except Exception:
    log.error("CHANNEL_ID должен быть числом (например: -100123456789).")
    raise SystemExit(1)

# --- Телеграм бот и Flask ---
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
app = Flask(__name__)

# --- Счётчик с блокировкой и простым persistence ---
_count_lock = threading.Lock()
_count_path = Path(PERSIST_FILE)


def load_count() -> int:
    if _count_path.exists():
        try:
            data = json.loads(_count_path.read_text(encoding="utf-8"))
            return int(data.get("count", 0))
        except Exception as e:
            log.warning("Не удалось загрузить счётчик из файла: %s", e)
    return 0


def save_count(count: int):
    try:
        tmp = _count_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"count": count}), encoding="utf-8")
        tmp.replace(_count_path)
    except Exception as e:
        log.warning("Не удалось сохранить счётчик в файл: %s", e)


message_count = load_count()
log.info("Текущий saved message_count = %d", message_count)

# --- Помощные функции ---
def increment_count(delta: int = 1):
    global message_count
    with _count_lock:
        message_count += delta
        # сохраняем сразу (можно оптимизировать для реже)
        save_count(message_count)


def is_admin(message) -> bool:
    return (ADMIN_ID is not None) and (message.from_user and message.from_user.id == ADMIN_ID)


# --- Webhook endpoint (Telegram -> our Flask) ---
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        log.exception("Ошибка при обработке webhook: %s", e)
        return "error", 500
    return "ok", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200


# --- Handlers ---
@bot.message_handler(commands=["start"])
def send_welcome(message):
    first_name = getattr(message.from_user, "first_name", "there")
    text = (
        f"👋 Привет, {first_name}!\n\n"
        "Напишите команду в формате: +число\n"
        "Например: +20 (отправит 20 сообщений в канал)\n\n"
        f"📊 Всего сообщений отправлено: {message_count}"
    )
    bot.reply_to(message, text)


@bot.message_handler(commands=["stats"])
def send_stats(message):
    channel_link = (
        f"https://t.me/{str(CHANNEL_ID).replace('-100', '')}"
        if CHANNEL_ID is not None
        else "CHANNEL_ID не настроен"
    )
    bot.reply_to(
        message,
        f"📊 Статистика:\n\nВсего сообщений отправлено: {message_count}\n🔗 Канал: {channel_link}",
    )


@bot.message_handler(func=lambda m: bool(getattr(m, "text", "") and m.text.strip().startswith("+")))
def handle_plus_command(message):
    # безопасность: только админ может запускать
    if not is_admin(message):
        bot.reply_to(
            message,
            f"❌ Вы не админ!\n\n📊 Всего сообщений: {message_count}\n🔗 Ссылка на канал: https://t.me/{str(CHANNEL_ID).replace('-100', '')}",
        )
        return

    txt = message.text.strip()[1:].strip()
    try:
        count = int(txt)
    except Exception:
        bot.reply_to(message, "❌ Неверный формат! Используйте: +число (например: +20)")
        return

    if count <= 0:
        bot.reply_to(message, "⚠️ Число должно быть положительным!")
        return

    if count > MAX_PER_REQ:
        bot.reply_to(message, f"⚠️ Максимум {MAX_PER_REQ} сообщений за раз!")
        return

    if CHANNEL_ID is None:
        bot.reply_to(message, "❌ CHANNEL_ID не настроен на сервере.")
        return

    bot.reply_to(message, f"⏳ Начинаю отправку {count} сообщений в канал...")

    sent = 0
    for i in range(1, count + 1):
        try:
            # сообщение в канал
            bot.send_message(CHANNEL_ID, f"+1 ({i}/{count})")
            increment_count(1)
            sent += 1
            if SLEEP_BETWEEN:
                time.sleep(SLEEP_BETWEEN)
        except telebot.apihelper.ApiException as e:
            log.exception("Telegram API exception при отправке #%d: %s", i, e)
            # уведомляем админа о проблеме и прекращаем цикл
            bot.send_message(message.chat.id, f"❌ Ошибка при отправке сообщения #{i}: {e}")
            break
        except Exception as e:
            log.exception("Неожиданная ошибка при отправке #%d: %s", i, e)
            bot.send_message(message.chat.id, f"❌ Ошибка при отправке сообщения #{i}: {e}")
            break

    bot.send_message(
        message.chat.id,
        f"✅ Завершено. Запрошено: {count}, Успешно отправлено: {sent}\n📊 Всего отправлено: {message_count}",
    )


@bot.message_handler(func=lambda m: True)
def handle_other_messages(message):
    bot.reply_to(message, "Я понимаю только команды вида +число\nНапример: +20")


# --- Webhook setup helper (optional) ---
def setup_webhook():
    if not WEBHOOK_URL:
        log.info("WEBHOOK_URL не задан — бот не настроит вебхук автоматически (режим polling возможен).")
        return

    full_url = WEBHOOK_URL.rstrip("/") + f"/{BOT_TOKEN}"
    try:
        log.info("Устанавливаю webhook: %s", full_url)
        bot.remove_webhook()
        time.sleep(0.5)
        result = bot.set_webhook(url=full_url)
        if result:
            log.info("Webhook успешно установлен.")
        else:
            log.warning("set_webhook вернул False.")
    except Exception as e:
        log.exception("Не удалось установить webhook: %s", e)


# --- При старте приложения (Render/Gunicorn импортирует этот модуль) ---
# Устанавливаем webhook один раз при импорте/старте процесса (будет вызван каждым воркером)
try:
    setup_webhook()
except Exception:
    log.exception("Ошибка при попытке настройки webhook на старте.")

# --- Только для локального запуска (не влияет на Render) ---
if __name__ == "__main__":
    log.info("Запуск локального Flask сервера для бота...")
    # при локальном запуске удобно иметь webhook: укажите WEBHOOK_URL или используйте polling
    if not WEBHOOK_URL:
        log.info("Нет WEBHOOK_URL — запускаем polling (локально).")
        bot.remove_webhook()
        bot.infinity_polling()
    else:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
