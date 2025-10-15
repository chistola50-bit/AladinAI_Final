import os, time, logging, threading, asyncio
from flask import Flask, request, render_template, redirect, url_for, abort, flash
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pathlib import Path

from database import (
    init_db, add_recipe, get_recipes, get_recipe, like_recipe, get_top_recipes,
    add_comment, add_chat_message, get_chat_messages,
    upsert_user, get_user, get_user_recipes,
    get_or_create_invite, use_invite
)
from utils import generate_caption


# ---------------- ИНИЦИАЛИЗАЦИЯ ----------------
logging.basicConfig(level=logging.INFO)
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET", "cooknet_secret")

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

BACKEND_URL = (os.getenv("COOKNET_URL") or "https://aladinai-final.onrender.com").strip()
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = BACKEND_URL.rstrip("/") + WEBHOOK_PATH

# --- инициализация базы данных ---
def ensure_db_initialized():
    """Создает базу и тестовые рецепты, если базы нет"""
    db_file = Path("cooknet.db")
    first_time = not db_file.exists()
    init_db()
    if first_time:
        add_recipe("andrey", "Борщ по-домашнему", "Ароматный борщ с говядиной и свёклой",
                   None, "https://images.unsplash.com/photo-1604908176997-1e488c60aee9",
                   "Любимый борщ от бабушки ❤️")
        add_recipe("anna", "Сырники", "Пышные творожные сырники с ванилью",
                   None, "https://images.unsplash.com/photo-1625944079467-3d09330cdd52",
                   "Лучшее утро начинается с сырников ☕")
        print("✅ Database initialized and sample recipes added!")

ensure_db_initialized()


# --- Инициализация Telegram-бота ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

user_last, ip_last = {}, {}
SPAM_DELAY = 3


# ---------------- АНТИСПАМ ----------------
def is_spam(uid: int) -> bool:
    now = time.time()
    last = user_last.get(uid, 0)
    if now - last < SPAM_DELAY:
        return True
    user_last[uid] = now
    return False


def is_ip_spam(ip: str) -> bool:
    now = time.time()
    last = ip_last.get(ip, 0)
    if now - last < 2:
        return True
    ip_last[ip] = now
    return False


# ---------------- FSM для добавления рецепта через бота ----------------
class AddRecipeFSM(StatesGroup):
    photo = State()
    title = State()
    desc = State()


# ---------------- Клавиатура ----------------
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


# ---------------- ОБРАБОТЧИКИ БОТА ----------------
@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    upsert_user(message.from_user.id, message.from_user.username or f"id{message.from_user.id}")
    await message.answer("👋 Привет! Это CookNet AI — делись рецептами и вдохновляйся 🍳", reply_markup=main_kb())


@dp.callback_query_handler(lambda c: c.data == "invite")
async def cb_invite(call: types.CallbackQuery):
    u = call.from_user.username or f"id{call.from_user.id}"
    code = get_or_create_invite(u)
    link = BACKEND_URL.rstrip("/") + f"/join/{code}"
    await call.message.answer(f"🤝 Твоя инвайт-ссылка:\n{link}\nПоделись с другом!")


@dp.callback_query_handler(lambda c: c.data == "top")
async def cb_top(call: types.CallbackQuery):
    if is_spam(call.from_user.id):
        await call.answer("⏳ Подожди немного…", show_alert=True); return
    top = get_top_recipes(limit=5)
    if not top:
        await call.message.answer("Пока пусто. Добавь свой первый рецепт!")
        return
    for r in top:
        caption = f"🍽 {r['title']}\n👤 @{r['username']}\n❤️ {r['likes']}\n\n{r['ai_caption'] or r['description']}"
        if r.get("photo_id"):
            try:
                await bot.send_photo(call.message.chat.id, r['photo_id'], caption=caption)
            except Exception:
                await bot.send_message(call.message.chat.id, caption)
        else:
            await bot.send_message(call.message.chat.id, caption)


# ---------------- WEB ----------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/recipes")
def recipes_page():
    return render_template("recipes.html", recipes=get_recipes(limit=50))


@app.route("/recipe/<int:rid>")
def recipe_page(rid):
    r = get_recipe(rid)
    if not r: abort(404)
    return render_template("recipe.html", r=r)


@app.route("/like/<int:rid>", methods=["POST"])
def like_route(rid):
    if is_ip_spam(request.remote_addr):
        return redirect(request.referrer or url_for("recipes_page"))
    like_recipe(rid)
    return redirect(request.referrer or url_for("recipes_page"))


@app.route("/chat", methods=["GET", "POST"])
def chat_page():
    if request.method == "POST":
        if is_ip_spam(request.remote_addr): return redirect(url_for("chat_page"))
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


# ---------------- ДОБАВЛЕНИЕ РЕЦЕПТА ----------------
@app.route("/add", methods=["GET", "POST"])
def add_recipe_page():
    if request.method == "POST":
        if (request.form.get("captcha") or "").strip() != "5":
            flash("❌ Неверный ответ на вопрос (2+3). Попробуйте снова.")
            return redirect(url_for("add_recipe_page"))

        username = (request.form.get("username") or "webuser").strip()[:32]
        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip()
        photo_url = (request.form.get("photo_url") or "").strip()
        video_url = (request.form.get("video_url") or "").strip()

        if not title or not description:
            flash("❗ Заполните все обязательные поля.")
            return redirect(url_for("add_recipe_page"))

        ai_caption = f"✨ {title}: {description[:60]}..."
        add_recipe(username, title, description, None, photo_url, ai_caption)
        flash("✅ Рецепт успешно добавлен!")
        return redirect(url_for("recipes_page"))

    return render_template("add.html")


# ---------------- ПРОФИЛИ ----------------
@app.route("/u/<username>")
def user_page(username):
    u = get_user(username)
    recs = get_user_recipes(username, limit=50)
    return render_template("user.html", u=u, recipes=recs, username=username)


# ---------------- ИНВАЙТ ----------------
@app.route("/join/<code>")
def join_via_invite(code):
    owner = use_invite(code)
    if not owner:
        return "<h3>❌ Неверная или устаревшая ссылка.</h3>", 400
    return "<h3>✅ Инвайт активирован! Напишите боту /start, чтобы завершить.</h3>"


# ---------------- ИНИЦИАЛИЗАЦИЯ ДЛЯ RENDER ----------------
@app.route("/init")
def init_data():
    try:
        init_db()
        add_recipe("andrey", "Борщ по-домашнему", "Ароматный борщ с говядиной и свёклой",
                   None, "https://images.unsplash.com/photo-1604908176997-1e488c60aee9",
                   "Любимый борщ от бабушки ❤️")
        add_recipe("anna", "Сырники", "Пышные творожные сырники с ванилью",
                   None, "https://images.unsplash.com/photo-1625944079467-3d09330cdd52",
                   "Лучшее утро начинается с сырников ☕")
        return "<h3>✅ База успешно создана и заполнена тестовыми рецептами!</h3>"
    except Exception as e:
        return f"<h3>❌ Ошибка инициализации: {e}</h3>"


# ---------------- WEBHOOK ----------------
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
        user = None
        if upd.message and upd.message.from_user:
            user = upd.message.from_user
        elif upd.callback_query and upd.callback_query.from_user:
            user = upd.callback_query.from_user
        if user:
            upsert_user(user.id, user.username or f"id{user.id}")
        await dp.process_update(upd)
    except Exception as ex:
        logging.exception(f"Process update error: {ex}")


# ---------------- ЗАПУСК ----------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
