# Базовый образ: Python 3.10 (на основе вашей среды)
FROM python:3.10-slim

# Установите рабочую директорию в контейнере
WORKDIR /app

# Скопируйте requirements.txt и установите зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Скопируйте весь код проекта
COPY . .

# Укажите порт для Flask (5009 из вашего кода)
EXPOSE 5009

# Команда для запуска приложения
CMD ["python", "subscriptions_service.py"]
