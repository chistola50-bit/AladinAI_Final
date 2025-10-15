import os, time, logging, threading, asyncio
from flask import Flask, request, render_template, redirect, url_for, abort, flash
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import (
    init_db, add_recipe, get_recipes, get_recipe, like_recipe, get_top_recipes,
    add_comment, add_chat_message, get_chat_messages,
    upsert_user, get_user, get_user_recipes,
    get_or_create_invite, use_invite
)
from utils import generate_caption
from pathlib import Path

# --- init database automatically ---
def ensure_db_initialized():
    """Проверяет, создана ли база, и добавляет тестовые рецепты при первом запуске."""
    db_file = Path("cooknet.db")
    first_time = not db_file.exists()
    init_db()
    if first_time:
        from database import add_recipe
        add_recipe("andrey", "Борщ по-домашнему", "Ароматный борщ с говядиной и свёклой",
                   None, "https://picsum.photos/400", "Любимый борщ от бабушки")
        add_recipe("anna", "Сырники", "Пышные творожные сырники с ванилью",
                   None, "https://picsum.photos/401", "Лучшее утро начинается с сырников ☕")
        print("✅ Database initialized and sample recipes added!")

ensure_db_initialized()


logging.basicConfig(level=logging.INFO)
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET", "cooknet_secret")

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

# правильный домен (или из переменной окружения)
BACKEND_URL = (os.getenv("COOKNET_URL") or "https://aladinai-final.onrender.com").strip()
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = BACKEND_URL.rstrip("/") + WEBHOOK_PATH

# --- init ---
init_db()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

# --- anti-spam (bot + web) ---
user_last, ip_last = {}, {}
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
@dp.message_handler(commands=['ping'])
async def ping(message: types.Message):
    await message.answer("✅ Бот активен!")

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

@dp.callback_query_handler(lambda c: c.data == "add")
async def cb_add(call: types.CallbackQuery, state: FSMContext):
    if is_spam(call.from_user.id):
        await call.answer("⏳ Чуть позже…", show_alert=True); return
    await call.answer()
    await state.finish()
    await AddRecipeFSM.photo.set()
    await state.update_data(_started_at=time.time())
    await bot.send_message(call.message.chat.id, "📸 Пришли фото блюда.\nОтмена: /cancel")

@dp.message_handler(commands=['cancel'], state='*')
async def cancel(message: types.Message, state: FSMContext):
    await state.finish()
    await message.answer("❌ Отменено.", reply_markup=main_kb())

@dp.message_handler(content_types=['photo'], state=AddRecipeFSM.photo)
async def fsm_photo(message: types.Message, state: FSMContext):
    await fsm_autoreset(message.from_user.id, state)
    if is_spam(message.from_user.id): return
    fid = message.photo[-1].file_id
    photo_url = None
    try:
        f = await bot.get_file(fid)
        photo_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}"
    except Exception:
        pass
    await state.update_data(photo_id=fid, photo_url=photo_url, _started_at=time.time())
    await AddRecipeFSM.next()
    await message.answer("🍽 Введи название:")

@dp.message_handler(lambda m: not m.photo, state=AddRecipeFSM.photo, content_types=types.ContentTypes.ANY)
async def require_photo(message: types.Message):
    await message.answer("Нужно фото 📷. Отправь фото или /cancel")

@dp.message_handler(state=AddRecipeFSM.title)
async def fsm_title(message: types.Message, state: FSMContext):
    await fsm_autoreset(message.from_user.id, state)
    title = (message.text or "").strip()
    if not title:
        await message.answer("Название не может быть пустым.")
        return
    await state.update_data(title=title, _started_at=time.time())
    await AddRecipeFSM.next()
    await message.answer("✍️ Короткое описание:")

@dp.message_handler(state=AddRecipeFSM.desc)
async def fsm_desc(message: types.Message, state: FSMContext):
    await fsm_autoreset(message.from_user.id, state)
    if is_spam(message.from_user.id): return
    data = await state.get_data()
    title = data.get("title")
    description = (message.text or "").strip()
    photo_id = data.get("photo_id")
    photo_url = data.get("photo_url")
    ai_caption = generate_caption(title, description)
    add_recipe(
        username=message.from_user.username or "anon",
        title=title,
        description=description,
        photo_id=photo_id,
        photo_url=photo_url,
        ai_caption=ai_caption
    )
    await message.answer(f"✅ Сохранено!\n✨ {ai_caption}", reply_markup=main_kb())
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == "top")
async def cb_top(call: types.CallbackQuery):
    if is_spam(call.from_user.id):
        await call.answer("⏳ Чуть позже…", show_alert=True); return
    top = get_top_recipes(limit=5)
    if not top:
        await call.message.answer("Пока пусто. Нажми «Добавить рецепт».")
        return
    for r in top:
        caption = f"🍽 {r['title']}\n👤 @{r['username']}\n❤️ {r['likes']}\n\n{(r['ai_caption'] or r['description'] or '')[:200]}"
        if r.get("photo_id"):
            try:
                await bot.send_photo(call.message.chat.id, r['photo_id'], caption=caption)
            except Exception:
                await bot.send_message(call.message.chat.id, caption)
        else:
            await bot.send_message(call.message.chat.id, caption)

# --------- WEB PAGES ----------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/recipes")
def recipes_page():
    return render_template("recipes.html", recipes=get_recipes(limit=60))

@app.route("/recipe/<int:rid>")
def recipe_page(rid):
    r = get_recipe(rid)
    if not r: abort(404)
    return render_template("recipe.html", r=r)

@app.route("/top")
def top_page():
    top_recipes = get_top_recipes(limit=20)
    return render_template("top.html", recipes=top_recipes)

@app.post("/like/<int:rid>")
def like_route(rid):
    if is_ip_spam(request.remote_addr): return redirect(request.referrer or url_for("recipes_page"))
    like_recipe(rid)
    return redirect(request.referrer or url_for("recipes_page"))

@app.post("/comment/<int:rid>")
def comment_route(rid):
    if is_ip_spam(request.remote_addr): return redirect(url_for("recipe_page", rid=rid))
    username = (request.form.get("username") or "webuser").strip()[:32]
    text = (request.form.get("text") or "").strip()[:500]
    captcha = (request.form.get("captcha") or "").strip()
    if captcha != "5":
        flash("Неверный ответ на вопрос. Попробуйте ещё раз.")
        return redirect(url_for("recipe_page", rid=rid))
    if text:
        add_comment(rid, username, text)
    return redirect(url_for("recipe_page", rid=rid))

@app.route("/u/<username>")
def user_page(username):
    u = get_user(username)
    recs = get_user_recipes(username, limit=50)
    return render_template("user.html", u=u, recipes=recs, username=username)

@app.route("/chat", methods=["GET","POST"])
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

@app.route("/join/<code>")
def join_via_invite(code):
    owner = use_invite(code)
    if not owner:
        return "<h3>❌ Неверная или устаревшая ссылка.</h3>", 400
    return "<h3>✅ Инвайт активирован! Напишите боту /start, чтобы завершить.</h3>"

# --- healthcheck для Render
@app.route("/health")
def health():
    return "OK", 200

# --------- DB INIT (разовая инициализация, защищена секретом) ----------
@app.route("/init")
def init_data():
    # Чтобы посторонние не дергали /init — можно задать INIT_SECRET в переменных Render
    need_secret = os.getenv("INIT_SECRET")
    secret_qs = request.args.get("s")
    if need_secret and secret_qs != need_secret:
        return "<h3>⛔ Доступ запрещён</h3>", 403

    try:
        # если база уже не пустая — ничего не делаем
        if get_recipes(limit=1):
            return "<h3>ℹ️ База уже инициализирована.</h3>"

        init_db()
        # стабильные картинки (борщ и сырники), не рандомные
        add_recipe(
            "andrey", "Борщ по-домашнему",
            "Ароматный борщ с говядиной и свёклой",
            None,
            "https://upload.wikimedia.org/wikipedia/commons/5/5c/Borscht_served.jpg",
            "Любимый борщ от бабушки"
        )
        add_recipe(
            "anna", "Сырники",
            "Пышные творожные сырники с ванилью",
            None,
            "https://upload.wikimedia.org/wikipedia/commons/4/47/Syrnyky.jpg",
            "Лучшее утро начинается с сырников ☕"
        )
        return redirect(url_for("recipes_page"))
    except Exception as e:
        logging.exception("Init error")
        return f"<h3>❌ Ошибка инициализации базы: {e}</h3>", 500

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
        logging.exception(e); return "FAIL", 500

async def _process_update(data):
    try:
        upd = types.Update(**data)
        from aiogram import Bot, Dispatcher
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

# --------- RUN ----------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
