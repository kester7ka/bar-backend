import os
import threading
from datetime import datetime, timedelta, timezone
import sqlite3
from flask import Flask, request, jsonify
from flask_cors import CORS
from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import hmac
import hashlib
import time
import json
from functools import wraps

load_dotenv()

# DEBUG: Вывести текущий API_SECRET при запуске
API_SECRET = os.getenv('API_SECRET', 'supersecretkey')
print('=== API_SECRET (Flask) ===')
print(repr(API_SECRET))
print('==========================')

SQLITE_DB = os.getenv("SQLITE_DB", "your_bot_db.sqlite")
USERS_TABLE = 'users'
INVITES_TABLE = 'invites'
BARS = ['АВОШ59', 'АВПМ97', 'АВЯР01', 'АВКОСМ04', 'АВКО04', 'АВДШ02', 'АВКШ78', 'АВПМ58', 'АВЛБ96']
CATEGORIES = ["🍯 Сиропы", "🥕 Ингредиенты", "☕ Кофе", "📦 Прочее"]
REG_WAIT_CODE = 0
UPLOAD_BACKUP_WAIT_FILE = 100  # новое состояние для загрузки бэкапа
RESTORE_BACKUP_WAIT_FILE = 101  # состояние для восстановления базы

app = Flask(__name__)
CORS(
    app,
    origins=[
        "https://kester7ka.github.io",
        "https://kester7ka.github.io/my-bar-site"
    ],
    allow_headers="*",
    methods=["GET", "POST", "OPTIONS"],
    expose_headers="*"
)

MSK_TZ = timezone(timedelta(hours=3))

TELEGRAM_ADMIN_ID = 1209688883  # твой user_id
DB_FILENAME = SQLITE_DB
BOT_TOKEN = os.getenv('BOT_TOKEN')

last_backup_time = None  # глобальная переменная для хранения времени последнего бэкапа

def verify_hmac(payload, timestamp, hmac_to_check):
    # Проверка времени (2 минуты)
    now = int(time.time())
    try:
        ts = int(timestamp)
    except Exception:
        return False
    if abs(now - ts) > 120:
        return False
    msg = payload + str(timestamp)
    expected = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, hmac_to_check)

def require_hmac(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        data = request.get_json()
        payload = data.get('payload')
        timestamp = data.get('timestamp')
        hmac_val = data.get('hmac')
        if not (payload and timestamp and hmac_val):
            return jsonify(ok=False, error='HMAC required'), 401
        if not verify_hmac(payload, timestamp, hmac_val):
            return jsonify(ok=False, error='Invalid HMAC'), 401
        # Подменяем request.json на распарсенный payload
        request._cached_json = json.loads(payload)
        return f(*args, **kwargs)
    return wrapper

def ensure_bar_table(bar_name):
    if bar_name not in BARS:
        raise Exception("Неизвестный бар")
    with sqlite3.connect(SQLITE_DB) as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {bar_name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            tob TEXT,
            name TEXT,
            manufactured_at TEXT,
            shelf_life_days INTEGER,
            opened_at TEXT,
            opened_shelf_life_days INTEGER,
            opened INTEGER DEFAULT 0
        )
        """)
        conn.commit()

def migrate_all_bars():
    for bar in BARS:
        ensure_bar_table(bar)

def db_query(sql, params=(), fetch=False):
    try:
        if not os.path.exists(SQLITE_DB):
            raise Exception(f"Файл базы не найден: {SQLITE_DB}")
        with sqlite3.connect(SQLITE_DB) as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            if fetch:
                return cursor.fetchall()
            conn.commit()
            return None
    except Exception as e:
        raise

def get_user_bar(user_id):
    try:
        res = db_query(f"SELECT bar_name FROM {USERS_TABLE} WHERE user_id=?", (user_id,), fetch=True)
        return res[0][0] if res else None
    except Exception as e:
        return None

def check_user_access(user_id):
    try:
        return get_user_bar(user_id) is not None
    except Exception as e:
        return False

def get_bar_table(user_id):
    bar_name = get_user_bar(user_id)
    if bar_name in BARS:
        ensure_bar_table(bar_name)
        return bar_name
    return None

def msk_now():
    return datetime.now(MSK_TZ)

def msk_today_str():
    return msk_now().strftime('%Y-%m-%d')

def calc_expiry_by_total(manufactured_at, shelf_life_days):
    if not manufactured_at or not shelf_life_days:
        return None
    try:
        return (datetime.strptime(manufactured_at, '%Y-%m-%d') + timedelta(days=int(shelf_life_days))).strftime('%Y-%m-%d')
    except:
        return None

def calc_expiry_by_opened(opened_at, opened_shelf_life_days):
    if not opened_at or not opened_shelf_life_days:
        return None
    try:
        return (datetime.strptime(opened_at, '%Y-%m-%d') + timedelta(days=int(opened_shelf_life_days))).strftime('%Y-%m-%d')
    except:
        return None

def min_date(date1, date2):
    if date1 and date2:
        return min(date1, date2)
    return date1 or date2

@app.route('/userinfo', methods=['POST'])
@require_hmac
def api_userinfo():
    data = request.get_json()
    user_id = data.get('user_id')
    try:
        if not user_id:
            return jsonify(ok=False, error="Нет user_id")
        res = db_query(f"SELECT username, bar_name FROM {USERS_TABLE} WHERE user_id=?", (user_id,), fetch=True)
        if res:
            username, bar_name = res[0]
            return jsonify(ok=True, username=username, bar_name=bar_name)
        return jsonify(ok=False, error="Пользователь не найден")
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route('/add', methods=['POST'])
@require_hmac
def api_add():
    data = request.get_json()
    user_id = data.get('user_id')
    try:
        if not user_id or not check_user_access(user_id):
            return jsonify(ok=False, error="Нет доступа")
        bar_table = get_bar_table(user_id)
        d = data
        # обязательные поля
        manufactured_at = d['manufactured_at']
        shelf_life_days = int(d['shelf_life_days'])
        opened = int(d.get('opened', 0))
        opened_at = d.get('opened_at')
        opened_shelf_life_days = d.get('opened_shelf_life_days')
        if opened_shelf_life_days is not None:
            opened_shelf_life_days = int(opened_shelf_life_days)
        with sqlite3.connect(SQLITE_DB) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"INSERT INTO {bar_table} (category, tob, name, manufactured_at, shelf_life_days, opened_at, opened_shelf_life_days, opened) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    d['category'],
                    d['tob'],
                    d['name'],
                    manufactured_at,
                    shelf_life_days,
                    opened_at,
                    opened_shelf_life_days,
                    opened
                )
            )
            new_id = cursor.lastrowid
            conn.commit()
        return jsonify(ok=True, id=new_id)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route('/open', methods=['POST'])
@require_hmac
def api_open():
    data = request.get_json()
    user_id = data.get('user_id')
    try:
        if not user_id or not check_user_access(user_id):
            return jsonify(ok=False, error="Нет доступа")
        bar_table = get_bar_table(user_id)
        tob = data['tob']
        category = data['category']
        name = data['name']
        today = msk_today_str()
        res = db_query(f"SELECT id, shelf_life_days FROM {bar_table} WHERE tob=? AND opened=1", (tob,), fetch=True)
        if res:
            old_id, shelf_life_days = res[0]
            db_query(f"UPDATE {bar_table} SET opened=0 WHERE id=?", (old_id,))
            expiry_at = (msk_now() + timedelta(days=int(shelf_life_days))).strftime('%Y-%m-%d')
            with sqlite3.connect(SQLITE_DB) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"INSERT INTO {bar_table} (category, tob, name, opened_at, shelf_life_days, expiry_at, opened) VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (category, tob, name, today, shelf_life_days, expiry_at)
                )
                new_id = cursor.lastrowid
                conn.commit()
            return jsonify(ok=True, replaced=True, id=new_id)
        else:
            shelf_life_days = int(data['shelf_life_days'])
            expiry_at = (msk_now() + timedelta(days=shelf_life_days)).strftime('%Y-%m-%d')
            with sqlite3.connect(SQLITE_DB) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"INSERT INTO {bar_table} (category, tob, name, opened_at, shelf_life_days, expiry_at, opened) VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (category, tob, name, today, shelf_life_days, expiry_at)
                )
                new_id = cursor.lastrowid
                conn.commit()
            return jsonify(ok=True, replaced=False, id=new_id)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route('/expired', methods=['POST'])
@require_hmac
def api_expired():
    data = request.get_json()
    user_id = data.get('user_id')
    try:
        if not user_id or not check_user_access(user_id):
            return jsonify(ok=False, error="Нет доступа")
        bar_table = get_bar_table(user_id)
        now = msk_today_str()
        select_columns = "id, category, tob, name, manufactured_at, shelf_life_days, opened_at, opened_shelf_life_days, opened"
        rows = db_query(
            f"SELECT {select_columns} FROM {bar_table}", (), fetch=True
        )
        results = []
        for r in rows:
            expiry_by_total = calc_expiry_by_total(r[4], r[5])
            expiry_by_opened = calc_expiry_by_opened(r[6], r[7])
            expiry_final = min_date(expiry_by_total, expiry_by_opened)
            if expiry_final and expiry_final <= now:
                results.append({
                    'id': r[0], 'category': r[1], 'tob': r[2], 'name': r[3],
                    'manufactured_at': r[4], 'shelf_life_days': r[5],
                    'opened_at': r[6], 'opened_shelf_life_days': r[7],
                    'opened': r[8],
                    'expiry_by_total': expiry_by_total,
                    'expiry_by_opened': expiry_by_opened,
                    'expiry_final': expiry_final
                })
        return jsonify(ok=True, results=results)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route('/search', methods=['POST'])
@require_hmac
def api_search():
    data = request.get_json()
    user_id = data.get('user_id')
    try:
        if not user_id or not check_user_access(user_id):
            return jsonify(ok=False, error="Нет доступа")
        bar_table = get_bar_table(user_id)
        query = data.get('query', '').strip().lower()
        select_columns = "id, category, tob, name, manufactured_at, shelf_life_days, opened_at, opened_shelf_life_days, opened"
        if not query:
            rows = db_query(
                f"SELECT {select_columns} FROM {bar_table}", (), fetch=True
            )
        elif query.isdigit() and len(query) == 6:
            rows = db_query(
                f"SELECT {select_columns} FROM {bar_table} WHERE tob=?", (query,), fetch=True
            )
        else:
            rows = db_query(
                f"SELECT {select_columns} FROM {bar_table} WHERE LOWER(name) LIKE ?", (f"%{query}%",), fetch=True
            )
        results = []
        for r in rows:
            expiry_by_total = calc_expiry_by_total(r[4], r[5])
            expiry_by_opened = calc_expiry_by_opened(r[6], r[7])
            expiry_final = min_date(expiry_by_total, expiry_by_opened)
            results.append({
                'id': r[0], 'category': r[1], 'tob': r[2], 'name': r[3],
                'manufactured_at': r[4], 'shelf_life_days': r[5],
                'opened_at': r[6], 'opened_shelf_life_days': r[7],
                'opened': r[8],
                'expiry_by_total': expiry_by_total,
                'expiry_by_opened': expiry_by_opened,
                'expiry_final': expiry_final
            })
        return jsonify(ok=True, results=results)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route('/update', methods=['POST'])
@require_hmac
def api_update():
    data = request.get_json()
    user_id = data.get('user_id')
    try:
        if not user_id or not check_user_access(user_id):
            return jsonify(ok=False, error="Нет доступа")
        bar_table = get_bar_table(user_id)
        item_id = data.get('id')
        if not item_id:
            return jsonify(ok=False, error="Не указан id позиции для обновления")
        fields = []
        params = []
        for field in ('category', 'name', 'manufactured_at', 'shelf_life_days', 'opened_at', 'opened_shelf_life_days', 'opened'):
            if field in data:
                fields.append(f"{field}=?")
                params.append(data[field])
        params.append(item_id)
        if not fields:
            return jsonify(ok=False, error="Нет данных для обновления")
        db_query(f"UPDATE {bar_table} SET {', '.join(fields)} WHERE id=?", tuple(params))
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route('/delete', methods=['POST'])
@require_hmac
def api_delete():
    data = request.get_json()
    user_id = data.get('user_id')
    try:
        if not user_id or not check_user_access(user_id):
            return jsonify(ok=False, error="Нет доступа")
        bar_table = get_bar_table(user_id)
        item_id = data.get('id')
        if not item_id:
            return jsonify(ok=False, error="Не указан id позиции для удаления")
        res = db_query(f"SELECT id FROM {bar_table} WHERE id=?", (item_id,), fetch=True)
        if not res:
            return jsonify(ok=False, error="Позиция с указанным id не найдена")
        db_query(f"DELETE FROM {bar_table} WHERE id=?", (item_id,))
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    import traceback
    tb = ''.join(traceback.format_exception(None, context.error, context.error.__traceback__))
    try:
        if update and hasattr(update, "message") and update.message:
            await update.message.reply_text(
                f"❌ Произошла ошибка:\n<code>{context.error}</code>\n\n"
                f"<b>Детали:</b>\n<code>{tb[-1500:]}</code>",
                parse_mode="HTML"
            )
    except Exception as e:
        pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        if not check_user_access(user_id):
            await update.message.reply_text("🔑 Введите ваш пригласительный код для регистрации:")
            return REG_WAIT_CODE
        res = db_query(f"SELECT bar_name, registered_at FROM {USERS_TABLE} WHERE user_id=?", (user_id,), fetch=True)
        if res:
            bar, reg = res[0]
            await update.message.reply_text(
                f"👤 Вы зарегистрированы в баре: <b>{bar}</b>\n"
                f"Дата регистрации: {reg}", parse_mode="HTML"
            )
        else:
            await update.message.reply_text("Данные пользователя не найдены. Зарегистрируйтесь с помощью кода.")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"Ошибка при проверке регистрации: {e}")

async def reg_wait_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    try:
        invites = db_query(
            f"SELECT bar_name FROM {INVITES_TABLE} WHERE code=? AND used='нет'", (code,), fetch=True
        )
        if not invites:
            await update.message.reply_text("❌ Код не найден или уже использован! Попробуйте снова или обратитесь к администратору.")
            return REG_WAIT_CODE
        user_exists = db_query(
            f"SELECT user_id FROM {USERS_TABLE} WHERE user_id=?", (user_id,), fetch=True
        )
        if user_exists:
            await update.message.reply_text("✅ Вы уже зарегистрированы.")
            return ConversationHandler.END
        bar_name = invites[0][0]
        now = msk_now().strftime('%Y-%m-%d %H:%M:%S')
        db_query(
            f"INSERT INTO {USERS_TABLE} (user_id, username, bar_name, registered_at) VALUES (?, ?, ?, ?)",
            (user_id, username, bar_name, now)
        )
        db_query(
            f"UPDATE {INVITES_TABLE} SET used='да' WHERE code=?", (code,)
        )
        ensure_bar_table(bar_name)
        await update.message.reply_text(f"✅ Добро пожаловать в {bar_name}!\nТеперь вы можете пользоваться мини-приложением (сайтом).")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"Ошибка при регистрации: {e}")

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        res = db_query(f"SELECT bar_name, registered_at FROM {USERS_TABLE} WHERE user_id=?", (user_id,), fetch=True)
        if res:
            bar, reg = res[0]
            await update.message.reply_text(
                f"👤 Вы зарегистрированы в баре: <b>{bar}</b>\n"
                f"Дата регистрации: {reg}", parse_mode="HTML"
            )
        else:
            await update.message.reply_text("Данные пользователя не найдены. Зарегистрируйтесь с помощью кода.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при получении информации о пользователе: {e}")

async def admin_only(update: Update):
    return update.effective_user and update.effective_user.id == TELEGRAM_ADMIN_ID

async def lastbackup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        await update.message.reply_text("Нет доступа")
        return
    global last_backup_time
    if last_backup_time:
        await update.message.reply_text(f"Последний бэкап был: {last_backup_time}")
    else:
        await update.message.reply_text("Бэкап ещё не отправлялся.")

async def forcebackup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        await update.message.reply_text("Нет доступа")
        return
    try:
        periodic_backup()
        await update.message.reply_text("Бэкап отправлен!")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при отправке бэкапа: {e}")

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin = (user_id == TELEGRAM_ADMIN_ID)
    if is_admin:
        commands = [
            '/start — регистрация или информация о вас',
            '/whoami — информация о вашем баре',
            '/lastbackup — время последнего бэкапа (админ)',
            '/forcebackup — сделать бэкап сейчас (админ)',
            '/sendbackup — получить текущий бэкап базы (админ)',
            '/restorebackup — восстановить базу из файла (админ)',
            '/uploadbackup — переслать файл в чат (админ)',
            '/info — список команд',
        ]
    else:
        commands = [
            '/start — регистрация или информация о вас',
            '/whoami — информация о вашем баре',
            '/info — список команд',
        ]
    text = 'Доступные команды:\n' + '\n'.join(commands)
    await update.message.reply_text(text)

def periodic_backup():
    global last_backup_time
    try:
        if not os.path.exists(DB_FILENAME):
            print(f"[periodic_backup] Файл базы не найден: {DB_FILENAME}")
            return
        file_size = os.path.getsize(DB_FILENAME)
        print(f"[periodic_backup] Размер файла: {file_size} байт")
        if file_size > 49 * 1024 * 1024:
            print(f"[periodic_backup] Файл слишком большой для Telegram (>49MB)")
            return
        # Используем асинхронную отправку через telegram.ext
        from telegram.ext import Application
        import asyncio
        async def send_backup():
            app = Application.builder().token(BOT_TOKEN).build()
            async with app:
                with open(DB_FILENAME, "rb") as f:
                    await app.bot.send_document(chat_id=TELEGRAM_ADMIN_ID, document=f, filename=DB_FILENAME)
            print("Бэкап базы отправлен в Telegram.")
        asyncio.run(send_backup())
        last_backup_time = datetime.now(pytz.timezone('Europe/Moscow')).strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        print(f"Ошибка при отправке бэкапа: {e}")

def start_periodic_backup():
    scheduler = BackgroundScheduler()
    scheduler.add_job(periodic_backup, 'interval', hours=2, minutes=30)
    scheduler.start()

def restore_db_from_telegram():
    try:
        bot = Bot(token=BOT_TOKEN)
        updates = bot.get_updates()
        bot_id = bot.get_me().id
        found = False
        # Сначала ищем среди сообщений от самого бота
        for update in reversed(updates):
            msg = update.message
            if msg and msg.from_user and msg.from_user.id == bot_id:
                if msg.document and msg.document.file_name.endswith('.sqlite'):
                    file = bot.get_file(msg.document.file_id)
                    file.download(DB_FILENAME)
                    print("База данных восстановлена из Telegram (отправитель: бот).")
                    found = True
                    break
        # Если не найдено — ищем среди сообщений от админа
        if not found:
            for update in reversed(updates):
                msg = update.message
                if msg and msg.from_user and msg.from_user.id == TELEGRAM_ADMIN_ID:
                    if msg.document and msg.document.file_name.endswith('.sqlite'):
                        file = bot.get_file(msg.document.file_id)
                        file.download(DB_FILENAME)
                        print("База данных восстановлена из Telegram (отправитель: админ).")
                        found = True
                        break
        if not found:
            print("Файл базы не найден в чате Telegram. Используется локальная база (если есть).")
    except Exception as e:
        print(f"Ошибка при восстановлении базы: {e}")

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

UPLOAD_BACKUP_WAIT_FILE = 100  # новое состояние для загрузки бэкапа

async def uploadbackup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != TELEGRAM_ADMIN_ID:
        await update.message.reply_text("Нет доступа")
        return ConversationHandler.END
    await update.message.reply_text("Пожалуйста, отправьте файл бэкапа (.sqlite)")
    return UPLOAD_BACKUP_WAIT_FILE

async def handle_backup_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != TELEGRAM_ADMIN_ID:
        await update.message.reply_text("Нет доступа")
        return ConversationHandler.END
    doc = update.message.document
    if not doc or not doc.file_name.endswith('.sqlite'):
        await update.message.reply_text("Пожалуйста, отправьте файл с расширением .sqlite")
        return UPLOAD_BACKUP_WAIT_FILE
    file = await doc.get_file()
    file_path = f"received_{doc.file_name}"
    await file.download_to_drive(file_path)
    await update.message.reply_text(f"Файл получен. Отправляю в чат...")
    bot = Bot(token=BOT_TOKEN)
    with open(file_path, "rb") as f:
        bot.send_document(chat_id=TELEGRAM_ADMIN_ID, document=f, filename=doc.file_name)
    await update.message.reply_text("Бэкап отправлен!")
    os.remove(file_path)
    return ConversationHandler.END

async def sendbackup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != TELEGRAM_ADMIN_ID:
        await update.message.reply_text("Нет доступа")
        return
    try:
        if not os.path.exists(DB_FILENAME):
            await update.message.reply_text(f"Файл базы не найден: {DB_FILENAME}")
            return
        file_size = os.path.getsize(DB_FILENAME)
        await update.message.reply_text(f"Размер файла: {file_size} байт. Пробую отправить...")
        if file_size > 49 * 1024 * 1024:
            await update.message.reply_text("Файл слишком большой для Telegram (>49MB)")
            return
        with open(DB_FILENAME, "rb") as f:
            await context.bot.send_document(chat_id=update.effective_chat.id, document=f, filename=DB_FILENAME)
        await update.message.reply_text("Бэкап отправлен!")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при отправке бэкапа: {e}")

async def restorebackup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != TELEGRAM_ADMIN_ID:
        await update.message.reply_text("Нет доступа")
        return ConversationHandler.END
    await update.message.reply_text("Пожалуйста, отправьте файл бэкапа (.sqlite), чтобы восстановить базу. ВНИМАНИЕ: текущая база будет перезаписана!")
    return RESTORE_BACKUP_WAIT_FILE

async def handle_restore_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != TELEGRAM_ADMIN_ID:
        await update.message.reply_text("Нет доступа")
        return ConversationHandler.END
    doc = update.message.document
    if not doc or not doc.file_name.endswith('.sqlite'):
        await update.message.reply_text("Пожалуйста, отправьте файл с расширением .sqlite")
        return RESTORE_BACKUP_WAIT_FILE
    file = await doc.get_file()
    await file.download_to_drive(DB_FILENAME)
    await update.message.reply_text(f"База успешно восстановлена из файла {doc.file_name}!")
    return ConversationHandler.END

if __name__ == '__main__':
    restore_db_from_telegram()  # сначала восстановить базу
    migrate_all_bars()
    start_periodic_backup()     # запустить периодический бэкап
    threading.Thread(target=run_flask, daemon=True).start()
    token = os.getenv('BOT_TOKEN')
    if not token:
        exit(1)
    bot_app = ApplicationBuilder().token(token).build()
    bot_app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            REG_WAIT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_wait_code)],
            UPLOAD_BACKUP_WAIT_FILE: [MessageHandler(filters.Document.ALL, handle_backup_file)],
            RESTORE_BACKUP_WAIT_FILE: [MessageHandler(filters.Document.ALL, handle_restore_file)]
        },
        fallbacks=[]
    ))
    bot_app.add_handler(CommandHandler('whoami', whoami))
    bot_app.add_handler(CommandHandler('lastbackup', lastbackup))
    bot_app.add_handler(CommandHandler('forcebackup', forcebackup))
    bot_app.add_handler(CommandHandler('info', info))
    bot_app.add_handler(CommandHandler('uploadbackup', uploadbackup))
    bot_app.add_handler(CommandHandler('sendbackup', sendbackup))
    bot_app.add_handler(CommandHandler('restorebackup', restorebackup))
    bot_app.add_error_handler(error_handler)
    bot_app.run_polling()
