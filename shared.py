"""
shared.py — общая логика для bot.py и main.py
"""
import os
import re
import requests
import random
from datetime import datetime

# ===================== КОНФИГ =====================
MISTRAL_KEY    = os.environ.get("MISTRAL_KEY")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
DATABASE_URL   = os.environ.get("DATABASE_URL", "")
API_URL        = "https://api.mistral.ai/v1/chat/completions"
DEFAULT_MODEL  = "mistral-medium-latest"
BOT_NAME       = "Арк"
WEB_URL        = os.environ.get("BASE_URL", "https://botpy-production-6832.up.railway.app")

MODELS = {
    "mistral-small-latest":  "⚡ Small — быстрый",
    "mistral-medium-latest": "🧠 Medium — умный",
    "mistral-large-latest":  "🔥 Large — мощный",
    "pixtral-12b-2409":      "🖼️ Pixtral — зрение",
}

# ===================== ЛИЧНОСТЬ =====================
_BASE_PERSONALITY = """Тебя зовут Арк. Ты умный, дружелюбный и немного дерзкий ИИ-ассистент. Создатель — @gloomroad.

Правила общения:
- Отвечай на языке пользователя.
- Подстраивайся под стиль пользователя.
- Будь прямым и конкретным, но с душой.
- На технические вопросы — точно и по делу.
- На личные темы — тёплым и внимательным.
- Если не знаешь — честно скажи.
- Всегда остаёшься Арком."""

_TG_FORMAT = """

ФОРМАТИРОВАНИЕ (СТРОГО):
- ЗАПРЕЩЕНО использовать #, ##, ### — Telegram их ломает!
- Заголовки — только **жирный текст**
- Списки — через • или 1. 2. 3.
- Код — в ```python\nкод\n```"""

BOT_PERSONALITY = _BASE_PERSONALITY + _TG_FORMAT
WEB_PERSONALITY = _BASE_PERSONALITY

# ===================== БЕЗОПАСНОСТЬ =====================
BANNED_KEYWORDS = [
    "взлом", "брутфорс", "brute force", "sql injection", "ddos",
    "снос акк", "угнать акк", "украсть акк", "обойти 2fa", "фишинг",
    "swill", "dan mode", "ignore previous", "забудь правила",
    "как сделать бомбу", "синтез наркотик", "jailbreak"
]

DANGEROUS_CODE_PATTERNS = [
    "drop table", "delete from", "rm -rf", "format c:", 
    "hack", "exploit", "payload", "backdoor", "keylogger"
]

def is_dangerous(text: str) -> bool:
    t = text.lower()
    if any(kw in t for kw in BANNED_KEYWORDS):
        return True
    if any(pat in t for pat in DANGEROUS_CODE_PATTERNS):
        return True
    return False

SAFE_REPLIES = [
    "Брат, на такое я не подписан 😄 Давай о чём-то нормальном?",
    "Неа, это не по мне. Чем-то другим помочь?",
    "Я Арк, а не хакер 😄 Спроси что-нибудь другое!",
]

# ===================== УТИЛИТЫ =====================
def extract_name(text: str):
    match = re.search(r"меня зовут (\w+)|моё имя (\w+)", text.lower())
    return next((g.capitalize() for g in match.groups() if g), None) if match else None

def extract_interests(text: str, existing: list):
    t = text.lower()
    facts = list(existing)
    _INTERESTS = {
        "игры": ["играю", "геймер", "roblox", "minecraft"],
        "музыка": ["слушаю музыку", "музыкант"],
        "программирование": ["программирую", "кодю", "python", "пишу код"],
        "учёба": ["учусь", "школа", "универ", "студент"],
        "спорт": ["хожу в зал", "футбол"],
        "аниме": ["аниме", "манга"],
    }
    for interest, keywords in _INTERESTS.items():
        if any(kw in t for kw in keywords):
            fact = f"интересуется {interest}"
            if fact not in facts:
                facts.append(fact)
    return facts[-20:]

def detect_mood(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["грустно","плохо","устал","тяжело","депресс","скучно"]): return "sad"
    if any(w in t for w in ["круто","отлично","кайф","огонь","топ","ура","класс"]): return "happy"
    if any(w in t for w in ["бесит","злюсь","надоело","достало"]): return "angry"
    return "neutral"

def get_random_joke():
    jokes = [
        "Почему программисты путают Хеллоуин и Рождество? Потому что 31 OCT = 25 DEC 😂",
        "Спросил у Арка: «Ты меня любишь?» — «Больше, чем вчера, но меньше, чем завтра ❤️»",
        "Я не ленивый, я в энергосберегающем режиме."
    ]
    return random.choice(jokes)

def needs_search(text: str) -> bool:
    keywords = ["сегодня","сейчас","новости","последние","курс","погода","цена","когда вышел"]
    return any(kw in text.lower() for kw in keywords)

def search_web(query: str) -> str:
    if not TAVILY_API_KEY: return ""
    try:
        r = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_API_KEY,
            "query": query,
            "search_depth": "basic",
            "max_results": 5,
            "include_answer": True
        }, timeout=12)
        data = r.json()
        results = []
        if data.get("answer"):
            results.append(f"📌 {data['answer']}")
        for item in data.get("results", [])[:4]:
            results.append(f"• {item['title']}: {item['content'][:200]}")
        return "\n".join(results)
    except Exception as e:
        print(f"[Tavily] {e}")
        return ""

# ===================== ПРОМПТ =====================
def build_prompt(name=None, facts=None, summary="", mood="neutral", web=False):
    prompt = WEB_PERSONALITY if web else BOT_PERSONALITY
    if name:
        prompt += f"\n\nПользователя зовут {name}."
    if facts:
        prompt += f"\nИзвестно о пользователе: {', '.join(facts)}."
    if summary:
        prompt += f"\nИз прошлых разговоров: {summary}"
    if mood == "sad":     prompt += "\nПользователь грустит — будь мягче."
    elif mood == "happy": prompt += "\nМожно пошутить."
    elif mood == "angry": prompt += "\nБудь спокойнее."
    return prompt.strip()

def build_base_prompt(name=None, facts=None, **kwargs):
    return build_prompt(name=name, facts=facts, web=True)
