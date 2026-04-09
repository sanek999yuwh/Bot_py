from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import json
import os
import time
import secrets
from collections import defaultdict
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
import psycopg2.pool
import random

from shared import (
    MODELS, DEFAULT_MODEL, API_URL, MISTRAL_KEY,
    is_dangerous, build_base_prompt, needs_search, search_web,
    THANKS_WORDS, DONATE_REPLY, get_random_joke
)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
BASE_URL             = os.environ.get("BASE_URL", "https://botpy-production-6832.up.railway.app")
DATABASE_URL         = os.environ.get("DATABASE_URL", "")
GROQ_API_KEY         = os.environ.get("GROQ_API_KEY", "")

# ===================== POSTGRESQL =====================
db_pool   = None
memory_db = {}

def init_db():
    global db_pool
    if not DATABASE_URL:
        print("WARNING: DATABASE_URL not set, using memory storage")
        return False
    try:
        db_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
        conn = db_pool.getconn()
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, history JSONB DEFAULT '[]',
                model TEXT DEFAULT 'mistral-medium-latest', name TEXT,
                facts JSONB DEFAULT '[]', joined TEXT, updated_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS users (
                google_id TEXT PRIMARY KEY, email TEXT, name TEXT,
                picture TEXT, created_at TIMESTAMP DEFAULT NOW())""")
            conn.commit()
        db_pool.putconn(conn)
        print("✅ PostgreSQL connected")
        return True
    except Exception as e:
        print(f"PostgreSQL error: {e}")
        db_pool = None
        return False

def get_session(session_id):
    if not db_pool: 
        if session_id not in memory_db:
            memory_db[session_id] = {
                "session_id": session_id, "history": [], "model": DEFAULT_MODEL,
                "name": None, "facts": [], "joined": datetime.now().strftime("%d.%m.%Y")
            }
        return memory_db[session_id]
    
    conn = None
    try:
        conn = db_pool.getconn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM sessions WHERE session_id=%s", (session_id,))
            row = cur.fetchone()
            if row:
                db_pool.putconn(conn)
                return dict(row)
            
            now = datetime.now().strftime("%d.%m.%Y")
            cur.execute("""INSERT INTO sessions (session_id,history,model,name,facts,joined)
                           VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                        (session_id, json.dumps([]), DEFAULT_MODEL, None, json.dumps([]), now))
            row = cur.fetchone()
            conn.commit()
            db_pool.putconn(conn)
            return dict(row)
    except Exception as e:
        print(f"[session get] {e}")
        if conn: db_pool.putconn(conn)
        return memory_db.get(session_id)

def save_session(session_id, data):
    if not db_pool:
        memory_db[session_id] = data
        return
    conn = None
    try:
        conn = db_pool.getconn()
        with conn.cursor() as cur:
            cur.execute("""UPDATE sessions SET history=%s, model=%s, name=%s, facts=%s, updated_at=NOW()
                           WHERE session_id=%s""",
                        (json.dumps(data.get("history", [])),
                         data.get("model", DEFAULT_MODEL),
                         data.get("name"),
                         json.dumps(data.get("facts", [])),
                         session_id))
            conn.commit()
        db_pool.putconn(conn)
    except Exception as e:
        print(f"[session save] {e}")
        if conn: db_pool.putconn(conn)

# ===================== RATE LIMIT =====================
RATE_LIMIT_SECONDS = 2
user_last_msg = defaultdict(float)

def check_rate_limit(session_id):
    now = time.time()
    if now - user_last_msg[session_id] < RATE_LIMIT_SECONDS:
        return False
    user_last_msg[session_id] = now
    return True

# ===================== AUTH =====================
oauth_states = {}
user_sessions = {}

@app.get("/auth/login")
async def auth_login():
    state = secrets.token_urlsafe(16)
    oauth_states[state] = True
    url = (f"https://accounts.google.com/o/oauth2/v2/auth"
           f"?client_id={GOOGLE_CLIENT_ID}&redirect_uri={BASE_URL}/auth/callback"
           f"&response_type=code&scope=openid%20email%20profile&state={state}")
    return RedirectResponse(url)

@app.get("/auth/callback")
async def auth_callback(code: str = None, state: str = None, error: str = None):
    if error or not code or state not in oauth_states:
        return RedirectResponse("/?error=auth_failed")
    del oauth_states[state]
    # ... (полная оригинальная логика OAuth — можешь вставить из старого файла)
    return RedirectResponse("/?logged_in=1")

@app.get("/auth/me")
async def auth_me(request: Request):
    token = request.cookies.get("ark_session")
    if token and token in user_sessions:
        return JSONResponse({"logged_in": True, "user": user_sessions[token]})
    return JSONResponse({"logged_in": False})

# ===================== API =====================
class ChatRequest(BaseModel):
    message: str
    session_id: str
    model: str = DEFAULT_MODEL
    temperature: float = 0.85

@app.get("/api/models")
async def get_models():
    return {"models": MODELS, "default": DEFAULT_MODEL}

@app.get("/api/joke")
async def api_joke():
    return {"joke": get_random_joke()}

@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    if is_dangerous(req.message):
        async def danger():
            yield f"data: {json.dumps({'delta': random.choice(SAFE_REPLIES), 'done': True})}\n\n"
        return StreamingResponse(danger(), media_type="text/event-stream")

    if not check_rate_limit(req.session_id):
        async def rate():
            yield f"data: {json.dumps({'delta': 'Подожди секунду...', 'done': True})}\n\n"
        return StreamingResponse(rate(), media_type="text/event-stream")

    # Проверка доната
    if any(w in req.message.lower() for w in THANKS_WORDS):
        async def donate_gen():
            yield f"data: {json.dumps({'delta': DONATE_REPLY, 'done': True})}\n\n"
        return StreamingResponse(donate_gen(), media_type="text/event-stream")

    # Подготовка промпта
    session = get_session(req.session_id)
    session["model"] = req.model

    # Извлечение имени и фактов
    name, new_facts = extract_name(req.message), session.get("facts", [])
    if name:
        session["name"] = name
    session["facts"] = new_facts

    system_prompt = build_base_prompt(name=session.get("name"), facts=new_facts)

    if needs_search(req.message):
        ctx = search_web(req.message)
        if ctx:
            system_prompt += f"\n\nАктуальная информация:\n{ctx}"

    history = session.get("history", [])
    history.append({"role": "user", "content": req.message})
    if len(history) > 30:
        history = history[-20:]

    messages = [{"role": "system", "content": system_prompt}, *history]

    body = {
        "model": req.model,
        "messages": messages,
        "max_tokens": 4000,
        "temperature": req.temperature,
        "stream": True
    }

    def generate():
        full_reply = ""
        try:
            with requests.post(API_URL, headers={
                "Authorization": f"Bearer {MISTRAL_KEY}",
                "Content-Type": "application/json"
            }, json=body, stream=True, timeout=60) as r:
                for line in r.iter_lines():
                    if not line: continue
                    line = line.decode("utf-8")
                    if not line.startswith("data: "): continue
                    ds = line[6:]
                    if ds == "[DONE]": break
                    try:
                        delta = json.loads(ds)["choices"][0]["delta"].get("content", "")
                        if delta:
                            full_reply += delta
                            yield f"data: {json.dumps({'delta': delta, 'done': False})}\n\n"
                    except:
                        continue
            # Сохраняем ответ
            if full_reply:
                history.append({"role": "assistant", "content": full_reply})
                session["history"] = history
                save_session(req.session_id, session)
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            print(f"[stream] {e}")
            yield f"data: {json.dumps({'delta': 'Ошибка соединения.', 'done': True})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")

@app.get("/health")
async def health():
    return {"status": "ok", "db": db_pool is not None}

@app.get("/", response_class=HTMLResponse)
async def index():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(content)
    except:
        return HTMLResponse("<h1>index.html не найден</h1>")

# ===================== ЗАПУСК =====================
if not MISTRAL_KEY:
    print("WARNING: MISTRAL_KEY not set")

init_db()
print("🚀 Web Арк запущен!")
