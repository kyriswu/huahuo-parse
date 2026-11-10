# syntax=docker/dockerfile:1.8.1@sha256:e87caa74dcb7d46cd820352bfea12591f3dba3ddc4285e19c7dcd13359f7cefd
ARG PYTHON_IMAGE=python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

FROM --platform=$BUILDPLATFORM ${PYTHON_IMAGE} AS models
ENV HF_HUB_DISABLE_TELEMETRY=1
RUN pip install --no-cache-dir huggingface-hub==1.4.1
COPY scripts/prepare_models.py /tmp/prepare_models.py
COPY licenses/Apache-2.0.txt /licenses/Apache-2.0.txt
RUN python /tmp/prepare_models.py --output /models --license-file /licenses/Apache-2.0.txt

FROM ${PYTHON_IMAGE} AS dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-linux.lock.txt /tmp/requirements-linux.lock.txt
# The hash-pinned Paddle GPU wheel and its locked NVIDIA wheels provide CUDA 11.8 / cuDNN 8.9.6.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r /tmp/requirements-linux.lock.txt \
    && /opt/venv/bin/pip check

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH PORT=4763 \
    PADDLEOCR_BACKEND=cuda MODEL_ROOT=/opt/models PRELOAD_MODEL=true \
    PADDLE_CPU_THREADS=4 CUDA_VISIBLE_DEVICES=0 \
    NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    FLAGS_allocator_strategy=auto_growth \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True PADDLE_PDX_DISABLE_DEVICE_FALLBACK=1 \
    HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 \
    STORAGE_LOCAL_ROOT=/app/var/storage
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libglib2.0-0 libgl1 libgomp1 \
       libreoffice-writer libreoffice-calc libreoffice-impress fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /app/var/storage \
    && chown -R app:app /app/var
COPY --from=dependencies /opt/venv /opt/venv
COPY --from=models /models /opt/models
COPY app/*.py /app/app/
WORKDIR /app
USER app
EXPOSE 4763
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4763/health', timeout=3)"
CMD ["python", "-m", "app.main"]
