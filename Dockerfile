FROM python:3.13-slim

# ffmpeg merges separate video and audio tracks; the app degrades gracefully
# without it, but more sites download in one piece when it is present.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=12000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 12000
# One worker with threads: the app keeps token state in process memory.
# Bind to $PORT when the host provides one, else 12000.
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 8 --timeout 300 --bind 0.0.0.0:${PORT:-12000} wsgi:app"]
