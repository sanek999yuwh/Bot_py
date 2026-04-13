from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests, json, os, time, threading, subprocess, sys, random
from pathlib import Path
from collections import defaultdict
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
import psycopg2.pool

from shared import (
    MODELS, DEFAULT_MODEL, API_URL, MISTRAL_KEY,
    is_dangerous, extract_facts_from_text,
    build_base_prompt, SAFE_REPLIES
)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ===================== DB =====================
db_pool = None
memory_db = {}

def init_db():
    global db_pool
    if not DATABASE_URL:
        print("WARNING: DATABASE_URL not set, using memory")
        return
    try:
        db_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
        print("✅ PostgreSQL connected")
    except Exception as e:
        print(f"DB Error: {e}")

def get_session(session_id):
    if not db_pool:
        if session_id not in memory_db:
            memory_db[session_id] = {"history": [], "model": DEFAULT_MODEL, "name": None, "facts": []}
        return memory_db[session_id]
    return {"history": [], "model": DEFAULT_MODEL, "name": None, "facts": []}

def save_session(session_id, data):
    pass

# ===================== API =====================
class ChatRequest(BaseModel):
    message: str
    session_id: str
    model: str = DEFAULT_MODEL
    temperature: float = 0.85

@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    if is_dangerous(req.message):
        async def danger():
            yield f"data: {json.dumps({'delta': random.choice(SAFE_REPLIES), 'done': True})}\n\n"
        return StreamingResponse(danger(), media_type="text/event-stream")

    session = get_session(req.session_id)
    name, facts = extract_facts_from_text(req.message, session.get("name"), session.get("facts"))
    session["name"] = name
    session["facts"] = facts

    system_prompt = build_base_prompt(name, facts)

    history = session.get("history", [])
    history.append({"role": "user", "content": req.message})
    if len(history) > 20:
        history = history[-15:]

    messages = [{"role": "system", "content": system_prompt}, *history]

    def generate():
        try:
            with requests.post(API_URL, 
                headers={"Authorization": f"Bearer {MISTRAL_KEY}"},
                json={"model": req.model, "messages": messages, "temperature": req.temperature, "stream": True},
                stream=True, timeout=60) as r:
                
                for line in r.iter_lines():
                    if line and line.startswith(b'data: '):
                        ds = line[6:]
                        if ds == b'[DONE]': break
                        try:
                            delta = json.loads(ds)["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield f"data: {json.dumps({'delta': delta, 'done': False})}\n\n"
                        except:
                            pass
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            print(f"[Error] {e}")
            yield f"data: {json.dumps({'delta': 'Ошибка соединения', 'done': True})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")

@app.get("/api/models")
async def get_models():
    return {"models": MODELS, "default": DEFAULT_MODEL}

@app.post("/api/clear")
async def clear_session(req: Request):
    data = await req.json()
    session_id = data.get("session_id", "")
    if session_id in memory_db:
        memory_db[session_id] = {"history": [], "model": DEFAULT_MODEL, "name": None, "facts": []}
    return {"ok": True}

@app.get("/auth/me")
async def auth_me():
    return JSONResponse({"logged_in": False})

@app.get("/auth/login")
async def auth_login():
    return JSONResponse({"error": "Google OAuth не настроен"}, status_code=501)

@app.get("/auth/logout")
async def auth_logout():
    return JSONResponse({"ok": True})

@app.get("/")
async def index():
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html не найден рядом с main.py</h1>", status_code=404)

# ===================== ЗАПУСК БОТА =====================
def run_telegram_bot():
    while True:
        try:
            print("🤖 Запуск Telegram бота...")
            subprocess.run([sys.executable, "bot.py"], check=True)
        except Exception as e:
            print(f"Бот упал: {e}")
            time.sleep(5)

init_db()
threading.Thread(target=run_telegram_bot, daemon=True).start()
print("🚀 Web + Telegram запущен!")
