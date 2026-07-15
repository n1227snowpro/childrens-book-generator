FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data/uploads /app/data/books

ENV PORT=9005
ENV DATA_DIR=/app/data

EXPOSE 9005

CMD ["gunicorn", "--worker-class", "gthread", "--workers", "2", "--threads", "8", "--timeout", "600", "--bind", "0.0.0.0:9005", "app:app"]
