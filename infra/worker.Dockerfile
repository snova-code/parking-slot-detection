# infra/worker.Dockerfile
# Built from the repository root:
#   docker build -f infra/worker.Dockerfile -t apms-worker .
#
# python:3.12-slim, not alpine: onnxruntime and opencv ship manylinux wheels
# and musl would mean compiling both. Alpine would be smaller and would cost
# a day.
FROM python:3.12-slim

# opencv-python-headless still needs these two at runtime. It does NOT need
# libgl1 or any X library — that is the point of the headless wheel, and the
# worker must not be able to open a window (02_ARCHITECTURE.md).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 worker

WORKDIR /app
COPY worker/requirements.txt /app/worker/requirements.txt
RUN pip install --no-cache-dir -r /app/worker/requirements.txt

COPY worker /app/worker

# Model cache and the outbox. Declared so an unmounted run still works and
# loses only the cache, not the code's assumptions.
ENV APMS_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data && chown worker:worker /data
VOLUME ["/data"]

USER worker
ENTRYPOINT ["python", "-m", "worker.main"]