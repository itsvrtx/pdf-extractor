# Web deployment image. Chromium lives on the SERVER; users only need a browser.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PDFX_CHANNEL=bundled \
    PDFX_NO_SANDBOX=1 \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /app
COPY requirements-web.txt .
# --with-deps installs the OS libraries Chromium needs
RUN pip install -r requirements-web.txt && playwright install --with-deps chromium

COPY pdf_extractor.py server.py ./
COPY web ./web
COPY assets ./assets

RUN useradd -m app && mkdir -p /data && chown -R app:app /data /app
USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/healthz')"

# One worker only: job state is kept in memory. Scale up with a bigger instance, not more workers.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
