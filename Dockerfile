FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

WORKDIR /app

# tesseract-ocr: OCR engine for images and scanned PDFs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 onebrain \
    && useradd --system --uid 10001 --gid onebrain --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin onebrain \
    && install -d --owner=onebrain --group=onebrain --mode=0750 /data \
    && install -d --owner=onebrain --group=onebrain --mode=0700 /tmp/onebrain

COPY requirements.txt .
RUN pip install --require-hashes --no-cache-dir -r requirements.txt

COPY app ./app
COPY deploy ./deploy
COPY migrations ./migrations
COPY alembic.ini .

ENV ONEBRAIN_DATA_DIR=/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp/onebrain \
    TMPDIR=/tmp/onebrain \
    XDG_CACHE_HOME=/tmp/onebrain/cache

USER onebrain
EXPOSE 8000

# A platform may set $PORT; the launcher defaults to API mode.
# Set ONEBRAIN_PROCESS=worker on the worker service.
CMD ["python", "-m", "app.deploy.start"]
