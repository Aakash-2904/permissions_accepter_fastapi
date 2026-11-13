FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

COPY relay/requirements.txt relay/requirements.txt
RUN pip install --no-cache-dir -r relay/requirements.txt

COPY relay/ relay/

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# State is in memory: exactly ONE worker. Do not scale horizontally.
CMD ["sh", "-c", "uvicorn relay.app:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
