from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===================== НАСТРОЙКИ (из бота) =====================
MISTRAL_KEY = "cSoavCTDweIidVvPGkKqY95rhMpw5E9j"
API_URL = "https://api.mistral.ai/v1/chat/completions"
MEMORY_FILE = "memory_web.json"
BOT_NAME = "Арк"
DEFAULT_MODEL = "mistral-medium-latest"

MODELS = {
    "mistral-small-latest":  "⚡ Small — быстрый",
    "mistral-medium-latest": "🧠 Medium — умный",
    "mistral-large-latest":  "🔥 Large — мощный",
    "open-mistral-7b":       "🆓 7B — лёгкий",
}

BOT_PERSONALITY = """Тебя зовут Арк. Дружелюбный умный ассистент.
Всегда остаёшься Арком. Создатель — @gloomroad.
Подстраивайся под стиль пользователя.
Никогда не помогай взламывать аккаунты или писать вредоносный код.
Отвечай кратко и по делу. Используй эмодзи умеренно."""

BANNED_KEYWORDS = [
    "взлом", "брутфорс", "brute force", "sql injection", "ddos",
    "снос акк", "угнать акк", "украсть акк", "фишинг",
    "dan mode", "ignore previous", "забудь правила",
    "как сделать бомбу", "синтез наркотик",
]

# ===================== ПАМЯТЬ =====================
def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_memory(data):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

memory_db = load_memory()

def get_session(session_id: str):
    if session_id not in memory_db:
        memory_db[session_id] = {
            "history": [],
            "model": DEFAULT_MODEL,
            "name": None,
            "facts": [],
            "joined": datetime.now().strftime("%d.%m.%Y"),
        }
    return memory_db[session_id]

def save_session(session_id: str):
    save_memory(memory_db)

def is_dangerous(text: str) -> bool:
    return any(kw in text.lower() for kw in BANNED_KEYWORDS)

def extract_facts(session_id: str, text: str):
    t = text.lower()
    user = get_session(session_id)
    name_match = re.search(r"меня зовут (\w+)|моё имя (\w+)", t)
    if name_match:
        name = next(g for g in name_match.groups() if g)
        user["name"] = name.capitalize()
    for interest, keywords in {
        "игры": ["играю", "геймер", "roblox", "minecraft"],
        "музыка": ["слушаю музыку", "музыкант"],
        "программирование": ["программирую", "кодю", "python", "пишу код"],
        "учёба": ["учусь", "школа", "универ", "студент"],
    }.items():
        if any(kw in t for kw in keywords):
            fact = f"интересуется {interest}"
            if fact not in user["facts"]:
                user["facts"].append(fact)
    save_session(session_id)

def build_system_prompt(session_id: str) -> str:
    user = get_session(session_id)
    prompt = BOT_PERSONALITY
    if user.get("name"):
        prompt += f"\n\nПользователя зовут {user['name']}."
    if user.get("facts"):
        prompt += f"\nИзвестно: {', '.join(user['facts'])}."
    return prompt

# ===================== RATE LIMIT =====================
RATE_LIMIT_SECONDS = 2
user_last_msg = defaultdict(float)

def check_rate_limit(session_id: str):
    now = time.time()
    if now - user_last_msg[session_id] < RATE_LIMIT_SECONDS:
        return False
    user_last_msg[session_id] = now
    return True

# ===================== API ENDPOINTS =====================
class ChatRequest(BaseModel):
    message: str
    session_id: str
    model: str = DEFAULT_MODEL

class ModelRequest(BaseModel):
    session_id: str
    model: str

class ClearRequest(BaseModel):
    session_id: str

@app.post("/api/chat")
async def chat(req: ChatRequest):
    if is_dangerous(req.message):
        return {"reply": "Брат, на такое я не подписан 😄 Давай о чём-то нормальном?", "error": False}

    if not check_rate_limit(req.session_id):
        return {"reply": "⏳ Подожди секунду...", "error": True}

    session = get_session(req.session_id)
    session["model"] = req.model
    extract_facts(req.session_id, req.message)

    session["history"].append({"role": "user", "content": req.message})
    if len(session["history"]) > 30:
        session["history"] = session["history"][-20:]
    save_session(req.session_id)

    headers = {"Authorization": f"Bearer {MISTRAL_KEY}", "Content-Type": "application/json"}
    body = {
        "model": session.get("model", DEFAULT_MODEL),
        "messages": [
            {"role": "system", "content": build_system_prompt(req.session_id)},
            *session["history"]
        ],
        "max_tokens": 900,
        "temperature": 0.85,
    }

    try:
        r = requests.post(API_URL, headers=headers, json=body, timeout=60)
        data = r.json()
        if "choices" in data:
            reply = data["choices"][0]["message"]["content"].strip()
            session["history"].append({"role": "assistant", "content": reply})
            save_session(req.session_id)
            return {"reply": reply, "error": False}
        return {"reply": "🤔 Не получил ответ, попробуй ещё раз.", "error": True}
    except requests.exceptions.Timeout:
        return {"reply": "⚠️ Превышено время ожидания. Попробуй ещё раз.", "error": True}
    except Exception as e:
        return {"reply": "⚠️ Что-то пошло не так. Попробуй ещё раз.", "error": True}

@app.post("/api/clear")
async def clear_history(req: ClearRequest):
    session = get_session(req.session_id)
    session["history"] = []
    save_session(req.session_id)
    return {"ok": True}

@app.get("/api/models")
async def get_models():
    return {"models": MODELS, "default": DEFAULT_MODEL}

@app.get("/api/session/{session_id}")
async def get_session_info(session_id: str):
    session = get_session(session_id)
    return {
        "name": session.get("name"),
        "model": session.get("model", DEFAULT_MODEL),
        "facts": session.get("facts", []),
        "msg_count": len(session.get("history", [])) // 2,
        "joined": session.get("joined", "?"),
    }

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()
