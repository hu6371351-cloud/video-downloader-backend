# Use an official lightweight Python image
FROM python:3.11-slim

# Install ffmpeg (needed by yt-dlp to merge video/audio streams)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory inside the container
WORKDIR /app

# Copy dependency list first (for faster rebuilds) and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the backend code
COPY . .

# Render provides the PORT environment variable at runtime
ENV PORT=5000
EXPOSE 5000

# Start the Flask app using gunicorn (production-ready WSGI server)
# --timeout 300 gives long video downloads/merges up to 5 minutes before being killed
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --timeout 300 --workers 2 server:app"]
