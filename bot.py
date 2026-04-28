import telebot
import requests
import base64
import time
import threading
import re
import json
import os
import random
import psycopg2
import psycopg2.pool
from collections import defaultdict
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from shared import (
    MISTRAL_KEY, DATABASE_URL, API_URL, DEFAULT_MODEL, MODELS, BOT_NAME, WEB_URL,
    is_dangerous, SAFE_REPLIES,
    extract_name, extract_interests, detect_mood, detect_style,
    build_prompt, build_summary_prompt, build_table_prompt
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID       = int(os.environ.get("ADMIN_ID", "0"))
VISION_MODEL   = "pixtral-12b-2409"

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# ===================== POSTGRESQL =====================
db_pool = None

def init_db():
    global db_pool
    if not DATABASE_URL:
        print("⚠️ [bot] DATABASE_URL не задан — используем memory storage")
        return False
    try:
        db_pool = psycopg2.pool.ThreadedConnectionPool(1, 5, DATABASE_URL)
        conn = db_pool.getconn()
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS banned_users (uid BIGINT PRIMARY KEY)""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_users (
                    uid BIGINT PRIMARY KEY, name TEXT,
                    history JSONB DEFAULT '[]', facts JSONB DEFAULT '[]',
                    summary TEXT DEFAULT '', style TEXT DEFAULT 'neutral',
                    mood TEXT DEFAULT 'neutral', model TEXT DEFAULT 'mistral-medium-latest',
                    msg_count INTEGER DEFAULT 0, joined TEXT, last_active TEXT
                )
            """)
            conn.commit()
        db_pool.putconn(conn)
        print("✅ [bot] PostgreSQL подключён")
        return True
    except Exception as e:
        print(f"❌ [bot] PostgreSQL ошибка: {e}")
        db_pool = None
        return False

def _gc(): return db_pool.getconn()
def _pc(c): db_pool.putconn(c)

# ===================== БАН-ЛИСТ =====================
banned_users = set()

def load_banned():
    global banned_users
    if not db_pool: return
    conn = None
    try:
        conn = _gc()
        with conn.cursor() as cur:
            cur.execute("SELECT uid FROM banned_users")
            banned_users = set(r[0] for r in cur.fetchall())
    except Exception as e:
        print(f"[ban load] {e}")
    finally:
        if conn: _pc(conn)

def ban_user(uid):
    banned_users.add(uid)
    if not db_pool: return
    conn = None
    try:
        conn = _gc()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO banned_users VALUES(%s) ON CONFLICT DO NOTHING", (uid,))
        conn.commit()
    except Exception as e:
        print(f"[ban] {e}")
    finally:
        if conn: _pc(conn)

def unban_user(uid):
    banned_users.discard(uid)
    if not db_pool: return
    conn = None
    try:
        conn = _gc()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM banned_users WHERE uid=%s", (uid,))
        conn.commit()
    except Exception as e:
        print(f"[unban] {e}")
    finally:
        if conn: _pc(conn)

# ===================== ПАМЯТЬ =====================
_mem = {}
_cache = {}
_cache_lock = threading.Lock()

def get_user(uid) -> dict:
    with _cache_lock:
        if uid in _cache: return _cache[uid]
    user = _load_user(uid)
    with _cache_lock:
        _cache[uid] = user
    return user

def save_user(uid):
    user = _cache.get(uid)
    if not user: return
    if db_pool: _save_user_db(uid, user)

def _load_user(uid) -> dict:
    if db_pool:
        conn = None
        try:
            conn = _gc()
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM bot_users WHERE uid=%s", (uid,))
                row = cur.fetchone()
                if row:
                    cols = [d[0] for d in cur.description]
                    data = dict(zip(cols, row))
                    for f in ("history","facts"):
                        if isinstance(data[f], str): data[f] = json.loads(data[f])
                    _pc(conn)
                    return data
                now = datetime.now()
                cur.execute("""INSERT INTO bot_users (uid,history,facts,joined,last_active)
                              VALUES(%s,%s,%s,%s,%s) RETURNING *""",
                            (uid,json.dumps([]),json.dumps([]),
                             now.strftime("%d.%m.%Y"),now.strftime("%d.%m.%Y %H:%M")))
                row = cur.fetchone()
                cols = [d[0] for d in cur.description]
                data = dict(zip(cols, row))
                conn.commit(); _pc(conn)
                for f in ("history","facts"):
                    if isinstance(data[f], str): data[f] = json.loads(data[f])
                return data
        except Exception as e:
            print(f"[load_user] {e}")
            if conn: _pc(conn)
    key = str(uid)
    if key not in _mem:
        now = datetime.now()
        _mem[key] = {"uid":uid,"name":None,"history":[],"facts":[],"summary":"",
                     "style":"neutral","mood":"neutral","model":DEFAULT_MODEL,"msg_count":0,
                     "joined":now.strftime("%d.%m.%Y"),"last_active":now.strftime("%d.%m.%Y %H:%M")}
    return _mem[key]

def _save_user_db(uid, user):
    conn = None
    try:
        conn = _gc()
        with conn.cursor() as cur:
            cur.execute("""UPDATE bot_users SET name=%s,history=%s,facts=%s,summary=%s,
    style=%s,mood=%s,model=%s,msg_count=%s,last_active=%s WHERE uid=%s""",
    (user.get("name"),json.dumps(user.get("history",[])),json.dumps(user.get("facts",[])),
     user.get("summary",""),user.get("style","neutral"),user.get("mood","neutral"),
     user.get("model",DEFAULT_MODEL),user.get("msg_count",0),user.get("last_active",""),uid))
    except Exception as e:
        print(f"[save_user] {e}")
    finally:
        if conn: _pc(conn)

def delete_user(uid):
    with _cache_lock: _cache.pop(uid, None)
    _mem.pop(str(uid), None)
    if db_pool:
        conn = None
        try:
            conn = _gc()
            with conn.cursor() as cur: cur.execute("DELETE FROM bot_users WHERE uid=%s",(uid,))
            conn.commit()
        except Exception as e: print(f"[delete_user] {e}")
        finally:
            if conn: _pc(conn)

def all_uids():
    if db_pool:
        conn = None
        try:
            conn = _gc()
            with conn.cursor() as cur:
                cur.execute("SELECT uid FROM bot_users")
                return [r[0] for r in cur.fetchall()]
        except: return []
        finally:
            if conn: _pc(conn)
    return [int(k) for k in _mem.keys()]

def user_count():
    if db_pool:
        conn = None
        try:
            conn = _gc()
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM bot_users")
                return cur.fetchone()[0]
        except: return 0
        finally:
            if conn: _pc(conn)
    return len(_mem)

def active_today():
    today = datetime.now().strftime("%d.%m.%Y")
    if db_pool:
        conn = None
        try:
            conn = _gc()
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM bot_users WHERE last_active LIKE %s",(f"{today}%",))
                return cur.fetchone()[0]
        except: return 0
        finally:
            if conn: _pc(conn)
    return sum(1 for u in _mem.values() if u.get("last_active","").startswith(today))

def total_msgs():
    if db_pool:
        conn = None
        try:
            conn = _gc()
            with conn.cursor() as cur:
                cur.execute("SELECT SUM(msg_count) FROM bot_users")
                return cur.fetchone()[0] or 0
        except: return 0
        finally:
            if conn: _pc(conn)
    return sum(u.get("msg_count",0) for u in _mem.values())

def recent_users(n=10):
    if db_pool:
        conn = None
        try:
            conn = _gc()
            with conn.cursor() as cur:
                cur.execute("SELECT uid,name,msg_count,last_active FROM bot_users ORDER BY last_active DESC LIMIT %s",(n,))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols,r)) for r in cur.fetchall()]
        except: return []
        finally:
            if conn: _pc(conn)
    return list(_mem.values())[-n:]

# ===================== КЛАВИАТУРЫ =====================
def reply_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("⚙️ Меню"), KeyboardButton("🧠 Что ты знаешь обо мне"))
    kb.row(KeyboardButton("📝 Сжать текст"), KeyboardButton("📊 Таблица"))
    return kb

def reply_keyboard_admin():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("⚙️ Меню"), KeyboardButton("🧠 Что ты знаешь обо мне"))
    kb.row(KeyboardButton("📝 Сжать текст"), KeyboardButton("📊 Таблица"))
    kb.row(KeyboardButton("👑 Админ панель"))
    return kb

def get_reply_kb(uid): return reply_keyboard_admin() if uid == ADMIN_ID else reply_keyboard()

def main_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("🤖 Сменить модель",callback_data="menu_models"),
           InlineKeyboardButton("🗑️ Очистить историю",callback_data="menu_clear"),
           InlineKeyboardButton("💀 Забыть всё",callback_data="menu_forget"))
    return kb

def models_keyboard(current):
    kb = InlineKeyboardMarkup(row_width=1)
    for mid,label in MODELS.items():
        kb.add(InlineKeyboardButton(f"{'✅ ' if mid==current else ''}{label}",callback_data=f"model_{mid}"))
    kb.add(InlineKeyboardButton("◀️ Назад",callback_data="menu_back"))
    return kb

def admin_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("👥 Пользователи",callback_data="admin_users"),
           InlineKeyboardButton("📊 Статистика",callback_data="admin_stats"),
           InlineKeyboardButton("📢 Рассылка",callback_data="admin_broadcast"),
           InlineKeyboardButton("🚫 Бан-лист",callback_data="admin_bans"))
    return kb

# ===================== MARKDOWN / ОТПРАВКА =====================
def fix_md(text):
    # Убираем # только вне блоков кода
    parts = re.split(r'(```[\s\S]*?```)', text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            result.append(part)  # код — не трогаем
        else:
            part = re.sub(r'^#{1,6}\s+', '', part, flags=re.MULTILINE)
            part = re.sub(r'^-{3,}$', '', part, flags=re.MULTILINE)
            part = re.sub(r'\n{3,}', '\n\n', part)
            result.append(part)
    return ''.join(result).strip()

def safe_md(text):
    for ch in ["*","_","`"]:
        if text.count(ch)%2!=0: text+=ch
    return text

def smart_split(text,limit=4000):
    if len(text)<=limit: return [text]
    parts,remaining=[],text
    while len(remaining)>limit:
        chunk=remaining[:limit]
        cut=max(chunk.rfind('. '),chunk.rfind('.\n'),chunk.rfind('! '),
                chunk.rfind('!\n'),chunk.rfind('? '),chunk.rfind('?\n'))
        if cut<=limit//2: cut=chunk.rfind('\n')
        if cut<=limit//2: cut=limit
        parts.append(remaining[:cut].strip()); remaining=remaining[cut:].strip()
    if remaining: parts.append(remaining)
    return parts

def send_safe(chat_id,text,**kw):
    text=fix_md(text)
    try: bot.send_message(chat_id,text,parse_mode="Markdown",**kw)
    except Exception:
        try: bot.send_message(chat_id,re.sub(r'[*_`]','',text),**kw)
        except Exception: pass

def edit_safe(text,chat_id,msg_id):
    text=fix_md(text)
    try: bot.edit_message_text(text,chat_id,msg_id,parse_mode="Markdown")
    except Exception:
        try: bot.edit_message_text(re.sub(r'[*_`]','',text),chat_id,msg_id)
        except Exception: pass

def send_long(chat_id,text):
    for i,part in enumerate(smart_split(fix_md(text))):
        if i>0: part="➡️ *Продолжение:*\n\n"+part
        send_safe(chat_id,part)
        if i>0: time.sleep(0.5)

# ===================== RATE LIMIT =====================
_last_msg=defaultdict(float); _msg_times=defaultdict(list)

def check_rate(uid):
    now=time.time()
    if now-_last_msg[uid]<3:
        return False,f"⏳ Подожди {round(3-(now-_last_msg[uid]),1)} сек."
    _msg_times[uid]=[t for t in _msg_times[uid] if now-t<60]
    if len(_msg_times[uid])>=10: return False,"🚫 Слишком много сообщений! Подожди минуту."
    _last_msg[uid]=now; _msg_times[uid].append(now)
    return True,""

# ===================== РАБОТА С ПОЛЬЗОВАТЕЛЕМ =====================
def update_facts(uid,text):
    user=get_user(uid)
    name=extract_name(text)
    if name: user["name"]=name
    user["facts"]=extract_interests(text,user.get("facts",[]))
    user["mood"]=detect_mood(text); user["style"]=detect_style(text)
    save_user(uid)

def add_message(uid,role,content):
    user=get_user(uid)
    user["history"].append({"role":role,"content":content})
    user["msg_count"]=user.get("msg_count",0)+1
    user["last_active"]=datetime.now().strftime("%d.%m.%Y %H:%M")
    if len(user["history"])>30:
        threading.Thread(target=compress_history,args=(uid,),daemon=True).start()
    save_user(uid)

def compress_history(uid):
    user=get_user(uid)
    if len(user["history"])<=20: return
    old=user["history"][:15]; user["history"]=user["history"][15:]
    old_text="\n".join(f"{m['role']}: {m['content']}" for m in old)
    headers={"Authorization":f"Bearer {MISTRAL_KEY}","Content-Type":"application/json"}
    try:
        r=requests.post(API_URL,headers=headers,json={"model":"mistral-small-latest",
            "messages":[{"role":"user","content":f"Сожми диалог в 3-5 предложений:\n\n{old_text}"}],
            "max_tokens":200},timeout=30)
        data=r.json()
        if "choices" in data:
            ns=data["choices"][0]["message"]["content"].strip()
            user["summary"]=(user.get("summary","")+" "+ns).strip()[-1000:]
            save_user(uid)
    except Exception as e: print(f"[compress] {e}")

def get_system_prompt(uid,mode="chat"):
    if mode=="summary": return build_summary_prompt()
    if mode=="table":   return build_table_prompt()
    user=get_user(uid)
    return build_prompt(name=user.get("name"),facts=user.get("facts",[]),
                        summary=user.get("summary",""),mood=user.get("mood","neutral"),
                        style=user.get("style","neutral"),web=False)

# ===================== ЗАПРОС К ИИ =====================
def ask_ai(uid,text,chat_id,mode="chat"):
    if mode=="chat": add_message(uid,"user",text); update_facts(uid,text)
    user=get_user(uid); model=user.get("model",DEFAULT_MODEL)
    headers={"Authorization":f"Bearer {MISTRAL_KEY}","Content-Type":"application/json"}
    messages=([{"role":"system","content":get_system_prompt(uid,mode)},{"role":"user","content":text}]
              if mode in("summary","table") else
              [{"role":"system","content":get_system_prompt(uid,"chat")},*user["history"]])
    body={"model":model,"messages":messages,"max_tokens":1200,"stream":True,
          "temperature":0.7 if mode in("summary","table") else 0.85}
    msg=None
    try:
        msg=bot.send_message(chat_id,"⏳"); full=""; last_upd=time.time()
        with requests.post(API_URL,headers=headers,json=body,stream=True,timeout=60) as r:
            for line in r.iter_lines():
                if not line: continue
                line=line.decode("utf-8")
                if not line.startswith("data: "): continue
                ds=line[6:]
                if ds=="[DONE]": break
                try:
                    delta=json.loads(ds)["choices"][0]["delta"].get("content","")
                    if delta:
                        full+=delta
                        if time.time()-last_upd>0.8:
                            edit_safe(fix_md(safe_md(full[-3800:]))+"▌",chat_id,msg.message_id)
                            last_upd=time.time()
                except Exception: continue
        if full:
            try: bot.delete_message(chat_id,msg.message_id)
            except Exception: pass
            send_long(chat_id,full)
            if mode=="chat": add_message(uid,"assistant",full)
        else: edit_safe("🤔 Пустой ответ, попробуй ещё раз.",chat_id,msg.message_id)
    except requests.exceptions.Timeout:
        if msg: edit_safe("⚠️ Таймаут, попробуй ещё раз.",chat_id,msg.message_id)
    except requests.exceptions.ConnectionError:
        if msg: edit_safe("⚠️ Проблема с интернетом.",chat_id,msg.message_id)
    except Exception as e:
        txt="⚠️ Проблема с соединением." if any(w in str(e).lower() for w in ["token","key","auth","ssl","connection","timeout"]) else "⚠️ Что-то пошло не так."
        if msg: edit_safe(txt,chat_id,msg.message_id)
        else: send_safe(chat_id,txt)

def ask_ai_image(uid,image_bytes,caption=""):
    b64=base64.b64encode(image_bytes).decode("utf-8")
    user_text=caption or "Посмотри на изображение. Если математика — реши пошагово. Если текст — прочитай. Если фото — опиши."
    headers={"Authorization":f"Bearer {MISTRAL_KEY}","Content-Type":"application/json"}
    body={"model":VISION_MODEL,"messages":[
        {"role":"system","content":get_system_prompt(uid)},
        {"role":"user","content":[
            {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}},
            {"type":"text","text":user_text}]}],"max_tokens":800}
    try:
        r=requests.post(API_URL,headers=headers,json=body,timeout=90); data=r.json()
        if "choices" in data:
            reply=data["choices"][0]["message"]["content"].strip()
            add_message(uid,"assistant",f"[по картинке]: {reply}")
            return reply
        return "⚠️ Не смог обработать картинку."
    except Exception as e:
        print(f"[image] {e}"); return "⚠️ Проблема с интернетом, подождите."

# ===================== НАПОМИНАНИЯ =====================
user_reminders={}

def parse_reminder(text):
    for pattern,unit in [(r"через (\d+)\s*сек","seconds"),(r"через (\d+)\s*мин","minutes"),
                         (r"через (\d+)\s*час","hours"),(r"через (\d+)\s*д[её]н","days")]:
        m=re.search(pattern,text.lower())
        if m:
            n=int(m.group(1))
            delta={"seconds":timedelta(seconds=n),"minutes":timedelta(minutes=n),
                   "hours":timedelta(hours=n),"days":timedelta(days=n)}[unit]
            rt=re.sub(r"(напомни|напомнить|напоминание).{0,20}через \d+\s*\w+\s*","",text,flags=re.I).strip()
            return datetime.now()+delta, rt or "Время!"
    return None,None

def reminder_checker():
    while True:
        now=datetime.now()
        for uid,reminders in list(user_reminders.items()):
            for r in reminders[:]:
                if now>=r["time"]:
                    try: send_safe(r["chat_id"],f"⏰ *Напоминание!*\n{r['text']}")
                    except Exception: pass
                    reminders.remove(r)
        time.sleep(10)

threading.Thread(target=reminder_checker,daemon=True).start()

# ===================== СОСТОЯНИЯ + КОЛБЭКИ =====================
user_states={}

@bot.callback_query_handler(func=lambda c:True)
def handle_callback(call):
    uid=call.from_user.id; data=call.data
    if data=="menu_models":
        user=get_user(uid)
        try: bot.edit_message_text("🤖 Выбери модель:",call.message.chat.id,call.message.message_id,
                                   reply_markup=models_keyboard(user.get("model",DEFAULT_MODEL)))
        except Exception: pass
    elif data.startswith("model_"):
        mid=data[6:]
        if mid in MODELS:
            get_user(uid)["model"]=mid; save_user(uid)
            try: bot.edit_message_text(f"✅ Модель: *{MODELS[mid]}*",call.message.chat.id,
                    call.message.message_id,parse_mode="Markdown",reply_markup=models_keyboard(mid))
            except Exception: pass
    elif data=="menu_clear":
        get_user(uid)["history"]=[]; save_user(uid); user_states[uid]=None
        try: bot.edit_message_text("🗑️ История очищена!",call.message.chat.id,
                                   call.message.message_id,reply_markup=main_keyboard())
        except Exception: pass
    elif data=="menu_forget":
        delete_user(uid); user_states[uid]=None
        try: bot.edit_message_text("💀 Всё забыл!",call.message.chat.id,call.message.message_id)
        except Exception: pass
    elif data=="menu_back":
        user_states[uid]=None
        try: bot.edit_message_text("⚙️ Меню:",call.message.chat.id,call.message.message_id,reply_markup=main_keyboard())
        except Exception: pass
    elif data=="admin_users":
        if uid!=ADMIN_ID: bot.answer_callback_query(call.id,"⛔ Доступ запрещён"); return
        text=f"👥 *Пользователи:*\n\nВсего: {user_count()}\nАктивны сегодня: {active_today()}\nЗабанено: {len(banned_users)}\n\n*Последние 10:*\n"
        for u in recent_users(10):
            ban=" 🚫" if u.get("uid") in banned_users else ""
            text+=f"• {u.get('name') or 'Без имени'}{ban} — {u.get('msg_count',0)} сообщ. ({u.get('last_active','?')})\n"
        try: bot.edit_message_text(text,call.message.chat.id,call.message.message_id,parse_mode="Markdown",reply_markup=admin_keyboard())
        except Exception: pass
    elif data=="admin_stats":
        if uid!=ADMIN_ID: bot.answer_callback_query(call.id,"⛔ Доступ запрещён"); return
        tu=user_count(); tm=total_msgs()
        text=f"📊 *Статистика:*\n\nПользователей: {tu}\nСообщений: {tm}\nСреднее: {tm//tu if tu else 0}\nЗабанено: {len(banned_users)}"
        try: bot.edit_message_text(text,call.message.chat.id,call.message.message_id,parse_mode="Markdown",reply_markup=admin_keyboard())
        except Exception: pass
    elif data=="admin_broadcast":
        if uid!=ADMIN_ID: bot.answer_callback_query(call.id,"⛔ Доступ запрещён"); return
        user_states[uid]="broadcast"
        try: bot.edit_message_text("📢 *Рассылка*\n\nНапиши сообщение:",call.message.chat.id,call.message.message_id,parse_mode="Markdown")
        except Exception: pass
    elif data=="admin_bans":
        if uid!=ADMIN_ID: bot.answer_callback_query(call.id,"⛔ Доступ запрещён"); return
        text=("🚫 *Бан-лист пуст*\n\n/ban [id] — забанить\n/unban [id] — разбанить" if not banned_users else
              "🚫 *Забаненные:*\n\n"+"\n".join(f"• `{u}`" for u in banned_users)+"\n\n/unban [id] — разбанить")
        try: bot.edit_message_text(text,call.message.chat.id,call.message.message_id,parse_mode="Markdown",reply_markup=admin_keyboard())
        except Exception: pass
    bot.answer_callback_query(call.id)

# ===================== ХЭНДЛЕРЫ =====================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid=message.from_user.id; name=message.from_user.first_name or "друг"
    get_user(uid)["name"]=name; save_user(uid)
    send_safe(message.chat.id,
        f"Привет, {name}! 👋\n\nЯ — *{BOT_NAME}*. Твой цифровой друг.\n\n"
        "💬 Умные ответы\n🖼️ Анализ картинок\n📝 Суммаризация\n"
        "📊 Таблицы\n🧠 Постоянная память\n\nКнопки внизу 👇",reply_markup=get_reply_kb(uid))
    bot.send_message(message.chat.id,"🌐 Веб-версия:",
        reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("🌐 Открыть сайт",url=WEB_URL)))

@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if message.from_user.id!=ADMIN_ID: bot.send_message(message.chat.id,"⛔ Доступ запрещён."); return
    send_safe(message.chat.id,"👑 *Админ-панель:*",reply_markup=admin_keyboard())

@bot.message_handler(commands=["ban"])
def cmd_ban(message):
    if message.from_user.id!=ADMIN_ID: bot.send_message(message.chat.id,"⛔ Доступ запрещён."); return
    parts=message.text.split()
    if len(parts)<2: bot.send_message(message.chat.id,"Использование: /ban [user_id]"); return
    try:
        ban_user(int(parts[1]))
        bot.send_message(message.chat.id,f"🚫 Пользователь `{parts[1]}` забанен.",parse_mode="Markdown")
    except Exception: bot.send_message(message.chat.id,"Неверный ID.")

@bot.message_handler(commands=["unban"])
def cmd_unban(message):
    if message.from_user.id!=ADMIN_ID: bot.send_message(message.chat.id,"⛔ Доступ запрещён."); return
    parts=message.text.split()
    if len(parts)<2: bot.send_message(message.chat.id,"Использование: /unban [user_id]"); return
    try:
        unban_user(int(parts[1]))
        bot.send_message(message.chat.id,f"✅ Пользователь `{parts[1]}` разбанен.",parse_mode="Markdown")
    except Exception: bot.send_message(message.chat.id,"Неверный ID.")

@bot.message_handler(commands=["help"])
def cmd_help(message):
    send_safe(message.chat.id,"📋 *Команды:*\n\n/start — начать сначала\n/help — помощь\n\nИспользуй кнопки внизу 👇",
              reply_markup=get_reply_kb(message.from_user.id))

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    uid=message.from_user.id
    if uid in banned_users: bot.send_message(message.chat.id,"⛔ Доступ запрещён."); return
    caption=message.caption or ""
    if is_dangerous(caption): bot.send_message(message.chat.id,random.choice(SAFE_REPLIES)); return
    ok,reason=check_rate(uid)
    if not ok: bot.send_message(message.chat.id,reason); return
    bot.send_chat_action(message.chat.id,"typing")
    try:
        fi=bot.get_file(message.photo[-1].file_id)
        send_long(message.chat.id,ask_ai_image(uid,bot.download_file(fi.file_path),caption))
    except Exception: send_safe(message.chat.id,"⚠️ Проблема с интернетом, подождите.")

@bot.message_handler(func=lambda m:True)
def handle_message(message):
    if not message.text: return
    uid=message.from_user.id; text=message.text
    if uid in banned_users: bot.send_message(message.chat.id,"⛔ Доступ запрещён."); return
    if text=="⚙️ Меню": bot.send_message(message.chat.id,"⚙️ Меню:",reply_markup=main_keyboard()); return
    if text=="🧠 Что ты знаешь обо мне":
        user=get_user(uid)
        mood_map={"sad":"😔 грустит","happy":"😄 хорошее","angry":"😤 раздражён","neutral":"🙂 нейтральное"}
        t=(f"🧠 *Что я знаю о тебе:*\n\nИмя: {user['name'] or 'не знаю'}\n"
           f"Настроение: {mood_map.get(user.get('mood','neutral'),'🙂')}\n"
           f"Модель: {MODELS.get(user.get('model',DEFAULT_MODEL),'?')}\n"
           f"Сообщений: {user.get('msg_count',0)}\nС нами с: {user.get('joined','?')}\n"
           f"Фактов: {len(user['facts'])}")
        if user["facts"]: t+="\n\n"+"\n".join(f"• {f}" for f in user["facts"])
        send_safe(message.chat.id,t); return
    if text=="📝 Сжать текст":
        user_states[uid]="summary"; bot.send_message(message.chat.id,"📝 Отправь текст для суммаризации 👇"); return
    if text=="📊 Таблица":
        user_states[uid]="table"; bot.send_message(message.chat.id,"📊 Опиши данные для таблицы 👇"); return
    if text=="👑 Админ панель":
        if uid!=ADMIN_ID: bot.send_message(message.chat.id,"⛔ Доступ запрещён."); return
        send_safe(message.chat.id,"👑 *Админ-панель:*",reply_markup=admin_keyboard()); return
    ok,reason=check_rate(uid)
    if not ok: bot.send_message(message.chat.id,reason); return
    get_user(uid)["last_active"]=datetime.now().strftime("%d.%m.%Y %H:%M"); save_user(uid)
    state=user_states.get(uid)
    if state=="broadcast" and uid==ADMIN_ID:
        user_states[uid]=None; sent=failed=0
        for ui in all_uids():
            try:
                bot.send_message(int(ui),f"📢 *Сообщение от создателя:*\n\n{text}",parse_mode="Markdown")
                sent+=1; time.sleep(0.05)
            except Exception: failed+=1
        bot.send_message(message.chat.id,f"📢 Рассылка завершена!\n✅ Доставлено: {sent}\n❌ Ошибок: {failed}"); return
    if state in("summary","table"):
        user_states[uid]=None; bot.send_chat_action(message.chat.id,"typing")
        threading.Thread(target=ask_ai,args=(uid,text,message.chat.id,state),daemon=True).start(); return
    if any(kw in text.lower() for kw in ["напомни","напоминание","напомнить"]):
        rt,rmsg=parse_reminder(text)
        if rt:
            user_reminders.setdefault(uid,[]).append({"time":rt,"text":rmsg,"chat_id":message.chat.id})
            delta=rt-datetime.now(); mins=int(delta.total_seconds()//60); secs=int(delta.total_seconds()%60)
            send_safe(message.chat.id,f"⏰ Напомню через {f'{mins} мин. {secs} сек.' if mins else f'{secs} сек.'}!\n*{rmsg}*"); return
    bot.send_chat_action(message.chat.id,"typing")
    threading.Thread(target=ask_ai,args=(uid,text,message.chat.id,"chat"),daemon=True).start()
)
# ===================== ЗАПУСК БОТА =====================
if not TELEGRAM_TOKEN:
    print("❌ TELEGRAM_TOKEN не задан")
elif not MISTRAL_KEY:
    print("❌ MISTRAL_KEY не задан")
else:
    init_db()
    load_banned()
    print(f"✅ {BOT_NAME} успешно запущен!")
    
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=25)
        except Exception as e:
            print(f"[polling] Ошибка: {e} — перезапуск через 5 сек.")
            time.sleep(5)
