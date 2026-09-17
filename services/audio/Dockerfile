# Use the provider's official prebuilt Deno image so the POT server is not
# rebuilt with a second package manager/runtime during the Audio image build.
FROM brainicism/bgutil-ytdlp-pot-provider:2.0.0-deno AS bgutil_provider

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POT_PROVIDER_HOME=/opt/bgutil-ytdlp-pot-provider \
    POT_PROVIDER_PORT=4416 \
    DENO_NO_PROMPT=1 \
    DENO_NO_UPDATE_CHECK=1 \
    DENO_DIR=/opt/bgutil-ytdlp-pot-provider/.cache/deno

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        libstdc++6 \
        libgcc-s1 \
    && rm -rf /var/lib/apt/lists/*

# Copy the exact Deno runtime and prebuilt provider dependencies from the
# provider's official 2.0.0 image.
COPY --from=bgutil_provider /usr/bin/deno /usr/local/bin/deno
COPY --from=bgutil_provider /app /opt/bgutil-ytdlp-pot-provider

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .
RUN chmod +x /app/entrypoint.sh

EXPOSE 10000

ENTRYPOINT ["/app/entrypoint.sh"]
