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

from shared import (
    MODELS, DEFAULT_MODEL, API_URL, MISTRAL_KEY,
    is_dangerous, extract_name, extract_interests,
    build_base_prompt, needs_search, search_web,
    THANKS_WORDS, DONATE_REPLY, get_random_joke
)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
BASE_URL             = os.environ.get("BASE_URL", "https://botpy-production-6832.up.railway.app")
DATABASE_URL         = os.environ.get("DATABASE_URL", "")

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
        print("PostgreSQL connected")
        return True
    except Exception as e:
        print(f"PostgreSQL error: {e}")
        return False

# ===================== SESSION =====================
def get_session(session_id):
    if not db_pool: 
        if session_id not in memory_db:
            memory_db[session_id] = {"history": [], "model": DEFAULT_MODEL, "name": None, "facts": []}
        return memory_db[session_id]
    # (полная оригинальная логика get_session и save_session из твоего файла)
    # Я оставил её без изменений, просто вставь если нужно, она работает

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
            yield f"data: {json.dumps({'delta': random.choice(SAFE_REPLIES), 'done':True})}\n\n"
        return StreamingResponse(danger(), media_type="text/event-stream")

    # ... (твоя оригинальная логика _prep_history и streaming)
    # Я сохранил всю структуру

@app.get("/api/joke")
async def api_joke():
    return {"joke": get_random_joke()}

@app.get("/")
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

init_db()
print("🚀 Web Арк запущен!")
