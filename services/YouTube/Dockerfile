FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates ffmpeg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/YouTube/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/__init__.py /app/services/__init__.py
COPY services/YouTube /app/services/YouTube

RUN node --version && ffmpeg -version >/dev/null 2>&1 && python -c "import yt_dlp,yt_dlp_ejs;print('yt-dlp',yt_dlp.version.__version__);print('yt-dlp-ejs ok')"

CMD ["uvicorn","services.YouTube.app:app","--host","0.0.0.0","--port","10000"]
