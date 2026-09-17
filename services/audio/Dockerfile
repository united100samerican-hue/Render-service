FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DENO_INSTALL=/usr/local \
    POT_PROVIDER_HOME=/opt/bgutil-ytdlp-pot-provider

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        tar \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Pin Deno to a known-good 2.x release instead of the moving latest tag.
RUN curl -fsSL https://deno.land/install.sh | sh -s v2.9.5 \
    && /usr/local/bin/deno --version

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY provider /tmp/audio-provider
RUN chmod +x /tmp/audio-provider/install.sh \
    && /tmp/audio-provider/install.sh \
    && rm -rf /tmp/audio-provider

COPY . .
RUN chmod +x /app/entrypoint.sh

EXPOSE 10000

ENTRYPOINT ["/app/entrypoint.sh"]
