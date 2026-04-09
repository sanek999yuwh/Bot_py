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
    build_prompt, build_summary_prompt, build_table_prompt, THANKS_WORDS, DONATE_REPLY
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
                    msg_count INTEGER DEFAULT 0, joined TEXT, last_active TEXT, donate_shown BOOLEAN DEFAULT FALSE
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
    style=%s,mood=%s,model=%s,msg_count=%s,last_active=%s,donate_shown=%s WHERE uid=%s""",
    (user.get("name"),json.dumps(user.get("history",[])),json.dumps(user.get("facts",[])),
     user.get("summary",""),user.get("style","neutral"),user.get("mood","neutral"),
     user.get("model",DEFAULT_MODEL),user.get("msg_count",0),user.get("last_active",""),
     user.get("donate_shown",False),uid)) 
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
    elif data == "show_help":
        bot.answer_callback_query(call.id)
        cmd_help(call.message)
        return
    bot.answer_callback_query(call.id)

# ===================== ХЭНДЛЕРЫ =====================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid=message.from_user.id; name=message.from_user.first_name or "друг"
    get_user(uid)["name"]=name; save_user(uid)
    greeting = (
        f"\U0001f44b Привет, {name}!\n\n"
        f"Я \u2014 *{BOT_NAME}*, твой умный помощник.\n\n"
        "\U0001f4ac Отвечаю на любые вопросы\n"
        "\U0001f5bc Анализирую картинки\n"
        "\U0001f3a8 Генерирую изображения /image\n"
        "\U0001f324 Показываю погоду /weather\n"
        "\u23f0 Ставлю напоминания\n"
        "\U0001f9e0 Запоминаю тебя\n\n"
        "Используй /help для списка команд"
    )
    send_safe(message.chat.id, greeting, reply_markup=get_reply_kb(uid))
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("\U0001f310 Веб-версия", url=WEB_URL),
        InlineKeyboardButton("\U0001f4d6 Помощь", callback_data="show_help")
    )
    bot.send_message(message.chat.id, "\u26a1 Быстрые действия:", reply_markup=kb)

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

import urllib.parse as _urlparse
import time as _time_mod

# ── /help ──────────────────────────────────────────────
@bot.message_handler(commands=["help"])
def cmd_help(message):
    lines = [
        "\U0001f4d6 *Команды Арка:*\n",
        "/start \u2014 запустить бота",
        "/help \u2014 список команд",
        "/me \u2014 моя статистика",
        "/clear \u2014 очистить историю",
        "/model \u2014 сменить модель ИИ",
        "/image [запрос] \u2014 нарисовать картинку",
        "/weather [город] \u2014 погода",
        "/remind через N мин \u2014 напоминание\n",
        "\U0001f4ce Отправь фото для анализа",
        "\U0001f4dd Кнопка Сжать текст \u2014 суммаризация",
    ]
    send_safe(message.chat.id, "\n".join(lines))

# ── /me ────────────────────────────────────────────────
@bot.message_handler(commands=["me"])
def cmd_me(message):
    uid = message.from_user.id
    user = get_user(uid)
    name = user.get("name") or message.from_user.first_name or "Неизвестно"
    facts = user.get("facts", [])
    model_label = MODELS.get(user.get("model", ""), user.get("model", "?"))
    msgs = user.get("msg_count", 0)
    joined = user.get("joined", "?")
    last = user.get("last_active", "?")
    hist_len = len(user.get("history", []))
    facts_text = "\n\u2022 ".join(facts) if facts else "пока ничего"
    lines = [
        "\U0001f464 *Твой профиль*\n",
        f"Имя: {name}",
        f"Модель: {model_label}",
        f"Сообщений: {msgs}",
        f"В памяти: {hist_len} сообщ.",
        f"С нами с: {joined}",
        f"Последний визит: {last}\n",
        f"\U0001f9e0 *Что я знаю о тебе:*\n\u2022 {facts_text}",
    ]
    send_safe(message.chat.id, "\n".join(lines))

# ── /clear ─────────────────────────────────────────────
@bot.message_handler(commands=["clear"])
def cmd_clear_hist(message):
    uid = message.from_user.id
    get_user(uid)["history"] = []
    save_user(uid)
    bot.send_message(message.chat.id, "\U0001f5d1 История чата очищена!")

# ── /model ─────────────────────────────────────────────
@bot.message_handler(commands=["model"])
def cmd_model_select(message):
    uid = message.from_user.id
    user = get_user(uid)
    bot.send_message(message.chat.id, "\U0001f916 Выбери модель:",
        reply_markup=models_keyboard(user.get("model", DEFAULT_MODEL)))

# ── /image ─────────────────────────────────────────────
@bot.message_handler(commands=["image"])
def cmd_image(message):
    parts = message.text.split(None, 1)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Напиши запрос: /image котик в космосе")
        return
    prompt = parts[1].strip()
    bot.send_chat_action(message.chat.id, "upload_photo")
    msg = bot.send_message(message.chat.id, "\U0001f3a8 Генерирую...")
    try:
        seed = int(_time_mod.time()) % 99999
        enc = _urlparse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{enc}?width=768&height=768&nologo=true&seed={seed}&model=flux"
        r = requests.get(url, timeout=90, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and r.content:
            try:
                bot.delete_message(message.chat.id, msg.message_id)
            except Exception:
                pass
            bot.send_photo(message.chat.id, r.content, caption=f"\U0001f3a8 {prompt[:100]}")
        else:
            bot.edit_message_text("\u26a0\ufe0f Не удалось сгенерировать.", message.chat.id, msg.message_id)
    except Exception as e:
        print(f"[image] {e}")
        try:
            bot.edit_message_text("\u26a0\ufe0f Ошибка генерации.", message.chat.id, msg.message_id)
        except Exception:
            pass

# ── /weather ───────────────────────────────────────────
@bot.message_handler(commands=["weather"])
def cmd_weather(message):
    parts = message.text.split(None, 1)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Укажи город: /weather Москва")
        return
    city = parts[1].strip()
    msg = bot.send_message(message.chat.id, "\U0001f324 Узнаю погоду...")
    try:
        r = requests.get(
            f"https://wttr.in/{_urlparse.quote(city)}?format=3&lang=ru",
            timeout=10, headers={"User-Agent": "curl/7.0"}
        )
        if r.status_code == 200 and r.text.strip():
            bot.edit_message_text(f"\U0001f324 {r.text.strip()}", message.chat.id, msg.message_id)
        else:
            bot.edit_message_text("\u26a0\ufe0f Город не найден.", message.chat.id, msg.message_id)
    except Exception as e:
        print(f"[weather] {e}")
        bot.edit_message_text("\u26a0\ufe0f Ошибка получения погоды.", message.chat.id, msg.message_id)


@bot.message_handler(func=lambda m:True)
def handle_message(message):
    if not message.text: return
    uid=message.from_user.id; text=message.text

    if uid in banned_users:
        bot.send_message(message.chat.id,"⛔ Доступ запрещён.")
        return

    if text=="⚙️ Меню":
        bot.send_message(message.chat.id,"⚙️ Меню:",reply_markup=main_keyboard())
        return

    if text=="👑 Админ панель":
        if uid!=ADMIN_ID:
            bot.send_message(message.chat.id,"⛔ Доступ запрещён.")
            return
        send_safe(message.chat.id,"👑 *Админ-панель:*",reply_markup=admin_keyboard())
        return

    # Проверка на благодарность
    user = get_user(uid)
    if not user.get("donate_shown", False):
        if any(word in text.lower() for word in THANKS_WORDS):
            user["donate_shown"] = True
            save_user(uid)
            send_safe(message.chat.id, DONATE_REPLY)
            return

    if is_dangerous(text):
        bot.send_message(message.chat.id, random.choice(SAFE_REPLIES))
        return

    ok, reason = check_rate(uid)
    if not ok:
        bot.send_message(message.chat.id, reason)
        return

    get_user(uid)["last_active"] = datetime.now().strftime("%d.%m.%Y %H:%M")
    save_user(uid)

    bot.send_chat_action(message.chat.id,"typing")
    threading.Thread(target=ask_ai,args=(uid,text,message.chat.id,"chat"),daemon=True).start()
    return

# ===================== ЗАПУСК =====================
if not TELEGRAM_TOKEN:
    print("❌ TELEGRAM_TOKEN не задан")
elif not MISTRAL_KEY:
    print("❌ MISTRAL_KEY не задан")
else:
    init_db()
    load_banned()
    print(f"✅ {BOT_NAME} запущен!")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=25)
        except Exception as e:
            print(f"[polling] {e} — перезапуск через 5 сек.")
            time.sleep(5)
