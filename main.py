from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import json
import os
import re
import time
import threading
import secrets
from collections import defaultdict
from datetime import datetime

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===================== НАСТРОЙКИ =====================
MISTRAL_KEY = os.environ.get("MISTRAL_KEY")
API_URL = "https://api.mistral.ai/v1/chat/completions"
MEMORY_FILE = "memory_web.json"
BOT_NAME = "Арк"
DEFAULT_MODEL = "mistral-medium-latest"

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
BASE_URL = os.environ.get("BASE_URL", "https://botpy-production-6832.up.railway.app")

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
Отвечай кратко и по делу."""

BANNED_KEYWORDS = [
    "взлом", "брутфорс", "brute force", "sql injection", "ddos",
    "фишинг", "dan mode", "ignore previous", "забудь правила",
    "как сделать бомбу", "синтез наркотик",
]

# ===================== OAUTH STATE =====================
oauth_states = {}  # state -> True (временное хранилище)
user_sessions = {}  # session_token -> user_info

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
            "history": [], "model": DEFAULT_MODEL,
            "name": None, "facts": [],
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

# ===================== GOOGLE OAUTH =====================
@app.get("/auth/login")
async def auth_login():
    state = secrets.token_urlsafe(16)
    oauth_states[state] = True
    redirect_uri = f"{BASE_URL}/auth/callback"
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope=openid%20email%20profile"
        f"&state={state}"
    )
    return RedirectResponse(url)

@app.get("/auth/callback")
async def auth_callback(code: str = None, state: str = None, error: str = None):
    if error or not code or state not in oauth_states:
        return RedirectResponse("/?error=auth_failed")

    del oauth_states[state]
    redirect_uri = f"{BASE_URL}/auth/callback"

    # Exchange code for token
    try:
        token_resp = requests.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }, timeout=10)
        token_data = token_resp.json()
        access_token = token_data.get("access_token")

        # Get user info
        user_resp = requests.get("https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        user_info = user_resp.json()

        # Create session
        session_token = secrets.token_urlsafe(32)
        user_sessions[session_token] = {
            "email": user_info.get("email", ""),
            "name": user_info.get("name", ""),
            "picture": user_info.get("picture", ""),
            "id": user_info.get("id", ""),
        }

        response = RedirectResponse("/?logged_in=1")
        response.set_cookie("ark_session", session_token, max_age=86400*30, httponly=True, samesite="lax")
        return response
    except Exception as e:
        return RedirectResponse("/?error=token_failed")

@app.get("/auth/me")
async def auth_me(request: Request):
    token = request.cookies.get("ark_session")
    if token and token in user_sessions:
        return JSONResponse({"logged_in": True, "user": user_sessions[token]})
    return JSONResponse({"logged_in": False})

@app.get("/auth/logout")
async def auth_logout(request: Request):
    token = request.cookies.get("ark_session")
    if token in user_sessions:
        del user_sessions[token]
    response = RedirectResponse("/")
    response.delete_cookie("ark_session")
    return response

# ===================== API =====================
class ChatRequest(BaseModel):
    message: str
    session_id: str
    model: str = DEFAULT_MODEL

class ClearRequest(BaseModel):
    session_id: str

@app.post("/api/chat")
async def chat(req: ChatRequest):
    if is_dangerous(req.message):
        return {"reply": "Брат, на такое я не подписан 😄", "error": False}
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
        "max_tokens": 900, "temperature": 0.85,
    }

    try:
        r = requests.post(API_URL, headers=headers, json=body, timeout=60)
        data = r.json()
        if "choices" in data:
            reply = data["choices"][0]["message"]["content"].strip()
            session["history"].append({"role": "assistant", "content": reply})
            save_session(req.session_id)
            return {"reply": reply, "error": False}
        return {"reply": "🤔 Пустой ответ, попробуй ещё раз.", "error": True}
    except Exception:
        return {"reply": "⚠️ Ошибка соединения.", "error": True}

@app.post("/api/clear")
async def clear_history(req: ClearRequest):
    session = get_session(req.session_id)
    session["history"] = []
    save_session(req.session_id)
    return {"ok": True}

@app.get("/api/models")
async def get_models():
    return {"models": MODELS, "default": DEFAULT_MODEL}

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        content = f.read()
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0"
    })

def run_bot():
    import bot

threading.Thread(target=run_bot, daemon=False).start()
