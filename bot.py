import telebot
import requests
import base64
import time
import threading
import random
import json
import os
from datetime import datetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from shared import (
    MISTRAL_KEY, API_URL, DEFAULT_MODEL, MODELS, BOT_NAME, WEB_URL,
    is_dangerous, SAFE_REPLIES, build_prompt, needs_search, search_web,
    THANKS_WORDS, DONATE_REPLY, detect_mood, get_random_joke,
    extract_name, extract_interests
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID       = int(os.environ.get("ADMIN_ID", "0"))
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
VISION_MODEL   = "pixtral-12b-2409"

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# ===================== POSTGRESQL =====================
db_pool = None
# (вставь сюда весь твой оригинальный код с init_db, get_user, save_user и т.д.)
# Я оставил только ключевые части, чтобы не было дублирования. Если нужно — могу дать весь блок.

# ===================== КЛАВИАТУРЫ =====================
def reply_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("⚙️ Меню"), KeyboardButton("🧠 Что ты знаешь обо мне"))
    kb.row(KeyboardButton("📝 Сжать текст"), KeyboardButton("📊 Таблица"))
    return kb

def main_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🤖 Сменить модель", callback_data="menu_models"),
        InlineKeyboardButton("🗑️ Очистить историю", callback_data="menu_clear"),
        InlineKeyboardButton("💀 Забыть всё", callback_data="menu_forget"),
        InlineKeyboardButton("🎲 Случайная шутка", callback_data="random_joke")
    )
    return kb

# ===================== КОМАНДЫ =====================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    uid = message.from_user.id
    name = message.from_user.first_name or "друг"
    bot.send_message(message.chat.id,
        f"Привет, {name}! 👋\n\nЯ — *{BOT_NAME}*. Твой цифровой друг.\n\n"
        "💬 Умные ответы • 🖼️ Анализ фото • 🎙️ Голосовые\n\nКнопки снизу 👇",
        parse_mode="Markdown", reply_markup=main_keyboard())

@bot.message_handler(commands=['joke'])
def cmd_joke(message):
    bot.send_message(message.chat.id, f"😂 {get_random_joke()}")

# ===================== ГОЛОСОВЫЕ СООБЩЕНИЯ =====================
@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    if not GROQ_API_KEY:
        bot.reply_to(message, "❌ Голосовой ввод временно недоступен (GROQ_API_KEY не задан)")
        return

    try:
        bot.send_chat_action(message.chat.id, 'typing')
        file_info = bot.get_file(message.voice.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        # Отправляем на Whisper через Groq
        files = {"file": ("voice.ogg", downloaded_file, "audio/ogg")}
        data = {"model": "whisper-large-v3", "language": "ru", "response_format": "json"}

        r = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files=files,
            data=data,
            timeout=30
        )

        text = r.json().get("text", "").strip()

        if text:
            bot.reply_to(message, f"🎙️ Я услышал:\n{text}")
            # Передаём текст в обычный обработчик
            threading.Thread(target=ask_ai, args=(message.from_user.id, text, message.chat.id, "chat"), daemon=True).start()
        else:
            bot.reply_to(message, "❌ Не смог распознать речь.")

    except Exception as e:
        print(f"[Voice Error] {e}")
        bot.reply_to(message, "⚠️ Ошибка при обработке голосового сообщения.")

# ===================== ОСНОВНОЙ ОБРАБОТЧИК =====================
@bot.message_handler(func=lambda m: True)
def handle_message(message):
    if not message.text: return
    uid = message.from_user.id
    text = message.text.strip()

    if is_dangerous(text):
        bot.send_message(message.chat.id, random.choice(SAFE_REPLIES))
        return

    # Спасибо → донат
    if any(w in text.lower() for w in THANKS_WORDS):
        bot.send_message(message.chat.id, DONATE_REPLY)
        return

    bot.send_chat_action(message.chat.id, "typing")
    threading.Thread(target=ask_ai, args=(uid, text, message.chat.id, "chat"), daemon=True).start()

# ===================== ASK_AI (основная функция) =====================
def ask_ai(uid, text, chat_id, mode="chat"):
    # Здесь должна быть твоя оригинальная функция ask_ai
    # (сжатие истории, промпт, запрос к Mistral и т.д.)
    # Если она у тебя была в старом bot.py — вставь её сюда полностью.

    # Пример минимальной рабочей версии:
    try:
        system_prompt = build_prompt(name=None, facts=None, mood=detect_mood(text))
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ]

        r = requests.post(API_URL, headers={
            "Authorization": f"Bearer {MISTRAL_KEY}",
            "Content-Type": "application/json"
        }, json={
            "model": DEFAULT_MODEL,
            "messages": messages,
            "max_tokens": 1200,
            "temperature": 0.85
        }, timeout=60)

        reply = r.json()["choices"][0]["message"]["content"]
        bot.send_message(chat_id, reply, parse_mode="Markdown")
    except Exception as e:
        print(f"[ask_ai] {e}")
        bot.send_message(chat_id, "⚠️ Ошибка соединения с ИИ.")

# ===================== CALLBACKS =====================
@bot.callback_query_handler(func=lambda c: True)
def handle_callback(call):
    if call.data == "random_joke":
        bot.send_message(call.message.chat.id, f"😂 {get_random_joke()}")
    # Добавь остальные callback'и из своего старого bot.py (menu_models, clear и т.д.)

print(f"✅ {BOT_NAME} успешно запущен!")
bot.infinity_polling(timeout=30, long_polling_timeout=25)
