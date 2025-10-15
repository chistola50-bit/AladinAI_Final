import os, time, logging, threading, asyncio, base64
from flask import Flask, request, render_template, redirect, url_for, abort, flash
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import openai

from database import (
    init_db, add_recipe, get_recipes, get_recipe, like_recipe, get_top_recipes,
    add_comment, add_chat_message, get_chat_messages,
    upsert_user, get_user, get_user_recipes,
    get_or_create_invite, use_invite
)
from utils import generate_caption
from pathlib import Path

logging.basicConfig(level=logging.INFO)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET", "cooknet_secret")

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

BACKEND_URL = (os.getenv("COOKNET_URL") or "https://aladinai-final.onrender.com").strip()
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = BACKEND_URL.rstrip("/") + WEBHOOK_PATH

# --- init ---
def ensure_db_initialized():
    """Проверяет, создана ли база, и добавляет примеры при первом запуске."""
    db_file = Path("/data/cooknet.db")
    first_time = not db_file.exists()
    init_db()
    if first_time:
        add_recipe("andrey", "Борщ по-домашнему", "Ароматный борщ с говядиной и свёклой",
                   None, "https://picsum.photos/400", "Любимый борщ от бабушки")
        add_recipe("anna", "Сырники", "Пышные творожные сырники с ванилью",
                   None, "https://picsum.photos/401", "Лучшее утро начинается с сырников ☕")
        print("✅ Database initialized and sample recipes added!")

ensure_db_initialized()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

# --- anti-spam ---
user_last = {}
ip_last = {}
SPAM_DELAY = 3
STATE_TIMEOUT = 300

def is_spam(uid:int)->bool:
    now = time.time()
    last = user_last.get(uid, 0)
    if now - last < SPAM_DELAY:
        return True
    user_last[uid] = now
    return False

def is_ip_spam(ip:str)->bool:
    now = time.time()
    last = ip_last.get(ip, 0)
    if now - last < 2:
        return True
    ip_last[ip] = now
    return False

async def fsm_autoreset(uid, state: FSMContext):
    data = await state.get_data()
    started = data.get("_started_at")
    if started and time.time() - started > STATE_TIMEOUT:
        await state.finish()

# --- FSM add recipe ---
class AddRecipeFSM(StatesGroup):
    photo = State()
    title = State()
    desc = State()

def main_kb():
    kb = InlineKeyboardMarkup(row_width=1)
    site_link = BACKEND_URL.rstrip("/") + "/recipes"
    kb.add(
        InlineKeyboardButton("➕ Добавить рецепт", callback_data="add"),
        InlineKeyboardButton("🏆 Топ недели", callback_data="top"),
        InlineKeyboardButton("🌐 Открыть сайт", url=site_link),
        InlineKeyboardButton("🤝 Инвайт", callback_data="invite")
    )
    return kb

# --------- BOT HANDLERS ----------
@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    upsert_user(message.from_user.id, message.from_user.username or f"id{message.from_user.id}")
    await message.answer("👋 Привет! Это CookNet AI — делись рецептами и вдохновляйся 🍳", reply_markup=main_kb())

@dp.callback_query_handler(lambda c: c.data == "add")
async def cb_add(call: types.CallbackQuery, state: FSMContext):
    await state.finish()
    await AddRecipeFSM.photo.set()
    await state.update_data(_started_at=time.time())
    await bot.send_message(call.message.chat.id, "📸 Пришли фото блюда.\nОтмена: /cancel")

@dp.message_handler(state=AddRecipeFSM.photo, content_types=['photo'])
async def fsm_photo(message: types.Message, state: FSMContext):
    fid = message.photo[-1].file_id
    photo_url = None
    try:
        f = await bot.get_file(fid)
        photo_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}"
    except Exception:
        pass
    await state.update_data(photo_id=fid, photo_url=photo_url)
    await AddRecipeFSM.next()
    await message.answer("🍽 Введи название:")

@dp.message_handler(state=AddRecipeFSM.title)
async def fsm_title(message: types.Message, state: FSMContext):
    title = (message.text or "").strip()
    await state.update_data(title=title)
    await AddRecipeFSM.next()
    await message.answer("✍️ Короткое описание:")

@dp.message_handler(state=AddRecipeFSM.desc)
async def fsm_desc(message: types.Message, state: FSMContext):
    data = await state.get_data()
    ai_caption = generate_caption(data["title"], message.text)
    add_recipe(
        username=message.from_user.username or "anon",
        title=data["title"],
        description=message.text,
        photo_id=data.get("photo_id"),
        photo_url=data.get("photo_url"),
        ai_caption=ai_caption
    )
    await message.answer(f"✅ Сохранено!\n✨ {ai_caption}", reply_markup=main_kb())
    await state.finish()

# --------- WEB ROUTES ----------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/recipes")
def recipes_page():
    return render_template("recipes.html", recipes=get_recipes(limit=12))

@app.route("/recipe/<int:rid>")
def recipe_page(rid):
    r = get_recipe(rid)
    if not r: abort(404)
    return render_template("recipe.html", r=r)

@app.route("/u/<username>")
def user_page(username):
    u = get_user(username)
    recs = get_user_recipes(username, limit=20)
    return render_template("user.html", u=u, recipes=recs, username=username)

@app.route("/chat", methods=["GET","POST"])
def chat_page():
    if request.method == "POST":
        username = (request.form.get("username") or "webuser").strip()[:32]
        text = (request.form.get("text") or "").strip()[:500]
        if text:
            add_chat_message(username, text)
        return redirect(url_for("chat_page"))
    msgs = get_chat_messages(limit=50)
    return render_template("chat.html", msgs=msgs)

# ===== 📸 AI-Камера =====
@app.route("/analyze", methods=["GET", "POST"])
def analyze_photo():
    if request.method == "GET":
        return render_template("analyze.html")

    if "photo" not in request.files:
        flash("Загрузите фото, пожалуйста.")
        return redirect(url_for("analyze_photo"))

    photo = request.files["photo"]
    if photo.filename == "":
        flash("Файл не выбран.")
        return redirect(url_for("analyze_photo"))

    tmp_path = f"/tmp/{photo.filename}"
    photo.save(tmp_path)

    try:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        with open(tmp_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты шеф-повар. Определи ингредиенты на фото и предложи 3 рецепта."},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": f"data:image/jpeg;base64,{img_b64}"}
                ]}
            ]
        )

        result_text = response.choices[0].message["content"]
        return render_template("analyze.html", result=result_text)

    except Exception as e:
        flash(f"Ошибка при анализе: {e}")
        return redirect(url_for("analyze_photo"))

# --------- WEBHOOK ----------
_loop = asyncio.new_event_loop()
def _runner():
    asyncio.set_event_loop(_loop)
    _loop.run_forever()
threading.Thread(target=_runner, daemon=True).start()

async def setup_webhook():
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"✅ Webhook: {WEBHOOK_URL}")

asyncio.run_coroutine_threadsafe(setup_webhook(), _loop)

@app.post(f"{WEBHOOK_PATH}")
def telegram_webhook():
    try:
        data = request.get_json(force=True)
        asyncio.run_coroutine_threadsafe(_process_update(data), _loop)
        return "OK", 200
    except Exception as e:
        logging.exception(e)
        return "FAIL", 500

async def _process_update(data):
    try:
        upd = types.Update(**data)
        Bot.set_current(bot)
        Dispatcher.set_current(dp)
        if upd.message and upd.message.from_user:
            upsert_user(upd.message.from_user.id, upd.message.from_user.username or f"id{upd.message.from_user.id}")
        await dp.process_update(upd)
    except Exception as ex:
        logging.exception(f"Process update error: {ex}")

# --------- RUN ----------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
