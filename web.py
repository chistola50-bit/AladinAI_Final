import os
import time
import logging
import threading
import asyncio
from flask import Flask, request, render_template, redirect, url_for, abort, flash
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from openai import OpenAI  # ✅ OpenAI SDK

from database import (
    init_db, add_recipe, get_recipes, get_recipe, like_recipe, get_top_recipes,
    add_comment, add_chat_message, get_chat_messages,
    upsert_user, get_user, get_user_recipes,
    get_or_create_invite, use_invite
)
from utils import generate_caption

# =============== НАСТРОЙКА ===============
logging.basicConfig(level=logging.INFO)
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET", "cooknet_secret")

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

BACKEND_URL = (os.getenv("COOKNET_URL") or "https://aladinai-final.onrender.com").strip()
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = BACKEND_URL.rstrip("/") + WEBHOOK_PATH

# Инициализация базы
init_db()

# Создание Telegram-бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

# Подключение OpenAI клиента
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# =============== АНТИСПАМ ===============
user_last = {}
ip_last = {}
SPAM_DELAY = 3
STATE_TIMEOUT = 300

def is_spam(uid: int) -> bool:
    now = time.time()
    if now - user_last.get(uid, 0) < SPAM_DELAY:
        return True
    user_last[uid] = now
    return False

def is_ip_spam(ip: str) -> bool:
    now = time.time()
    if now - ip_last.get(ip, 0) < 2:
        return True
    ip_last[ip] = now
    return False

async def fsm_autoreset(uid, state: FSMContext):
    data = await state.get_data()
    started = data.get("_started_at")
    if started and time.time() - started > STATE_TIMEOUT:
        await state.finish()

# =============== FSM (Добавление рецепта) ===============
class AddRecipeFSM(StatesGroup):
    photo = State()
    title = State()
    desc = State()

def main_kb():
    kb = InlineKeyboardMarkup(row_width=1)
    site_link = BACKEND_URL.rstrip("/") + "/recipes"
    kb.add(
        InlineKeyboardButton("📷 AI-Камера", callback_data="camera"),
        InlineKeyboardButton("➕ Добавить рецепт", callback_data="add"),
        InlineKeyboardButton("🏆 Топ недели", callback_data="top"),
        InlineKeyboardButton("🌐 Открыть сайт", url=site_link)
    )
    return kb

# =============== ХЕНДЛЕРЫ БОТА ===============
@dp.message_handler(commands=['ping'])
async def ping(message: types.Message):
    await message.answer("✅ Бот активен!")

@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    upsert_user(message.from_user.id, message.from_user.username or f"id{message.from_user.id}")
    await message.answer("👋 Привет! Это CookNet AI 🍳\nОтправь фото продуктов — я предложу рецепты!", reply_markup=main_kb())

# --- Новый AI-КАМЕРА ХЕНДЛЕР ---
@dp.callback_query_handler(lambda c: c.data == "camera")
async def camera_info(call: types.CallbackQuery):
    await call.message.answer("📸 Отправь фото ингредиентов — я подскажу, что можно приготовить!")

@dp.message_handler(content_types=['photo'])
async def analyze_photo(message: types.Message):
    """Анализ фото продуктов с помощью OpenAI"""
    if is_spam(message.from_user.id):
        await message.answer("⏳ Подожди немного…")
        return

    file = await bot.get_file(message.photo[-1].file_id)
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
    await message.answer("🔍 Анализирую фото, подожди пару секунд...")

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",  # поддерживает vision
            messages=[
                {
                    "role": "system",
                    "content": "Ты — шеф-повар AI. Определи продукты на фото и предложи 2–3 рецепта из них."
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Посмотри на фото и опиши, что на нём, и предложи рецепты."},
                        {"type": "image_url", "image_url": {"url": file_url}}
                    ]
                }
            ],
            max_tokens=400
        )

        text = response.choices[0].message.content
        await message.answer(f"🍳 {text}", reply_markup=main_kb())

    except Exception as e:
        await message.answer(f"❌ Ошибка при анализе фото: {e}")

# =============== САЙТ (Flask) ===============
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/recipes")
def recipes_page():
    recipes = get_recipes(limit=20)
    return render_template("recipes.html", recipes=recipes)

@app.route("/recipe/<int:rid>")
def recipe_page(rid):
    r = get_recipe(rid)
    if not r:
        abort(404)
    return render_template("recipe.html", r=r)

@app.route("/chat", methods=["GET", "POST"])
def chat_page():
    if request.method == "POST":
        if is_ip_spam(request.remote_addr):
            return redirect(url_for("chat_page"))
        username = (request.form.get("username") or "webuser").strip()[:32]
        text = (request.form.get("text") or "").strip()[:500]
        captcha = (request.form.get("captcha") or "").strip()
        if captcha != "5":
            flash("Неверный ответ. Попробуйте ещё раз.")
        elif text:
            add_chat_message(username, text)
        return redirect(url_for("chat_page"))
    msgs = get_chat_messages(limit=100)
    return render_template("chat.html", msgs=msgs)

# =============== WEBHOOK НАСТРОЙКА ===============
_loop = asyncio.new_event_loop()
def _runner():
    asyncio.set_event_loop(_loop)
    _loop.run_forever()
threading.Thread(target=_runner, daemon=True).start()

async def setup_webhook():
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"✅ Webhook установлен: {WEBHOOK_URL}")

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
        elif upd.callback_query and upd.callback_query.from_user:
            upsert_user(upd.callback_query.from_user.id, upd.callback_query.from_user.username or f"id{upd.callback_query.from_user.id}")
        await dp.process_update(upd)
    except Exception as ex:
        logging.exception(f"Process update error: {ex}")

# =============== RUN ===============
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
