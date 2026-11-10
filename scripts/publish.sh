#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${DOCKERHUB_REPO:?Set DOCKERHUB_REPO to the public namespace/repository}"
VERSION_TAG="${VERSION_TAG:-vl16-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short HEAD)}"
IMAGE="${DOCKERHUB_REPO}:${VERSION_TAG}"
mkdir -p var/release
docker buildx build --platform linux/amd64 --tag "$IMAGE" \
  --metadata-file var/release/build-metadata.json --push .
"${PYTHON:-.venv/bin/python}" scripts/verify_release.py "$IMAGE" --build-metadata var/release/build-metadata.json
