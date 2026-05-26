FROM python:3.12-slim

WORKDIR /code

# System deps (lxml needs libxml2; opencv-python-headless is GUI-free so no libgl1).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Banner QA detection (CRAFT via easyocr) — heavy (~2GB with torch), so
# kept in its own layer for cache locality. The /banner page works once
# this is installed; if it's omitted the route still 200s but detection
# falls back to whole-image bbox.
RUN pip install --no-cache-dir easyocr

COPY app/ ./app/
COPY data/ ./data/

# DB lives on /tmp (or override with DB_PATH env)
ENV DB_PATH=/tmp/sessions.db
ENV PORT=8080
# Banner QA defaults
ENV BANNER_DEFAULT_THRESHOLD=0.30
ENV BANNER_RUN_ROOT=/tmp/banner_qa_runs

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
