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
import psycopg2
from psycopg2.extras import RealDictCursor
import psycopg2.pool

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===================== НАСТРОЙКИ =====================
MISTRAL_KEY          = os.environ.get("MISTRAL_KEY")
API_URL              = "https://api.mistral.ai/v1/chat/completions"
DEFAULT_MODEL        = "mistral-medium-latest"
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
BASE_URL             = os.environ.get("BASE_URL", "https://botpy-production-6832.up.railway.app")
DATABASE_URL         = os.environ.get("DATABASE_URL", "")
TAVILY_API_KEY       = os.environ.get("TAVILY_API_KEY", "")

MODELS = {
    "mistral-small-latest":  "⚡ Small — быстрый",
    "mistral-medium-latest": "🧠 Medium — умный",
    "mistral-large-latest":  "🔥 Large — мощный",
    "open-mistral-7b":       "🆓 7B — лёгкий",
}

BOT_PERSONALITY = """Ты — Арк, умный и дружелюбный ассистент. Создатель — @gloomroad.

Как отвечать:
- Пиши компактно — никаких огромных отступов между абзацами.
- Один абзац = одна мысль. Без лишних переносов строк.
- Используй маркированный список только когда это реально нужно (3+ пунктов).
- Не начинай каждый ответ с "Конечно!", "Отличный вопрос!", "Разумеется!".
- Подстраивайся под стиль пользователя — если он пишет коротко, отвечай коротко.
- Если вопрос технический — отвечай точно и по делу, без воды.
- Не помогай со взломом, вредоносным кодом и подобным."""

BANNED_KEYWORDS = [
    "взлом", "брутфорс", "brute force", "sql injection", "ddos",
    "фишинг", "dan mode", "ignore previous", "забудь правила",
    "как сделать бомбу", "синтез наркотик",
]

# ===================== POSTGRESQL =====================
db_pool = None

def init_db():
    global db_pool
    if not DATABASE_URL:
        print("⚠️ DATABASE_URL не задан — используем memory storage")
        return False
    try:
        db_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
        conn = db_pool.getconn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    history    JSONB DEFAULT '[]',
                    model      TEXT DEFAULT 'mistral-medium-latest',
                    name       TEXT,
                    facts      JSONB DEFAULT '[]',
                    joined     TEXT,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    google_id  TEXT PRIMARY KEY,
                    email      TEXT,
                    name       TEXT,
                    picture    TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()
        db_pool.putconn(conn)
        print("✅ PostgreSQL подключён")
        return True
    except Exception as e:
        print(f"❌ PostgreSQL ошибка: {e}")
        db_pool = None
        return False

def get_session_db(session_id: str) -> dict:
    if not db_pool:
        return get_session_memory(session_id)
    conn = None
    try:
        conn = db_pool.getconn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
            row = cur.fetchone()
            if row:
                db_pool.putconn(conn)
                return dict(row)
            now = datetime.now().strftime("%d.%m.%Y")
            cur.execute("""
                INSERT INTO sessions (session_id, history, model, name, facts, joined)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
            """, (session_id, json.dumps([]), DEFAULT_MODEL, None, json.dumps([]), now))
            row = cur.fetchone()
            conn.commit()
            db_pool.putconn(conn)
            return dict(row)
    except Exception as e:
        print(f"DB get error: {e}")
        if conn:
            db_pool.putconn(conn)
        return get_session_memory(session_id)

def save_session_db(session_id: str, data: dict):
    if not db_pool:
        return save_session_memory(session_id, data)
    conn = None
    try:
        conn = db_pool.getconn()
        with conn.cursor() as cur:
            history = data.get("history", [])
            if isinstance(history, str):
                history = json.loads(history)
            facts = data.get("facts", [])
            if isinstance(facts, str):
                facts = json.loads(facts)
            cur.execute("""
                UPDATE sessions SET
                    history = %s, model = %s, name = %s,
                    facts = %s, updated_at = NOW()
                WHERE session_id = %s
            """, (
                json.dumps(history),
                data.get("model", DEFAULT_MODEL),
                data.get("name"),
                json.dumps(facts),
                session_id
            ))
            conn.commit()
        db_pool.putconn(conn)
    except Exception as e:
        print(f"DB save error: {e}")
        if conn:
            db_pool.putconn(conn)

# Fallback — память если нет БД
memory_db = {}

def get_session_memory(session_id: str) -> dict:
    if session_id not in memory_db:
        memory_db[session_id] = {
            "session_id": session_id,
            "history": [],
            "model": DEFAULT_MODEL,
            "name": None,
            "facts": [],
            "joined": datetime.now().strftime("%d.%m.%Y"),
        }
    return memory_db[session_id]

def save_session_memory(session_id: str, data: dict):
    memory_db[session_id] = data

def get_session(session_id: str) -> dict:
    return get_session_db(session_id)

def save_session(session_id: str, data: dict):
    save_session_db(session_id, data)

# ===================== OAUTH =====================
oauth_states  = {}
user_sessions = {}

# ===================== TAVILY ПОИСК =====================
def search_web(query: str) -> str:
    if not TAVILY_API_KEY:
        return ""
    try:
        r = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "basic",
                "max_results": 4,
                "include_answer": True,
            },
            timeout=10
        )
        data = r.json()
        results = []
        if data.get("answer"):
            results.append(f"Краткий ответ: {data['answer']}")
        for item in data.get("results", [])[:3]:
            results.append(f"• {item['title']}: {item['content'][:200]}")
        return "\n".join(results) if results else ""
    except Exception as e:
        print(f"Tavily error: {e}")
        return ""

def needs_search(text: str) -> bool:
    keywords = [
        "сегодня", "сейчас", "новости", "последние", "актуальн",
        "2024", "2025", "2026", "курс", "погода", "цена", "стоимость",
        "что происходит", "последние события", "недавно", "вчера",
        "когда вышел", "когда выйдет", "анонс", "релиз"
    ]
    return any(kw in text.lower() for kw in keywords)

# ===================== UTILS =====================
def is_dangerous(text: str) -> bool:
    return any(kw in text.lower() for kw in BANNED_KEYWORDS)

def extract_facts(session_id: str, text: str):
    t = text.lower()
    session = get_session(session_id)
    facts = session.get("facts", [])
    if isinstance(facts, str):
        facts = json.loads(facts)

    name_match = re.search(r"меня зовут (\w+)|моё имя (\w+)", t)
    if name_match:
        name = next(g for g in name_match.groups() if g)
        session["name"] = name.capitalize()

    for interest, keywords in {
        "игры": ["играю", "геймер", "roblox", "minecraft"],
        "программирование": ["программирую", "кодю", "python", "пишу код"],
        "музыка": ["слушаю музыку", "музыкант"],
    }.items():
        if any(kw in t for kw in keywords):
            fact = f"интересуется {interest}"
            if fact not in facts:
                facts.append(fact)

    session["facts"] = facts
    save_session(session_id, session)

def build_system_prompt(session_id: str) -> str:
    session = get_session(session_id)
    prompt = BOT_PERSONALITY
    name = session.get("name")
    if name:
        prompt += f"\n\nПользователя зовут {name}."
    facts = session.get("facts", [])
    if isinstance(facts, str):
        facts = json.loads(facts)
    if facts:
        prompt += f"\nИзвестно: {', '.join(facts)}."
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
        user_resp = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10
        )
        user_info = user_resp.json()
        session_token = secrets.token_urlsafe(32)
        user_sessions[session_token] = {
            "email":   user_info.get("email", ""),
            "name":    user_info.get("name", ""),
            "picture": user_info.get("picture", ""),
            "id":      user_info.get("id", ""),
        }
        # Сохраняем пользователя в БД
        if db_pool:
            conn = None
            try:
                conn = db_pool.getconn()
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO users (google_id, email, name, picture)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (google_id) DO UPDATE SET
                            email = EXCLUDED.email,
                            name = EXCLUDED.name,
                            picture = EXCLUDED.picture
                    """, (
                        user_info.get("id"),
                        user_info.get("email"),
                        user_info.get("name"),
                        user_info.get("picture"),
                    ))
                    conn.commit()
                db_pool.putconn(conn)
            except Exception as e:
                print(f"User save error: {e}")
                if conn:
                    db_pool.putconn(conn)

        response = RedirectResponse("/?logged_in=1")
        response.set_cookie("ark_session", session_token, max_age=86400*30, httponly=True, samesite="lax")
        return response
    except Exception as e:
        print(f"Auth error: {e}")
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

    history = session.get("history", [])
    if isinstance(history, str):
        history = json.loads(history)

    # Поиск в интернете если нужен
    search_context = ""
    if TAVILY_API_KEY and needs_search(req.message):
        search_context = search_web(req.message)

    history.append({"role": "user", "content": req.message})
    if len(history) > 30:
        history = history[-20:]
    session["history"] = history
    save_session(req.session_id, session)

    system_prompt = build_system_prompt(req.session_id)
    if search_context:
        system_prompt += f"\n\nАктуальная информация из интернета:\n{search_context}\n\nИспользуй эти данные если они релевантны вопросу."

    headers = {"Authorization": f"Bearer {MISTRAL_KEY}", "Content-Type": "application/json"}
    body = {
        "model": session.get("model", DEFAULT_MODEL),
        "messages": [
            {"role": "system", "content": system_prompt},
            *history
        ],
        "max_tokens": 4000,
        "temperature": 0.85,
    }

    try:
        r = requests.post(API_URL, headers=headers, json=body, timeout=60)
        data = r.json()
        if "choices" in data:
            reply = data["choices"][0]["message"]["content"].strip()
            history.append({"role": "assistant", "content": reply})
            session["history"] = history
            save_session(req.session_id, session)
            return {"reply": reply, "error": False}
        return {"reply": "🤔 Пустой ответ, попробуй ещё раз.", "error": True}
    except Exception as e:
        print(f"Chat error: {e}")
        return {"reply": "⚠️ Ошибка соединения.", "error": True}

@app.post("/api/clear")
async def clear_history(req: ClearRequest):
    session = get_session(req.session_id)
    session["history"] = []
    save_session(req.session_id, session)
    return {"ok": True}

@app.get("/api/models")
async def get_models():
    return {"models": MODELS, "default": DEFAULT_MODEL}

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0"
    })

# ===================== ЗАПУСК =====================
init_db()

def run_bot():
    import bot

threading.Thread(target=run_bot, daemon=False).start()
