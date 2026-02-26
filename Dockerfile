FROM python:3.12-slim

WORKDIR /code

# System deps (lxml needs libxml2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY data/ ./data/

# DB lives on /tmp (or override with DB_PATH env)
ENV DB_PATH=/tmp/sessions.db
ENV PORT=8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
