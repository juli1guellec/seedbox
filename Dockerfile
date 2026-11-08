FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache tzdata curl ca-certificates && \
    pip install --no-cache-dir requests

ENV TZ=Europe/Paris \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DATA_DIR=/data

RUN mkdir -p /data

COPY seedbox_pilot.py /app/seedbox_pilot.py

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:8080/healthz || exit 1

CMD ["python3", "/app/seedbox_pilot.py"]
