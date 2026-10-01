# Small, current Python base
FROM python:3.12-slim

WORKDIR /app

# Install deps first so Docker caches this layer when only code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (the HTML UI is baked into the image and served as a route)
COPY server.py epg_engine.py iptv-checker.html ./

# Data (playlist.m3u, epg.xml, json, aliases, backups) lives on a mounted
# volume at /data so it survives rebuilds and container restarts.
ENV IPTV_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8766

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8766"]
