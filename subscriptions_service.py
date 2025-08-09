from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3
from datetime import datetime, date
import uuid
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import logging
import threading
import re

app = Flask(__name__)
app.secret_key = 'your-secret-key'  # Необходим для flash-сообщений

# Конфигурация Telegram-бота
TELEGRAM_BOT_TOKEN = '8112793004:AAFxvygvCn8GyoPIAcAXqe_C_8xv9eWZoJE'
CHECK_INTERVAL = 60  # Проверка подписок каждые 60 секунд (1 минута)

# Инициализация Telegram-бота
application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions 
                 (id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL, 
                  start_date TEXT, end_date TEXT NOT NULL, status TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS keys 
                 (id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL, 
                  key_name TEXT NOT NULL, key_value TEXT NOT NULL, 
                  FOREIGN KEY (subscription_id) REFERENCES subscriptions(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS chats 
                 (chat_id TEXT PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS alert_settings 
                 (id INTEGER PRIMARY KEY, alert_time TEXT NOT NULL)''')
    c.execute('INSERT OR IGNORE INTO alert_settings (id, alert_time) VALUES (1, ?)', ('12:00',))
    conn.commit()
    conn.close()

# Функция для сохранения chat_id
async def save_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = datetime.now()
    chat_id = str(update.effective_chat.id)
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    try:
        c.execute('INSERT OR IGNORE INTO chats (chat_id) VALUES (?)', (chat_id,))
        conn.commit()
        await update.message.reply_text(f"Чат {chat_id} добавлен для уведомлений.")
        logging.info(f"Команда /start обработана для чата {chat_id} за {(datetime.now() - start_time).total_seconds():.2f} сек")
    except Exception as e:
        logging.error(f"Ошибка при сохранении chat_id: {str(e)}")
        await update.message.reply_text(f"Ошибка при добавлении чата: {str(e)}")
    finally:
        conn.close()

# Функция для установки времени уведомлений
async def set_alert_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = datetime.now()
    if not context.args:
        await update.message.reply_text("Использование: /setalerttime HH:MM (например, /setalerttime 14:30)")
        logging.info(f"Команда /setalerttime обработана за {(datetime.now() - start_time).total_seconds():.2f} сек")
        return
    time_str = context.args[0]
    if not re.match(r'^\d{2}:\d{2}$', time_str):
        await update.message.reply_text("Неверный формат времени. Используйте HH:MM (например, 14:30).")
        logging.info(f"Команда /setalerttime обработана за {(datetime.now() - start_time).total_seconds():.2f} сек")
        return
    try:
        hour, minute = map(int, time_str.split(':'))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            await update.message.reply_text("Недопустимое время. Часы: 0-23, минуты: 0-59.")
            logging.info(f"Команда /setalerttime обработана за {(datetime.now() - start_time).total_seconds():.2f} сек")
            return
    except ValueError:
        await update.message.reply_text("Неверный формат времени. Используйте HH:MM (например, 14:30).")
        logging.info(f"Команда /setalerttime обработана за {(datetime.now() - start_time).total_seconds():.2f} сек")
        return
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    try:
        c.execute('UPDATE alert_settings SET alert_time = ? WHERE id = 1', (time_str,))
        conn.commit()
        await update.message.reply_text(f"Время уведомлений установлено: {time_str}.")
        logging.info(f"Команда /setalerttime ({time_str}) обработана за {(datetime.now() - start_time).total_seconds():.2f} сек")
    except Exception as e:
        logging.error(f"Ошибка при установке времени уведомлений: {str(e)}")
        await update.message.reply_text(f"Ошибка: {str(e)}")
    finally:
        conn.close()

# Функция отправки уведомления во все сохранённые чаты
async def send_telegram_notification(message: str, disable_notification: bool = False):
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute('SELECT chat_id FROM chats')
    chat_ids = [row[0] for row in c.fetchall()]
    conn.close()

    if not chat_ids:
        logging.warning("Нет чатов для отправки уведомлений. Отправьте /start в чате с ботом.")
        return

    for chat_id in chat_ids:
        for attempt in range(3):  # Пытаемся отправить до 3 раз
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    disable_notification=disable_notification
                )
                logging.info(f"Уведомление отправлено в чат {chat_id}: {message}")
                break
            except Exception as e:
                logging.error(f"Попытка {attempt + 1} не удалась для чата {chat_id}: {str(e)}")
                if attempt == 2:
                    logging.error(f"Не удалось отправить уведомление в чат {chat_id} после 3 попыток.")
                await asyncio.sleep(0.5)  # Уменьшена задержка до 0.5 сек
        await asyncio.sleep(0.2)  # Уменьшена задержка между чатами до 0.2 сек

# Функция проверки подписок
last_notification_time = None

async def check_subscriptions():
    global last_notification_time
    start_time = datetime.now()
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute("SELECT * FROM subscriptions WHERE status = 'active'")
    subscriptions = c.fetchall()
    c.execute("SELECT alert_time FROM alert_settings WHERE id = 1")
    alert_time = c.fetchone()[0]
    conn.close()

    alert_hour, alert_minute = map(int, alert_time.split(':'))
    current_time = datetime.now()  # Локальное время

    # Проверяем, находится ли текущее время в окне ±1 минута
    alert_datetime = current_time.replace(hour=alert_hour, minute=alert_minute, second=0, microsecond=0)
    time_diff = (current_time - alert_datetime).total_seconds() / 60.0
    if not (-1 <= time_diff <= 1):
        return

    # Проверяем, не отправляли ли уведомления недавно (в последние 3 минуты)
    if last_notification_time and (current_time - last_notification_time).total_seconds() < 180:
        logging.info(f"Уведомления пропущены в {current_time.strftime('%H:%M:%S')} (уже отправлены в {last_notification_time.strftime('%H:%M:%S')})")
        return

    logging.info(f"Отправка уведомлений в {current_time.strftime('%H:%M:%S')} для времени {alert_time}")
    last_notification_time = current_time

    current_date = current_time.date()
    for sub in subscriptions:
        sub_id, sub_type, sub_name, start_date, end_date, status = sub
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        days_left = (end_date - current_date).days
        if days_left in [14, 7, 6, 5, 4, 3, 2, 1]:
            message = f'{sub_type} {sub_name} истекает {end_date}.'
            await send_telegram_notification(message, disable_notification=(days_left != 7))
        elif days_left == 0:
            message = f'{sub_type} {sub_name} истекла сегодня ({end_date}).'
            await send_telegram_notification(message, disable_notification=True)
    logging.info(f"Проверка подписок завершена за {(datetime.now() - start_time).total_seconds():.2f} сек")

# Функция отправки уведомлений по команде /notify
async def notify_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = datetime.now()
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute("SELECT * FROM subscriptions WHERE status = 'active'")
    subscriptions = c.fetchall()
    conn.close()

    current_date = datetime.now().date()  # Локальное время
    if not subscriptions:
        await update.message.reply_text("Нет активных подписок.")
        logging.info(f"Команда /notify обработана за {(datetime.now() - start_time).total_seconds():.2f} сек")
        return

    for sub in subscriptions:
        sub_id, sub_type, sub_name, start_date, end_date, status = sub
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        days_left = (end_date - current_date).days
        if days_left in [14, 7, 6, 5, 4, 3, 2, 1]:
            message = f'{sub_type} {sub_name} истекает {end_date}.'
            await send_telegram_notification(message, disable_notification=(days_left != 7))
        elif days_left == 0:
            message = f'{sub_type} {sub_name} истекла сегодня ({end_date}).'
            await send_telegram_notification(message, disable_notification=True)
    logging.info(f"Команда /notify обработана за {(datetime.now() - start_time).total_seconds():.2f} сек")

# Асинхронный цикл проверки подписок
async def subscription_checker_loop():
    while True:
        try:
            logging.info(f"Проверка подписок в {datetime.now().strftime('%H:%M:%S')}")
            await check_subscriptions()
        except Exception as e:
            logging.error(f"Ошибка в цикле проверки подписок: {str(e)}")
        await asyncio.sleep(CHECK_INTERVAL)

# Запуск Telegram-бота в отдельном потоке
def run_telegram_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(application.run_polling(poll_interval=5))  # Уменьшен интервал до 5 сек

# Запуск проверки подписок
def start_subscription_checker():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    threading.Thread(target=lambda: loop.run_until_complete(subscription_checker_loop()), daemon=True).start()

# Главная страница со списком подписок
@app.route('/')
def index():
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute('SELECT * FROM subscriptions')
    subscriptions = []
    current_date = date.today()
    for row in c.fetchall():
        end_date = datetime.strptime(row[4], '%Y-%m-%d').date()
        status = row[5].strip().lower()
        if end_date < current_date and status != 'expired':
            c.execute('UPDATE subscriptions SET status = ? WHERE id = ?', ('expired', row[0]))
            conn.commit()
            status = 'expired'
        subscriptions.append({
            'id': row[0], 'type': row[1], 'name': row[2],
            'start_date': row[3], 'end_date': row[4], 'status': status,
            'subscription_keys': []
        })
    conn.close()
    return render_template('index.html', subscriptions=subscriptions)

# Страница управления ключами подписки
@app.route('/subscription/<subscription_id>/keys')
def keys(subscription_id):
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute('SELECT * FROM subscriptions WHERE id = ?', (subscription_id,))
    subscription = c.fetchone()
    if not subscription:
        conn.close()
        flash('Подписка не найдена!', 'danger')
        return redirect(url_for('index'))
    c.execute('SELECT id, key_name, key_value FROM keys WHERE subscription_id = ?', (subscription_id,))
    keys = [{'id': key[0], 'key_name': key[1], 'key_value': key[2]} for key in c.fetchall()]
    subscription_data = {'id': subscription[0], 'name': subscription[2]}
    conn.close()
    return render_template('keys.html', subscription=subscription_data, keys=keys)

# Страница добавления подписки
@app.route('/subscription/add', methods=['GET', 'POST'])
def add_subscription():
    if request.method == 'POST':
        try:
            subscription_id = str(uuid.uuid4())
            status = request.form['status'].strip().lower()
            if status not in ['active', 'inactive', 'expired']:
                raise ValueError("Недопустимое значение статуса")
            start_date = request.form.get('start_date') or None
            conn = sqlite3.connect('subscriptions.db')
            c = conn.cursor()
            c.execute('''INSERT INTO subscriptions (id, type, name, start_date, end_date, status) 
                         VALUES (?, ?, ?, ?, ?, ?)''',
                      (subscription_id, request.form['type'], request.form['name'],
                       start_date, request.form['end_date'], status))
            conn.commit()
            conn.close()
            flash('Подписка успешно добавлена!', 'success')
            return redirect(url_for('index'))
        except Exception as e:
            flash(f'Ошибка при добавлении подписки: {str(e)}', 'danger')
    return render_template('add_subscription.html')

# Страница редактирования подписки
@app.route('/subscription/edit/<id>', methods=['GET', 'POST'])
def edit_subscription(id):
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute('SELECT * FROM subscriptions WHERE id = ?', (id,))
    subscription = c.fetchone()
    if not subscription:
        conn.close()
        flash('Подписка не найдена!', 'danger')
        return redirect(url_for('index'))
    if request.method == 'POST':
        try:
            status = request.form['status'].strip().lower()
            if status not in ['active', 'inactive', 'expired']:
                raise ValueError("Недопустимое значение статуса")
            start_date = request.form.get('start_date') or None
            c.execute('''UPDATE subscriptions SET type = ?, name = ?, start_date = ?, end_date = ?, status = ? 
                         WHERE id = ?''',
                      (request.form['type'], request.form['name'], start_date,
                       request.form['end_date'], status, id))
            conn.commit()
            flash('Подписка успешно обновлена!', 'success')
            conn.close()
            return redirect(url_for('index'))
        except Exception as e:
            flash(f'Ошибка при обновлении подписки: {str(e)}', 'danger')
    subscription_data = {
        'id': subscription[0], 'type': subscription[1], 'name': subscription[2],
        'start_date': subscription[3], 'end_date': subscription[4], 'status': subscription[5].strip().lower()
    }
    conn.close()
    return render_template('edit_subscription.html', subscription=subscription_data)

# Удаление подписки
@app.route('/subscription/delete/<id>')
def delete_subscription(id):
    try:
        conn = sqlite3.connect('subscriptions.db')
        c = conn.cursor()
        c.execute('SELECT * FROM subscriptions WHERE id = ?', (id,))
        if not c.fetchone():
            conn.close()
            flash('Подписка не найдена!', 'danger')
            return redirect(url_for('index'))
        c.execute('DELETE FROM keys WHERE subscription_id = ?', (id,))
        c.execute('DELETE FROM subscriptions WHERE id = ?', (id,))
        conn.commit()
        conn.close()
        flash('Подписка и связанные ключи успешно удалены!', 'success')
    except Exception as e:
        flash(f'Ошибка при удалении подписки: {str(e)}', 'danger')
    return redirect(url_for('index'))

# Страница добавления ключа
@app.route('/subscription/<subscription_id>/key/add', methods=['GET', 'POST'])
def add_key(subscription_id):
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute('SELECT * FROM subscriptions WHERE id = ?', (subscription_id,))
    subscription = c.fetchone()
    if not subscription:
        conn.close()
        flash('Подписка не найдена!', 'danger')
        return redirect(url_for('index'))
    if request.method == 'POST':
        try:
            key_id = str(uuid.uuid4())
            c.execute('''INSERT INTO keys (id, subscription_id, key_name, key_value) 
                         VALUES (?, ?, ?, ?)''',
                      (key_id, subscription_id, request.form['key_name'], request.form['key_value']))
            conn.commit()
            flash('Ключ успешно добавлен!', 'success')
            conn.close()
            return redirect(url_for('keys', subscription_id=subscription_id))
        except Exception as e:
            flash(f'Ошибка при добавлении ключа: {str(e)}', 'danger')
    conn.close()
    return render_template('add_key.html', subscription_id=subscription_id)

# Страница редактирования ключа
@app.route('/key/edit/<key_id>', methods=['GET', 'POST'])
def edit_key(key_id):
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute('SELECT * FROM keys WHERE id = ?', (key_id,))
    key = c.fetchone()
    if not key:
        conn.close()
        flash('Ключ не найден!', 'danger')
        return redirect(url_for('index'))
    if request.method == 'POST':
        try:
            c.execute('''UPDATE keys SET key_name = ?, key_value = ? WHERE id = ?''',
                      (request.form['key_name'], request.form['key_value'], key_id))
            conn.commit()
            flash('Ключ успешно обновлен!', 'success')
            conn.close()
            return redirect(url_for('keys', subscription_id=key[1]))
        except Exception as e:
            flash(f'Ошибка при обновлении ключа: {str(e)}', 'danger')
    key_data = {'id': key[0], 'subscription_id': key[1], 'key_name': key[2], 'key_value': key[3]}
    conn.close()
    return render_template('edit_key.html', key=key_data)

# Удаление ключа
@app.route('/key/delete/<key_id>')
def delete_key(key_id):
    try:
        conn = sqlite3.connect('subscriptions.db')
        c = conn.cursor()
        c.execute('SELECT * FROM keys WHERE id = ?', (key_id,))
        key = c.fetchone()
        if not key:
            conn.close()
            flash('Ключ не найден!', 'danger')
            return redirect(url_for('index'))
        c.execute('DELETE FROM keys WHERE id = ?', (key_id,))
        conn.commit()
        conn.close()
        flash('Ключ успешно удален!', 'success')
        return redirect(url_for('keys', subscription_id=key[1]))
    except Exception as e:
        flash(f'Ошибка при удалении ключа: {str(e)}', 'danger')
    return redirect(url_for('index'))

if __name__ == '__main__':
    init_db()
    application.add_handler(CommandHandler("start", save_chat_id))
    application.add_handler(CommandHandler("notify", notify_subscriptions))
    application.add_handler(CommandHandler("setalerttime", set_alert_time))
    start_subscription_checker()
    threading.Thread(target=run_telegram_bot, daemon=True).start()
    app.run(debug=False, host='127.0.0.1', port=5000)