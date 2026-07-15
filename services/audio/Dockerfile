FROM python:3.11-slim
WORKDIR /app
COPY services/audio/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY services/audio /app
ENV PORT=10000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "10000"]