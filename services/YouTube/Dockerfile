FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/YouTube/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/YouTube /app/services/YouTube

CMD ["uvicorn","services.YouTube.app:app","--host","0.0.0.0","--port","10000"]