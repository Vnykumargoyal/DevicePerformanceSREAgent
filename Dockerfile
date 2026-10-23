FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    SRE_CONFIG=/app/monitor.json \
    SRE_DATA_DIR=/var/lib/device-sre \
    SRE_HOST=0.0.0.0 \
    SRE_PORT=8765

WORKDIR /app
COPY requirements-runtime.lock ./
RUN python -m pip install --no-cache-dir -r requirements-runtime.lock \
    && groupadd --gid 10001 device-sre \
    && useradd --uid 10001 --gid device-sre --no-create-home --shell /usr/sbin/nologin device-sre \
    && install -d -o 10001 -g 10001 -m 0700 /var/lib/device-sre
COPY src/ ./src/
COPY monitor.example.json ./monitor.json

USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3).close()"]
ENTRYPOINT ["python", "-m", "device_sre"]
CMD ["serve", "--transport", "streamable-http"]
