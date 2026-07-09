FROM python:3.12-slim

WORKDIR /app

# tesseract-ocr: OCR engine for images and scanned PDFs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini .

ENV ONEBRAIN_DATA_DIR=/data
EXPOSE 8000

# Railway/Heroku set $PORT; the launcher defaults to API mode.
# Set ONEBRAIN_PROCESS=worker on the worker service.
CMD ["python", "-m", "app.deploy.start"]
