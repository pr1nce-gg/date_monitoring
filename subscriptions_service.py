from flask import Flask, render_template, request, redirect, url_for, flash, send_file
import sqlite3
from datetime import datetime, date
import uuid
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import logging
import threading
import re
import os
import shutil
import time

app = Flask(__name__)
app.secret_key = 'your-secret-key'

# Конфигурация Telegram-бота
CHECK_INTERVAL = 60

# Глобальные переменные для управления ботом
application = None
bot_thread = None
stop_bot = False

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)

# Функции для работы с настройками в БД
def get_setting(key):
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute('SELECT value FROM settings WHERE key = ?', (key,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def set_setting(key, value):
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()

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
    c.execute('''CREATE TABLE IF NOT EXISTS settings 
                 (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('bot_token', ''))
    conn.commit()
    conn.close()

# Функция для сохранения chat_id
async def save_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    try:
        c.execute('INSERT OR IGNORE INTO chats (chat_id) VALUES (?)', (chat_id,))
        conn.commit()
        await update.message.reply_text(f"Чат {chat_id} добавлен для уведомлений.")
        logging.info(f"Чат {chat_id} добавлен для уведомлений")
    except Exception as e:
        logging.error(f"Ошибка при сохранении chat_id: {str(e)}")
        await update.message.reply_text(f"Ошибка при добавлении чата: {str(e)}")
    finally:
        conn.close()

# Функция для установки времени уведомлений
async def set_alert_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /setalerttime HH:MM (например, /setalerttime 14:30)")
        return
    
    time_str = context.args[0]
    if not re.match(r'^\d{2}:\d{2}$', time_str):
        await update.message.reply_text("Неверный формат времени. Используйте HH:MM (например, 14:30).")
        return
    
    try:
        hour, minute = map(int, time_str.split(':'))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            await update.message.reply_text("Недопустимое время. Часы: 0-23, минуты: 0-59.")
            return
    except ValueError:
        await update.message.reply_text("Неверный формат времени. Используйте HH:MM (например, 14:30).")
        return
    
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    try:
        c.execute('UPDATE alert_settings SET alert_time = ? WHERE id = 1', (time_str,))
        conn.commit()
        await update.message.reply_text(f"Время уведомлений установлено: {time_str}.")
        logging.info(f"Время уведомлений установлено: {time_str}")
    except Exception as e:
        logging.error(f"Ошибка при установке времени уведомлений: {str(e)}")
        await update.message.reply_text(f"Ошибка: {str(e)}")
    finally:
        conn.close()

# Функция отправки уведомления во все сохранённые чаты
async def send_telegram_notification(message: str, disable_notification: bool = False):
    global application
    if application is None:
        logging.warning("Бот не инициализирован.")
        return

    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute('SELECT chat_id FROM chats')
    chat_ids = [row[0] for row in c.fetchall()]
    conn.close()

    if not chat_ids:
        logging.warning("Нет чатов для отправки уведомлений.")
        return

    for chat_id in chat_ids:
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=message,
                disable_notification=disable_notification
            )
            logging.info(f"Уведомление отправлено в чат {chat_id}: {message}")
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление в чат {chat_id}: {str(e)}")

# Функция проверки подписок
last_notification_time = None

async def check_subscriptions():
    global last_notification_time
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute("SELECT * FROM subscriptions WHERE status = 'active'")
    subscriptions = c.fetchall()
    c.execute("SELECT alert_time FROM alert_settings WHERE id = 1")
    alert_time = c.fetchone()[0]
    conn.close()

    alert_hour, alert_minute = map(int, alert_time.split(':'))
    current_time = datetime.now()

    # Проверяем, находится ли текущее время в окне ±1 минута
    alert_datetime = current_time.replace(hour=alert_hour, minute=alert_minute, second=0, microsecond=0)
    time_diff = (current_time - alert_datetime).total_seconds() / 60.0
    if not (-1 <= time_diff <= 1):
        return

    # Проверяем, не отправляли ли уведомления недавно
    if last_notification_time and (current_time - last_notification_time).total_seconds() < 180:
        return

    last_notification_time = current_time
    current_date = current_time.date()

    for sub in subscriptions:
        sub_id, sub_type, sub_name, start_date, end_date, status = sub
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        days_left = (end_date - current_date).days
        
        if days_left in [14, 7, 6, 5, 4, 3, 2, 1]:
            message = f'{sub_type} {sub_name} истекает {end_date}. Осталось дней: {days_left}'
            await send_telegram_notification(message, disable_notification=(days_left != 7))
        elif days_left == 0:
            message = f'{sub_type} {sub_name} истекла сегодня ({end_date}).'
            await send_telegram_notification(message, disable_notification=True)

# Функция отправки уведомлений по команде /notify
async def notify_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute("SELECT * FROM subscriptions WHERE status = 'active'")
    subscriptions = c.fetchall()
    conn.close()

    current_date = datetime.now().date()
    
    if not subscriptions:
        await update.message.reply_text("Нет активных подписок.")
        return
    
    for sub in subscriptions:
        sub_id, sub_type, sub_name, start_date, end_date, status = sub
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        days_left = (end_date - current_date).days
        
        if days_left in [14, 7, 6, 5, 4, 3, 2, 1]:
            message = f'{sub_type} {sub_name} истекает {end_date}. Осталось дней: {days_left}'
            await send_telegram_notification(message, disable_notification=(days_left != 7))
        elif days_left == 0:
            message = f'{sub_type} {sub_name} истекла сегодня ({end_date}).'
            await send_telegram_notification(message, disable_notification=True)
    
    await update.message.reply_text("Уведомления отправлены.")

# Асинхронный цикл проверки подписок
async def subscription_checker_loop():
    while True:
        try:
            await check_subscriptions()
        except Exception as e:
            logging.error(f"Ошибка в цикле проверки подписок: {str(e)}")
        await asyncio.sleep(CHECK_INTERVAL)

# Запуск Telegram-бота
async def run_telegram_bot():
    global application, stop_bot
    token = get_setting('bot_token')
    if not token:
        logging.error("Токен бота не настроен в БД. Бот не запускается.")
        return

    application = Application.builder().token(token).build()

    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", save_chat_id))
    application.add_handler(CommandHandler("notify", notify_subscriptions))
    application.add_handler(CommandHandler("setalerttime", set_alert_time))
    
    logging.info("Запуск Telegram бота...")
    
    try:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(
            poll_interval=3.0,
            drop_pending_updates=True,
            timeout=10
        )
        logging.info("Telegram бот запущен успешно")
        
        # Бесконечный цикл с проверкой флага остановки
        while not stop_bot:
            await asyncio.sleep(1)
        
        stop_bot = False
        
    except Exception as e:
        logging.error(f"Ошибка при запуске Telegram бота: {str(e)}")
    finally:
        try:z
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
            application = None
        except:
            pass

# Запуск проверки подписок в отдельном потоке
def start_subscription_checker():
    def run_checker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(subscription_checker_loop())
        except Exception as e:
            logging.error(f"Ошибка в checker loop: {str(e)}")
        finally:
            loop.close()
    
    thread = threading.Thread(target=run_checker, daemon=True)
    thread.start()
    logging.info("Запущен checker подписок")

# Запуск Telegram бота в отдельном потоке
def start_telegram_bot():
    global bot_thread
    def run_bot():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_telegram_bot())
        except Exception as e:
            logging.error(f"Ошибка при запуске бота: {str(e)}")
        finally:
            loop.close()
    
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    logging.info("Запущен Telegram бот в отдельном потоке")

# Функция перезапуска бота
def restart_bot():
    global stop_bot, bot_thread
    if bot_thread and bot_thread.is_alive():
        stop_bot = True
        bot_thread.join()
        logging.info("Бот остановлен для перезапуска.")
    start_telegram_bot()
    logging.info("Бот перезапущен с новым токеном.")

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

# Создание резервной копии базы данных
@app.route('/backup')
def backup():
    try:
        backup_filename = f'backup_subscriptions_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
        shutil.copy2('subscriptions.db', backup_filename)
        
        return send_file(
            backup_filename,
            as_attachment=True,
            download_name=backup_filename,
            mimetype='application/x-sqlite3'
        )
    except Exception as e:
        flash(f'Ошибка при создании резервной копии: {str(e)}', 'danger')
        return redirect(url_for('index'))

# Восстановление из резервной копии
@app.route('/restore', methods=['GET', 'POST'])
def restore():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('Файл не выбран', 'danger')
            return redirect(request.url)
        
        file = request.files['file']
        if file.filename == '':
            flash('Файл не выбран', 'danger')
            return redirect(request.url)
        
        if file and file.filename.endswith('.db'):
            try:
                file.save('restore_backup.db')
                
                try:
                    test_conn = sqlite3.connect('restore_backup.db')
                    test_conn.cursor().execute('SELECT name FROM sqlite_master WHERE type="table"')
                    test_conn.close()
                except:
                    flash('Неверный формат файла базы данных', 'danger')
                    os.remove('restore_backup.db')
                    return redirect(request.url)
                
                backup_current = f'backup_before_restore_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
                shutil.copy2('subscriptions.db', backup_current)
                
                try:
                    os.remove('subscriptions.db')
                    shutil.copy2('restore_backup.db', 'subscriptions.db')
                    os.remove('restore_backup.db')
                    
                    flash('База данных успешно восстановлена из резервной копии!', 'success')
                    return redirect(url_for('index'))
                    
                except Exception as e:
                    if os.path.exists('subscriptions.db'):
                        os.remove('subscriptions.db')
                    shutil.copy2(backup_current, 'subscriptions.db')
                    os.remove(backup_current)
                    raise e
                    
            except Exception as e:
                flash(f'Ошибка при восстановлении базы данных: {str(e)}', 'danger')
                return redirect(request.url)
        else:
            flash('Неверный формат файла. Требуется файл .db', 'danger')
            return redirect(request.url)
    
    return render_template('restore.html')

# Страница настроек (для токена бота)
@app.route('/settings', methods=['GET', 'POST'])
def settings():
    token = get_setting('bot_token') or ''
    
    if request.method == 'POST':
        new_token = request.form['bot_token']
        if new_token:
            set_setting('bot_token', new_token)
            flash('Токен бота сохранен в БД!', 'success')
            restart_bot()  # Перезапускаем бота с новым токеном
        else:
            flash('Токен не может быть пустым.', 'danger')
        return redirect(url_for('settings'))
    
    html = '''
    <h1>Настройки Telegram-бота</h1>
    <form method="post">
        Токен бота: <input type="text" name="bot_token" value="{{ token }}" placeholder="Вставь токен от BotFather" required><br>
        <input type="submit" value="Сохранить и перезапустить бота">
    </form>
    <p><small>После сохранения бот будет перезапущен автоматически.</small></p>
    <a href="/">Назад к списку подписок</a>
    '''
    return app.jinja_env.from_string(html).render(token=token)

if __name__ == '__main__':
    init_db()
    
    # Запускаем компоненты
    start_subscription_checker()
    start_telegram_bot()
    
    # Запускаем Flask
    logging.info("Запуск Flask приложения...")
run(debug=False, host='0.0.0.0', port=5000, use_reloader=False)