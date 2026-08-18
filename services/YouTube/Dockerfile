FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY services/YouTube/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY services/__init__.py /app/services/__init__.py
COPY services/YouTube /app/services/YouTube
EXPOSE 10000
CMD ["sh","-c","uvicorn services.YouTube.app:app --host 0.0.0.0 --port ${PORT:-10000}"]
