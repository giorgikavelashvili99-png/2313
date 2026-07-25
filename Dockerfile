FROM python:3.11-slim

# ffmpeg (and ffprobe, which ships alongside it) — required for server-side compression
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=5000
EXPOSE 5000

# Long timeout: compression jobs run in a background thread, but the health
# check / any synchronous request should still get a generous window.
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "--timeout", "300", "app:app"]
