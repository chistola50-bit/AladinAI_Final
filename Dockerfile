# Используем официальный Python-образ
FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файлы проекта
COPY . /app

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Открываем порт
EXPOSE 10000

# Переменная окружения для Flask
ENV PORT=10000

# Запуск через gunicorn (надёжный сервер)
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "web:app"]
