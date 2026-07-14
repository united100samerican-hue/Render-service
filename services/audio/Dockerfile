FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/audio/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/audio/ .

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "10000"]
