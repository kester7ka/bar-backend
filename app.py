import os
import threading
from datetime import datetime, timedelta
import sqlite3
from flask import Flask, request, jsonify
from flask_cors import CORS
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)
from dotenv import load_dotenv

load_dotenv()

SQLITE_DB = os.getenv("SQLITE_DB", "your_bot_db.sqlite")
USERS_TABLE = 'users'
INVITES_TABLE = 'invites'
BARS = ['АВОШ59', 'АВПМ97', 'АВЯР01', 'АВКОСМ04', 'АВКО04', 'АВДШ02', 'АВКШ78', 'АВПМ58', 'АВЛБ96']
CATEGORIES = ["🍯 Сиропы", "🥕 Ингредиенты", "📦 Прочее"]

REG_WAIT_CODE = 0

app = Flask(__name__)
CORS(app, origins=["https://kester7ka.github.io", "https://kester7ka.github.io/my-bar-site"], supports_credentials=True)

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

def ensure_bar_table(bar_name):
    if bar_name not in BARS:
        raise Exception("Неизвестный бар")
    with sqlite3.connect(SQLITE_DB) as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {bar_name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            tob TEXT UNIQUE,
            name TEXT,
            opened_at TEXT,
            shelf_life_days INTEGER,
            expiry_at TEXT,
            opened INTEGER DEFAULT 1
        )
        """)
        conn.commit()

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

@app.route('/userinfo', methods=['POST'])
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
def api_add():
    data = request.get_json()
    user_id = data.get('user_id')
    try:
        if not user_id or not check_user_access(user_id):
            return jsonify(ok=False, error="Нет доступа")
        bar_table = get_bar_table(user_id)
        d = data
        opened = int(d.get('opened', 1))
        db_query(
            f"INSERT INTO {bar_table} (category, tob, name, opened_at, shelf_life_days, expiry_at, opened) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                d['category'],
                d['tob'],
                d['name'],
                d['opened_at'],
                int(d['shelf_life_days']),
                (datetime.strptime(d['opened_at'], '%Y-%m-%d') + timedelta(days=int(d['shelf_life_days']))).strftime('%Y-%m-%d'),
                opened
            )
        )
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route('/expired', methods=['POST'])
def api_expired():
    data = request.get_json()
    user_id = data.get('user_id')
    try:
        if not user_id or not check_user_access(user_id):
            return jsonify(ok=False, error="Нет доступа")
        bar_table = get_bar_table(user_id)
        now = datetime.now().strftime('%Y-%m-%d')
        rows = db_query(
            f"SELECT category, tob, name, expiry_at, opened FROM {bar_table} WHERE expiry_at <= ?", (now,), fetch=True
        )
        results = []
        for cat, tob, name, exp, opened in rows:
            results.append({
                'category': cat, 'tob': tob, 'name': name, 'expiry_at': str(exp), 'opened': opened
            })
        return jsonify(ok=True, results=results)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route('/search', methods=['POST'])
def api_search():
    data = request.get_json()
    user_id = data.get('user_id')
    try:
        if not user_id or not check_user_access(user_id):
            return jsonify(ok=False, error="Нет доступа")
        bar_table = get_bar_table(user_id)
        query = data.get('query', '').strip()
        if not query:
            rows = db_query(
                f"SELECT category, tob, name, opened_at, shelf_life_days, expiry_at, opened FROM {bar_table}", (), fetch=True
            )
        elif query.isdigit() and len(query) == 6:
            rows = db_query(
                f"SELECT category, tob, name, opened_at, shelf_life_days, expiry_at, opened FROM {bar_table} WHERE tob=?", (query,), fetch=True
            )
        else:
            rows = db_query(
                f"SELECT category, tob, name, opened_at, shelf_life_days, expiry_at, opened FROM {bar_table} WHERE name LIKE ?", (f"%{query}%",), fetch=True
            )
        results = []
        for r in rows:
            results.append({
                'category': r[0], 'tob': r[1], 'name': r[2],
                'opened_at': str(r[3]), 'shelf_life_days': r[4],
                'expiry_at': str(r[5]), 'opened': r[6]
            })
        return jsonify(ok=True, results=results)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route('/reopen', methods=['POST'])
def api_reopen():
    data = request.get_json()
    user_id = data.get('user_id')
    try:
        if not user_id or not check_user_access(user_id):
            return jsonify(ok=False, error="Нет доступа")
        bar_table = get_bar_table(user_id)
        tob = data['tob']
        opened_at = data['opened_at']
        shelf_life_days = int(data['shelf_life_days'])
        expiry_at = (datetime.strptime(opened_at, '%Y-%m-%d') + timedelta(days=shelf_life_days)).strftime('%Y-%m-%d')
        db_query(
            f"UPDATE {bar_table} SET opened_at=?, shelf_life_days=?, expiry_at=? WHERE tob=?",
            (opened_at, shelf_life_days, expiry_at, tob)
        )
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route('/delete', methods=['POST'])
def api_delete():
    data = request.get_json()
    user_id = data.get('user_id')
    tob = data.get('tob')
    try:
        if not user_id or not check_user_access(user_id):
            return jsonify(ok=False, error="Нет доступа")
        bar_table = get_bar_table(user_id)
        res = db_query(f"SELECT name FROM {bar_table} WHERE tob=?", (tob,), fetch=True)
        if not res:
            return jsonify(ok=False, error="Позиция не найдена")
        db_query(f"DELETE FROM {bar_table} WHERE tob=?", (tob,))
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
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

if __name__ == '__main__':
    print("Используется база данных:", SQLITE_DB)
    print("Файл существует?", os.path.exists(SQLITE_DB))
    threading.Thread(target=run_flask, daemon=True).start()
    token = os.getenv('BOT_TOKEN')
    if not token:
        print("В файле .env не найден BOT_TOKEN!")
        exit(1)
    bot_app = ApplicationBuilder().token(token).build()
    bot_app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={REG_WAIT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_wait_code)]},
        fallbacks=[]
    ))
    bot_app.add_handler(CommandHandler('whoami', whoami))
    bot_app.add_error_handler(error_handler)
    bot_app.run_polling()
