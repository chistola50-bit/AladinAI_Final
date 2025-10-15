# ✅ Базовый образ с Python 3.11, полностью совместим с Render
FROM python:3.11-slim

# Создание директории приложения
WORKDIR /app

# Копируем файлы проекта
COPY . .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Открываем порт (Render слушает переменную $PORT)
EXPOSE 10000

# Запускаем Flask сервер
CMD ["python", "web.py"]
