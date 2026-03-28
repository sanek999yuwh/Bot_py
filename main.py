from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import json
import os
import time
import threading
import secrets
from collections import defaultdict
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
import psycopg2.pool

from shared import (
    MODELS, DEFAULT_MODEL, API_URL, MISTRAL_KEY,
    is_dangerous, 
    extract_facts_from_text, 
    build_base_prompt,
    needs_search, 
    search_web,
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
        print("PostgreSQL connected")
        return True
    except Exception as e:
        print(f"PostgreSQL error: {e}")
        db_pool = None
        return False

def get_session(session_id):
    if not db_pool: return _mem_get(session_id)
    conn = None
    try:
        conn = db_pool.getconn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM sessions WHERE session_id=%s", (session_id,))
            row = cur.fetchone()
            if row: db_pool.putconn(conn); return dict(row)
            now = datetime.now().strftime("%d.%m.%Y")
            cur.execute("""INSERT INTO sessions (session_id,history,model,name,facts,joined)
                           VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                        (session_id, json.dumps([]), DEFAULT_MODEL, None, json.dumps([]), now))
            row = cur.fetchone(); conn.commit(); db_pool.putconn(conn)
            return dict(row)
    except Exception as e:
        print(f"[session get] {e}")
        if conn: db_pool.putconn(conn)
        return _mem_get(session_id)

def save_session(session_id, data):
    if not db_pool: memory_db[session_id] = data; return
    conn = None
    try:
        conn = db_pool.getconn()
        with conn.cursor() as cur:
            history = data.get("history", [])
            if isinstance(history, str): history = json.loads(history)
            facts = data.get("facts", [])
            if isinstance(facts, str): facts = json.loads(facts)
            cur.execute("""UPDATE sessions SET history=%s,model=%s,name=%s,facts=%s,updated_at=NOW()
                           WHERE session_id=%s""",
                        (json.dumps(history), data.get("model", DEFAULT_MODEL),
                         data.get("name"), json.dumps(facts), session_id))
            conn.commit()
        db_pool.putconn(conn)
    except Exception as e:
        print(f"[session save] {e}")
        if conn: db_pool.putconn(conn)

def _mem_get(session_id):
    if session_id not in memory_db:
        memory_db[session_id] = {
            "session_id": session_id, "history": [], "model": DEFAULT_MODEL,
            "name": None, "facts": [], "joined": datetime.now().strftime("%d.%m.%Y"),
        }
    return memory_db[session_id]

# ===================== OAUTH =====================
oauth_states  = {}
user_sessions = {}

# ===================== UTILS =====================
RATE_LIMIT_SECONDS = 2
user_last_msg = defaultdict(float)

def check_rate_limit(session_id):
    now = time.time()
    if now - user_last_msg[session_id] < RATE_LIMIT_SECONDS: return False
    user_last_msg[session_id] = now
    return True

def _prep_history(session_id, message, model):
    session = get_session(session_id)
    session["model"] = model
    facts = session.get("facts", [])
    if isinstance(facts, str): facts = json.loads(facts)
    name, new_facts = extract_facts_from_text(message, session.get("name"), facts)
    session["name"] = name; session["facts"] = new_facts
    save_session(session_id, session)

    history = session.get("history", [])
    if isinstance(history, str): history = json.loads(history)

    system_prompt = build_base_prompt(name=session.get("name"), facts=new_facts)
    if needs_search(message):
        ctx = search_web(message)
        if ctx: system_prompt += f"\n\nАктуальная информация:\n{ctx}\nИспользуй если релевантно."

    history.append({"role": "user", "content": message})
    if len(history) > 30: history = history[-20:]
    session["history"] = history
    save_session(session_id, session)
    return [{"role": "system", "content": system_prompt}, *history], session

# ===================== AUTH =====================
@app.get("/auth/login")
async def auth_login():
    state = secrets.token_urlsafe(16)
    oauth_states[state] = True
    url = ("https://accounts.google.com/o/oauth2/v2/auth"
           f"?client_id={GOOGLE_CLIENT_ID}&redirect_uri={BASE_URL}/auth/callback"
           f"&response_type=code&scope=openid%20email%20profile&state={state}")
    return RedirectResponse(url)

@app.get("/auth/callback")
async def auth_callback(code: str = None, state: str = None, error: str = None):
    if error or not code or state not in oauth_states:
        return RedirectResponse("/?error=auth_failed")
    del oauth_states[state]
    try:
        token = requests.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": f"{BASE_URL}/auth/callback", "grant_type": "authorization_code",
        }, timeout=10).json().get("access_token")
        info = requests.get("https://www.googleapis.com/oauth2/v2/userinfo",
                            headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
        st = secrets.token_urlsafe(32)
        user_sessions[st] = {"email": info.get("email",""), "name": info.get("name",""),
                              "picture": info.get("picture",""), "id": info.get("id","")}
        if db_pool:
            conn = None
            try:
                conn = db_pool.getconn()
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO users (google_id,email,name,picture) VALUES (%s,%s,%s,%s)
                                   ON CONFLICT (google_id) DO UPDATE SET email=EXCLUDED.email,
                                   name=EXCLUDED.name, picture=EXCLUDED.picture""",
                                (info.get("id"), info.get("email"), info.get("name"), info.get("picture")))
                conn.commit(); db_pool.putconn(conn)
            except Exception as e:
                print(f"[user save] {e}")
                if conn: db_pool.putconn(conn)
        resp = RedirectResponse("/?logged_in=1")
        resp.set_cookie("ark_session", st, max_age=86400*30, httponly=True, samesite="lax")
        return resp
    except Exception as e:
        print(f"[auth] {e}")
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
    if token in user_sessions: del user_sessions[token]
    resp = RedirectResponse("/")
    resp.delete_cookie("ark_session")
    return resp

# ===================== API =====================
class ChatRequest(BaseModel):
    message: str
    session_id: str
    model: str = DEFAULT_MODEL

class ClearRequest(BaseModel):
    session_id: str

@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    if is_dangerous(req.message):
        async def danger():
            yield f"data: {json.dumps({'delta':'Брат, на такое я не подписан 😄','done':False})}\n\n"
            yield f"data: {json.dumps({'done':True})}\n\n"
        return StreamingResponse(danger(), media_type="text/event-stream")
    if not check_rate_limit(req.session_id):
        async def rate():
            yield f"data: {json.dumps({'delta':'Подожди секунду...','done':False})}\n\n"
            yield f"data: {json.dumps({'done':True})}\n\n"
        return StreamingResponse(rate(), media_type="text/event-stream")

    messages, session = _prep_history(req.session_id, req.message, req.model)
    body = {"model": session.get("model", DEFAULT_MODEL), "messages": messages,
            "max_tokens": 4000, "temperature": 0.85, "stream": True}

    def generate():
        full_reply = ""
        try:
            with requests.post(API_URL,
                    headers={"Authorization": f"Bearer {MISTRAL_KEY}", "Content-Type": "application/json"},
                    json=body, stream=True, timeout=60) as r:
                for line in r.iter_lines():
                    if not line: continue
                    line = line.decode("utf-8")
                    if not line.startswith("data: "): continue
                    ds = line[6:]
                    if ds == "[DONE]": break
                    try:
                        delta = json.loads(ds)["choices"][0]["delta"].get("content","")
                        if delta:
                            full_reply += delta
                            yield f"data: {json.dumps({'delta':delta,'done':False})}\n\n"
                    except Exception: continue
            if full_reply:
                sess = get_session(req.session_id)
                hist = sess.get("history", [])
                if isinstance(hist, str): hist = json.loads(hist)
                hist.append({"role": "assistant", "content": full_reply})
                sess["history"] = hist
                save_session(req.session_id, sess)
            yield f"data: {json.dumps({'done':True})}\n\n"
        except Exception as e:
            print(f"[stream] {e}")
            yield f"data: {json.dumps({'delta':'Ошибка соединения.','done':False})}\n\n"
            yield f"data: {json.dumps({'done':True})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")

@app.post("/api/chat")
async def chat(req: ChatRequest):
    if is_dangerous(req.message): return {"reply": "Брат, на такое я не подписан 😄", "error": False}
    if not check_rate_limit(req.session_id): return {"reply": "Подожди секунду...", "error": True}
    messages, session = _prep_history(req.session_id, req.message, req.model)
    try:
        r = requests.post(API_URL,
              headers={"Authorization": f"Bearer {MISTRAL_KEY}", "Content-Type": "application/json"},
              json={"model": session.get("model", DEFAULT_MODEL), "messages": messages,
                    "max_tokens": 4000, "temperature": 0.85}, timeout=60)
        data = r.json()
        if "choices" in data:
            reply = data["choices"][0]["message"]["content"].strip()
            sess  = get_session(req.session_id)
            hist  = sess.get("history", [])
            if isinstance(hist, str): hist = json.loads(hist)
            hist.append({"role": "assistant", "content": reply})
            sess["history"] = hist; save_session(req.session_id, sess)
            return {"reply": reply, "error": False}
        return {"reply": "Пустой ответ, попробуй ещё раз.", "error": True}
    except Exception as e:
        print(f"[chat] {e}")
        return {"reply": "Ошибка соединения.", "error": True}

@app.post("/api/voice")
async def transcribe_voice(file: UploadFile = File(...), session_id: str = Form("")):
    if not GROQ_API_KEY:
        return JSONResponse({"error": "GROQ_API_KEY не задан"}, status_code=500)
    try:
        audio_bytes = await file.read()
        r = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": (file.filename or "voice.webm", audio_bytes, file.content_type or "audio/webm")},
            data={"model": "whisper-large-v3", "language": "ru", "response_format": "json"},
            timeout=30)
        text = r.json().get("text", "").strip()
        if text: return {"text": text}
        return JSONResponse({"error": "Пустой ответ"}, status_code=500)
    except Exception as e:
        print(f"[voice] {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/clear")
async def clear_history(req: ClearRequest):
    session = get_session(req.session_id)
    session["history"] = []
    save_session(req.session_id, session)
    return {"ok": True}

@app.get("/api/models")
async def get_models():
    return {"models": MODELS, "default": DEFAULT_MODEL}

@app.get("/health")
async def health():
    return {"status": "ok", "db": db_pool is not None}

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0"})

# ===================== ЗАПУСК БОТА =====================
if not MISTRAL_KEY:
    print("WARNING: MISTRAL_KEY not set")

init_db()

def run_bot():
    try: import bot
    except Exception as e: print(f"Bot error: {e}")

threading.Thread(target=run_bot, daemon=False).start()
