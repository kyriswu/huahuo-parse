FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS base

FROM base AS nltk-data
# Pin the upstream data revision and verify both archives before extraction.
ADD --checksum=sha256:51c3078994aeaf650bfc8e028be4fb42b4a0d177d41c012b6a983979653660ec https://cdn.jsdelivr.net/gh/nltk/nltk_data@550b6625bcef1f2abff2ff770a5a0d272c9c6b2a/packages/tokenizers/punkt.zip /tmp/punkt.zip
ADD --checksum=sha256:e1f13cf2532daadfd6f3bc481a49859f0b8ea6432ccdcd83e6a49a5f19008de9 https://cdn.jsdelivr.net/gh/nltk/nltk_data@550b6625bcef1f2abff2ff770a5a0d272c9c6b2a/packages/taggers/averaged_perceptron_tagger.zip /tmp/tagger.zip
RUN python -m zipfile -e /tmp/punkt.zip /nltk_data/tokenizers \
    && python -m zipfile -e /tmp/tagger.zip /nltk_data/taggers

FROM base AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NLTK_DATA=/usr/local/share/nltk_data \
    PORT=4763

WORKDIR /app

ARG DEBIAN_MIRROR=https://deb.debian.org
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libmagic1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock.txt ./
# Unstructured imports PDF helpers (including OpenCV) even for fast mode.
# Install the locked core dependencies without inference, OCR or model extras.
ARG PYPI_INDEX_URL=https://pypi.org/simple
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url "$PYPI_INDEX_URL" -r requirements.txt \
    && pip check

COPY --from=nltk-data /nltk_data/ /usr/local/share/nltk_data/
RUN python -c "import nltk; nltk.sent_tokenize('Docker fast parsing works.'); nltk.pos_tag(['Docker'])"

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/var/storage \
    && chown -R app:app /app/var

COPY app ./app
USER app

EXPOSE 4763
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '4763') + '/health', timeout=2)"

CMD ["python", "-m", "app.main"]
