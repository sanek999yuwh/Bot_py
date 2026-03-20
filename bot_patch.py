# ============================================================
# ИЗМЕНЕНИЯ В bot.py — добавить кнопку на сайт
# ============================================================
#
# 1. В самом начале файла найди строку:
#    TELEGRAM_TOKEN = "..."
#    И ДОБАВЬ ПОСЛЕ НЕЁ:
#
WEB_URL = "https://botpy-production-6832.up.railway.app/"  # <-- замени на свой URL
#
# 2. Найди функцию cmd_start и замени reply_markup в send_safe на:
#
#    send_safe(message.chat.id,
#        f"Привет, {name}! 👋\n\n"
#        f"Я — *{BOT_NAME}*. Твой цифровой друг.\n\n"
#        "💬 Умные ответы\n🖼️ Анализ картинок\n⏰ Напоминания\n"
#        "📝 Суммаризация текстов\n📊 Генерация таблиц\n🧠 Постоянная память\n\n"
#        "Кнопки внизу — для быстрого доступа 👇",
#        reply_markup=get_reply_kb(uid))
#    bot.send_message(message.chat.id,
#        "🌐 Также доступен веб-интерфейс:",
#        reply_markup=web_keyboard())
#
# 3. Найди функцию reply_keyboard() и ДОБАВЬ РЯДОМ новую функцию:

def web_keyboard():
    """Кнопка со ссылкой на сайт"""
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(
        "🌐 Открыть веб-интерфейс",
        url=WEB_URL
    ))
    return kb

# 4. Добавь команду /web — пользователь пишет /web и получает ссылку:

@bot.message_handler(commands=["web"])
def cmd_web(message):
    send_safe(message.chat.id,
        "🌐 *Веб-интерфейс Арка*\n\n"
        "Общайся со мной прямо в браузере — без Telegram!",
        reply_markup=web_keyboard())

# 5. В handle_message, в блоке if text == "⚙️ Меню": замени на:
#
#    if text == "⚙️ Меню":
#        bot.send_message(message.chat.id, "⚙️ Меню:", reply_markup=main_keyboard())
#        bot.send_message(message.chat.id, "🌐 Веб-версия:", reply_markup=web_keyboard())
#        return
