import os
import threading
from datetime import datetime, timedelta
import sqlite3
from flask import Flask, request, jsonify
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
DELETE_WAIT_TOB = 1000  # состояние для удаления

def db_query(sql, params=(), fetch=False):
    try:
        if not os.path.exists(SQLITE_DB):
            print(f"Файл базы не найден: {SQLITE_DB}")
            raise Exception(f"Файл базы не найден: {SQLITE_DB}")
        with sqlite3.connect(SQLITE_DB) as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            if fetch:
                return cursor.fetchall()
            conn.commit()
            return None
    except Exception as e:
        print(f"[db_query] SQL error: {e}")
        raise

def get_user_bar(user_id):
    try:
        res = db_query(f"SELECT bar_name FROM {USERS_TABLE} WHERE user_id=?", (user_id,), fetch=True)
        return res[0][0] if res else None
    except Exception as e:
        print(f"[get_user_bar] {e}")
        return None

def check_user_access(user_id):
    try:
        return get_user_bar(user_id) is not None
    except Exception as e:
        print(f"[check_user_access] {e}")
        return False

def get_bar_table(user_id):
    bar_name = get_user_bar(user_id)
    return bar_name if bar_name in BARS else None

# ================== FLASK (API для сайта) ===================
app = Flask(__name__)

@app.route('/userinfo', methods=['POST'])
def api_userinfo():
    data = request.get_json()
    user_id = data.get('user_id')
    try:
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
        if not check_user_access(user_id):
            return jsonify(ok=False, error="Нет доступа")
        bar_table = get_bar_table(user_id)
        d = data
        db_query(
            f"INSERT INTO {bar_table} (category, tob, name, opened_at, shelf_life_days, expiry_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                d['category'],
                d['tob'],
                d['name'],
                d['opened_at'],
                int(d['shelf_life_days']),
                (datetime.strptime(d['opened_at'], '%Y-%m-%d') + timedelta(days=int(d['shelf_life_days']))).strftime('%Y-%m-%d')
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
        if not check_user_access(user_id):
            return jsonify(ok=False, error="Нет доступа")
        bar_table = get_bar_table(user_id)
        now = datetime.now().strftime('%Y-%m-%d')
        rows = db_query(
            f"SELECT category, tob, name, expiry_at FROM {bar_table} WHERE expiry_at <= ?", (now,), fetch=True
        )
        results = []
        for cat, tob, name, exp in rows:
            results.append({
                'category': cat, 'tob': tob, 'name': name, 'expiry_at': str(exp)
            })
        return jsonify(ok=True, results=results)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route('/search', methods=['POST'])
def api_search():
    data = request.get_json()
    user_id = data.get('user_id')
    try:
        if not check_user_access(user_id):
            return jsonify(ok=False, error="Нет доступа")
        bar_table = get_bar_table(user_id)
        query = data.get('query', '').strip()
        if not query:
            rows = db_query(
                f"SELECT category, tob, name, opened_at, shelf_life_days, expiry_at FROM {bar_table}", (), fetch=True
            )
        elif query.isdigit() and len(query) == 6:
            rows = db_query(
                f"SELECT category, tob, name, opened_at, shelf_life_days, expiry_at FROM {bar_table} WHERE tob=?", (query,), fetch=True
            )
        else:
            rows = db_query(
                f"SELECT category, tob, name, opened_at, shelf_life_days, expiry_at FROM {bar_table} WHERE name LIKE ?", (f"%{query}%",), fetch=True
            )
        results = []
        for r in rows:
            results.append({
                'category': r[0], 'tob': r[1], 'name': r[2],
                'opened_at': str(r[3]), 'shelf_life_days': r[4],
                'expiry_at': str(r[5])
            })
        return jsonify(ok=True, results=results)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route('/reopen', methods=['POST'])
def api_reopen():
    data = request.get_json()
    user_id = data.get('user_id')
    try:
        if not check_user_access(user_id):
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
        if not check_user_access(user_id):
            return jsonify(ok=False, error="Нет доступа")
        bar_table = get_bar_table(user_id)
        res = db_query(f"SELECT name FROM {bar_table} WHERE tob=?", (tob,), fetch=True)
        if not res:
            return jsonify(ok=False, error="Позиция не найдена")
        db_query(f"DELETE FROM {bar_table} WHERE tob=?", (tob,))
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

# =============== TELEGRAM BOT ==============

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    import traceback
    print("[Telegram Error Handler]", context.error)
    tb = ''.join(traceback.format_exception(None, context.error, context.error.__traceback__))
    try:
        if update and hasattr(update, "message") and update.message:
            await update.message.reply_text(
                f"❌ Произошла ошибка:\n<code>{context.error}</code>\n\n"
                f"<b>Детали:</b>\n<code>{tb[-1500:]}</code>",
                parse_mode="HTML"
            )
    except Exception as e:
        print("Ошибка при отправке сообщения об ошибке:", e)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        if not check_user_access(user_id):
            await update.message.reply_text("🔑 Введите ваш пригласительный код для регистрации:")
            return REG_WAIT_CODE
        await update.message.reply_text("✅ Вы уже зарегистрированы!\n\nДля проверки регистрации напишите /whoami.\nДля списка позиций: /list\nДля удаления позиции: /delete")
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

# ----------- DELETE LOGIC -----------
async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_user_access(user_id):
        await update.message.reply_text("Нет доступа. Сначала зарегистрируйтесь.")
        return ConversationHandler.END
    await update.message.reply_text("Введите TOB позиции (6 цифр), которую хотите удалить:")
    return DELETE_WAIT_TOB

async def delete_wait_tob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tob = update.message.text.strip()
    if not tob.isdigit() or len(tob) != 6:
        await update.message.reply_text("Некорректный TOB. Введите ровно 6 цифр:")
        return DELETE_WAIT_TOB
    bar_table = get_bar_table(user_id)
    if not bar_table:
        await update.message.reply_text("Не удалось определить бар пользователя.")
        return ConversationHandler.END
    res = db_query(f"SELECT name FROM {bar_table} WHERE tob=?", (tob,), fetch=True)
    if not res:
        await update.message.reply_text("Позиция с таким TOB не найдена.")
        return ConversationHandler.END
    db_query(f"DELETE FROM {bar_table} WHERE tob=?", (tob,))
    await update.message.reply_text(f"✅ Позиция '{res[0][0]}' (TOB:{tob}) удалена.")
    return ConversationHandler.END
# ----------- END DELETE LOGIC -----------

# ----------- LIST POSITIONS -----------
async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_user_access(user_id):
        await update.message.reply_text("Нет доступа. Сначала зарегистрируйтесь.")
        return
    bar_table = get_bar_table(user_id)
    if not bar_table:
        await update.message.reply_text("Не удалось определить бар пользователя.")
        return
    rows = db_query(f"SELECT category, tob, name, expiry_at FROM {bar_table} ORDER BY expiry_at ASC", (), fetch=True)
    if not rows:
        await update.message.reply_text("У вас нет добавленных позиций.")
        return
    msg = "📋 Ваши позиции:\n"
    for cat, tob, name, exp in rows:
        msg += f"\n<b>{name}</b> [{cat}]\nTOB: <code>{tob}</code>\nГоден до: <code>{exp}</code>\n"
    await update.message.reply_text(msg, parse_mode="HTML")
# ----------- END LIST POSITIONS -----------

def run_flask():
    app.run(host="0.0.0.0", port=5000)

if __name__ == '__main__':
    print("Используется база данных:", SQLITE_DB)
    print("Файл существует?", os.path.exists(SQLITE_DB))
    threading.Thread(target=run_flask, daemon=True).start()
    token = os.getenv('BOT_TOKEN')
    if not token:
        print("В файле .env не найден BOT_TOKEN!")
        exit(1)
    bot_app = ApplicationBuilder().token(token).build()
    # Регистрация
    bot_app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={REG_WAIT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_wait_code)]},
        fallbacks=[]
    ))
    # Удаление
    bot_app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('delete', delete_command)],
        states={DELETE_WAIT_TOB: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_wait_tob)]},
        fallbacks=[]
    ))
    # Показывать свои позиции
    bot_app.add_handler(CommandHandler('list', list_command))
    bot_app.add_handler(CommandHandler('whoami', whoami))
    bot_app.add_error_handler(error_handler)
    bot_app.run_polling()
