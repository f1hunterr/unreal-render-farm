# Unreal Render Farm master (dashboard, queue, scheduler).
# Render agents are NOT containerized: they run natively on the Windows render nodes (Unreal + GPU).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    URF_MASTER_DIR=/data \
    URF_MASTER_BIND=0.0.0.0 \
    URF_MASTER_PORT=5000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY master/ master/

RUN useradd --system --uid 10001 --no-create-home farm \
    && mkdir /data && chown farm /data
USER farm

VOLUME /data
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4)"]

CMD ["python", "master/farm_master.py"]
