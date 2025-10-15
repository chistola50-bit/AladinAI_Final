import sqlite3
import os
from datetime import datetime
import secrets

# ✅ Путь к базе
DB_PATH = "/data/cooknet.db"

# если запускается локально — создаём каталог data/
if not os.path.exists("/data"):
    os.makedirs("data", exist_ok=True)
    DB_PATH = "data/cooknet.db"

def _conn():
    return sqlite3.connect(DB_PATH)

# --- инициализация БД ---
def init_db():
    con = _conn(); cur = con.cursor()

    # таблица рецептов
    cur.execute("""
    CREATE TABLE IF NOT EXISTS recipes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        title TEXT,
        description TEXT,
        photo_id TEXT,
        photo_url TEXT,
        video_url TEXT,        -- ✅ новое поле
        ai_caption TEXT,
        likes INTEGER DEFAULT 0,
        created_at TEXT
    )
    """)

    # комментарии
    cur.execute("""
    CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recipe_id INTEGER,
        username TEXT,
        text TEXT,
        created_at TEXT
    )
    """)

    # чат
    cur.execute("""
    CREATE TABLE IF NOT EXISTS chat (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        text TEXT,
        created_at TEXT
    )
    """)

    # пользователи
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER,
        username TEXT UNIQUE,
        joined_at TEXT,
        invited_by TEXT DEFAULT NULL
    )
    """)

    # инвайты
    cur.execute("""
    CREATE TABLE IF NOT EXISTS invites (
        code TEXT PRIMARY KEY,
        owner TEXT,
        uses INTEGER DEFAULT 0,
        created_at TEXT
    )
    """)

    con.commit()
    con.close()

# --- рецепты ---
def add_recipe(username, title, description, photo_id=None, photo_url=None, ai_caption=None, video_url=None):
    """Добавляет рецепт. Автоматически добавляет колонку video_url, если её нет."""
    con = _conn(); cur = con.cursor()
    # если старая база — добавляем колонку
    try:
        cur.execute("ALTER TABLE recipes ADD COLUMN video_url TEXT")
        con.commit()
    except Exception:
        pass  # колонка уже есть
    cur.execute("""
        INSERT INTO recipes (username,title,description,photo_id,photo_url,ai_caption,video_url,likes,created_at)
        VALUES (?,?,?,?,?,?,?,0,?)
    """, (username, title, description, photo_id, photo_url, ai_caption, video_url, datetime.utcnow().isoformat()))
    con.commit(); con.close()

def get_recipes(limit=50):
    con = _conn(); cur = con.cursor()
    cur.execute("""
        SELECT id,username,title,description,photo_id,photo_url,video_url,ai_caption,likes,created_at
        FROM recipes ORDER BY id DESC LIMIT ?
    """, (limit,))
    rows = cur.fetchall(); con.close()
    keys = ["id","username","title","description","photo_id","photo_url","video_url","ai_caption","likes","created_at"]
    return [dict(zip(keys,r)) for r in rows]

def get_recipe(rid:int):
    con = _conn(); cur = con.cursor()
    cur.execute("""
        SELECT id,username,title,description,photo_id,photo_url,video_url,ai_caption,likes,created_at
        FROM recipes WHERE id=?
    """, (rid,))
    r = cur.fetchone()
    if not r:
        con.close(); return None
    keys = ["id","username","title","description","photo_id","photo_url","video_url","ai_caption","likes","created_at"]
    d = dict(zip(keys,r))
    cur.execute("""
        SELECT username,text,created_at FROM comments
        WHERE recipe_id=? ORDER BY id DESC LIMIT 50
    """, (rid,))
    d["comments"] = [{"username":u,"text":t,"created_at":ts} for (u,t,ts) in cur.fetchall()]
    con.close()
    return d

def like_recipe(rid:int):
    con = _conn(); cur = con.cursor()
    cur.execute("UPDATE recipes SET likes=COALESCE(likes,0)+1 WHERE id=?", (rid,))
    con.commit(); con.close()

def get_top_recipes(limit=10):
    con = _conn(); cur = con.cursor()
    cur.execute("""
        SELECT id,username,title,description,photo_id,photo_url,video_url,ai_caption,likes,created_at
        FROM recipes ORDER BY likes DESC, id DESC LIMIT ?
    """, (limit,))
    rows = cur.fetchall(); con.close()
    keys = ["id","username","title","description","photo_id","photo_url","video_url","ai_caption","likes","created_at"]
    return [dict(zip(keys,r)) for r in rows]

# --- комментарии ---
def add_comment(recipe_id:int, username:str, text:str):
    con = _conn(); cur = con.cursor()
    cur.execute("""
        INSERT INTO comments (recipe_id,username,text,created_at)
        VALUES (?,?,?,?)
    """, (recipe_id, username, text, datetime.utcnow().isoformat()))
    con.commit(); con.close()

# --- чат ---
def add_chat_message(username:str, text:str):
    con = _conn(); cur = con.cursor()
    cur.execute("""
        INSERT INTO chat (username,text,created_at)
        VALUES (?,?,?)
    """, (username, text, datetime.utcnow().isoformat()))
    con.commit(); con.close()

def get_chat_messages(limit=100):
    con = _conn(); cur = con.cursor()
    cur.execute("SELECT username,text,created_at FROM chat ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall(); con.close()
    return [{"username":u,"text":t,"created_at":ts} for (u,t,ts) in rows]

# --- пользователи ---
def upsert_user(telegram_id:int, username:str, invited_by:str|None=None):
    con = _conn(); cur = con.cursor()
    cur.execute("SELECT username FROM users WHERE username=?", (username,))
    if cur.fetchone():
        con.close(); return
    cur.execute("""
        INSERT INTO users (telegram_id,username,joined_at,invited_by)
        VALUES (?,?,?,?)
    """, (telegram_id, username, datetime.utcnow().isoformat(), invited_by))
    con.commit(); con.close()

def get_user(username:str):
    con = _conn(); cur = con.cursor()
    cur.execute("SELECT username,joined_at,invited_by FROM users WHERE username=?", (username,))
    row = cur.fetchone(); con.close()
    if not row: return None
    return {"username": row[0], "joined_at": row[1], "invited_by": row[2]}

def get_user_recipes(username:str, limit=50):
    con = _conn(); cur = con.cursor()
    cur.execute("""
        SELECT id,title,likes,created_at FROM recipes
        WHERE username=? ORDER BY id DESC LIMIT ?
    """, (username, limit))
    rows = cur.fetchall(); con.close()
    return [{"id":r[0],"title":r[1],"likes":r[2],"created_at":r[3]} for r in rows]

# --- инвайты ---
def get_or_create_invite(owner:str) -> str:
    con = _conn(); cur = con.cursor()
    cur.execute("SELECT code FROM invites WHERE owner=?", (owner,))
    row = cur.fetchone()
    if row:
        code = row[0]; con.close(); return code
    code = secrets.token_urlsafe(6)
    cur.execute("""
        INSERT INTO invites (code,owner,uses,created_at)
        VALUES (?,?,0,?)
    """, (code, owner, datetime.utcnow().isoformat()))
    con.commit(); con.close()
    return code

def use_invite(code:str) -> str | None:
    con = _conn(); cur = con.cursor()
    cur.execute("SELECT owner FROM invites WHERE code=?", (code,))
    row = cur.fetchone()
    if not row:
        con.close(); return None
    owner = row[0]
    cur.execute("UPDATE invites SET uses=uses+1 WHERE code=?", (code,))
    con.commit(); con.close()
    return owner
