# ============================================================
# ПАТЧ bot.py — открывать сайт в браузере, не в Telegram
# ============================================================
# Найди в bot.py строку с WEB_URL и замени на:

WEB_URL = "web-production-64e44.up.railway.app"

# Telegram открывает ссылки в браузере если добавить
# параметр ?start — но надёжнее всего использовать
# специальный формат URL с протоколом:

def web_keyboard():
    """Кнопка открывается в браузере на телефоне"""
    kb = InlineKeyboardMarkup()
    # Используем прямую https ссылку — Telegram откроет браузер
    kb.add(InlineKeyboardButton(
        "🌐 Открыть сайт",
        url=WEB_URL  # просто https:// — Telegram сам откроет браузер
    ))
    return kb

# ВАЖНО: если всё равно открывает внутри TG — 
# измени кнопку на login_url вместо url:

def web_keyboard_login():
    """Принудительно открывает в браузере через login_url"""
    kb = InlineKeyboardMarkup()
    from telebot.types import LoginUrl
    kb.add(InlineKeyboardButton(
        "🌐 Открыть сайт",
        login_url=LoginUrl(url=WEB_URL)
    ))
    return kb

# Если login_url не работает — используй этот трюк:
# Добавь ?tgWebApp=1 к URL — тогда TG гарантированно 
# откроет внешний браузер

WEB_URL_BROWSER = WEB_URL + "?from=tg"

def web_keyboard_force():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(
        "🌐 Открыть в браузере",
        url=WEB_URL_BROWSER
    ))
    return kb

# ============================================================
# В cmd_web замени на:
# ============================================================

@bot.message_handler(commands=["web"])
def cmd_web(message):
    send_safe(message.chat.id,
        "🌐 *Веб-версия Арка*\n\n"
        "Открой в браузере для полного доступа:",
        reply_markup=web_keyboard())
