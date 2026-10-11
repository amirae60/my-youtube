FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DATA_DIR=/data \
    IN_DOCKER=1

WORKDIR /app
COPY index.html sw.js manifest.webmanifest server.py ./
COPY icons ./icons

# Runs as root so the ./data folder mounted from the host is always writable
RUN mkdir -p /data

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/ping' % os.environ.get('PORT','8000'), timeout=3)" || exit 1

CMD ["python", "server.py"]
