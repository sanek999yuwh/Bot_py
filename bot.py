import telebot
import requests
import base64
import time
import threading
import random
import json
import os
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from shared import (
    MISTRAL_KEY, API_URL, DEFAULT_MODEL, MODELS, BOT_NAME, WEB_URL,
    is_dangerous, SAFE_REPLIES, build_prompt, needs_search, search_web,
    THANKS_WORDS, DONATE_REPLY, detect_mood, get_random_joke,
    extract_name, extract_interests
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID       = int(os.environ.get("ADMIN_ID", "0"))
VISION_MODEL   = "pixtral-12b-2409"

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# ===================== БАЗА И ПАМЯТЬ =====================
# (вставь сюда весь твой оригинальный код с db_pool, get_user, save_user, compress_history и т.д.)
# Он полностью рабочий, я его не трогал.

# ===================== КЛАВИАТУРЫ =====================
def main_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("🤖 Сменить модель", callback_data="menu_models"),
           InlineKeyboardButton("🗑️ Очистить историю", callback_data="menu_clear"),
           InlineKeyboardButton("💀 Забыть всё", callback_data="menu_forget"),
           InlineKeyboardButton("🎲 Случайная шутка", callback_data="random_joke"))
    return kb

# ===================== КОМАНДЫ =====================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    uid = message.from_user.id
    name = message.from_user.first_name or "друг"
    bot.send_message(message.chat.id,
        f"Привет, {name}! 👋\n\nЯ — *{BOT_NAME}*. Твой цифровой друг.\n\n"
        "💬 Умные ответы\n🖼️ Анализ картинок\n🎲 Шутки\n\nКнопки внизу 👇",
        parse_mode="Markdown", reply_markup=main_keyboard())

@bot.message_handler(commands=['joke'])
def cmd_joke(message):
    bot.send_message(message.chat.id, f"😂 {get_random_joke()}")

@bot.message_handler(commands=['fact'])
def cmd_fact(message):
    bot.send_message(message.chat.id, "🔬 Арк открыт для тебя каждый день ❤️")

# ===================== CALLBACK =====================
@bot.callback_query_handler(func=lambda c: True)
def handle_callback(call):
    if call.data == "random_joke":
        bot.send_message(call.message.chat.id, f"😂 {get_random_joke()}")
    # ... все остальные callback'и из твоего оригинального bot.py

# ===================== ОСНОВНОЙ ХЭНДЛЕР =====================
@bot.message_handler(func=lambda m: True)
def handle_message(message):
    if not message.text: return
    uid = message.from_user.id
    text = message.text

    if uid in banned_users:  # если у тебя есть banned_users
        bot.send_message(message.chat.id, "⛔ Доступ запрещён.")
        return

    if is_dangerous(text):
        bot.send_message(message.chat.id, random.choice(SAFE_REPLIES))
        return

    if any(w in text.lower() for w in THANKS_WORDS):
        bot.send_message(message.chat.id, DONATE_REPLY)
        return

    # Авто-реакции
    if "❤️" in text or "люблю" in text.lower():
        bot.send_message(message.chat.id, "❤️")

    bot.send_chat_action(message.chat.id, "typing")
    threading.Thread(target=ask_ai, args=(uid, text, message.chat.id, "chat"), daemon=True).start()

print(f"✅ {BOT_NAME} запущен!")
bot.infinity_polling(timeout=30, long_polling_timeout=25)
