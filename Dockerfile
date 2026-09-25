# Lightweight Python base image
FROM python:3.11-slim

# ffmpeg is required for on-the-fly remuxing/audio conversion
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=5000
EXPOSE 5000

# --workers 2 --threads 4: threads matter here because streaming responses
# hold a worker/thread open for the duration of the download, so a thread
# pool lets multiple downloads proceed concurrently without blocking.
# --timeout 300: allows longer videos time to fully stream through.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 300 app:app"]
