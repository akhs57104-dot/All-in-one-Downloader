FROM python:3.13-slim

# ffmpeg merges separate video and audio tracks; the app degrades gracefully
# without it, but more sites download in one piece when it is present.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Normalise permissions and run as a non-root user. Some hosts (Railway, Fly)
# run the container as an unprivileged uid; a 0600 file from a checkout would
# otherwise fail with "Permission denied" while gunicorn boots the worker.
RUN chmod -R a+rX /app \
    && chmod +x /app/start.sh /app/run.sh \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser
ENV HOME=/home/appuser

EXPOSE 12000
# One worker with threads: the app keeps token state in process memory.
# start.sh binds to $PORT (falling back to 12000) and only passes
# --no-control-socket when the installed gunicorn supports it.
CMD ["./start.sh"]
