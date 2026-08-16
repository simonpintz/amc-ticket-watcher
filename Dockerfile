FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY watcher.py .

# State file lives in the container's writable layer; on most platforms (Railway,
# Fly.io) this resets on redeploy, which just means you might get re-notified -
# harmless. Mount a volume at /app if you want it to persist across deploys.
CMD ["python", "watcher.py"]
