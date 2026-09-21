#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${ROOT}/pi-release"

mkdir -p "${DEST}/ai/speaker"

if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude ".git/" \
    --exclude "__pycache__/" \
    --exclude "*.pyc" \
    --exclude ".cache/" \
    --exclude "logs/" \
    --exclude "scripts/test/" \
    --exclude "speaker.bak/" \
    --exclude "pi-release/" \
    --exclude ".env" \
    --exclude ".envrc" \
    "${ROOT}/" "${DEST}/ai/speaker/"
else
  rm -rf "${DEST}/ai/speaker"
  mkdir -p "${DEST}/ai/speaker"
  for item in "${ROOT}/"*; do
    name="$(basename "$item")"
    case "$name" in
      .git|__pycache__|.cache|logs|speaker.bak|pi-release) continue ;;
    esac
    if [[ "$name" == *.pyc ]]; then
      continue
    fi
    cp -a "$item" "${DEST}/ai/speaker/"
  done
  rm -rf "${DEST}/ai/speaker/scripts/test" || true
  rm -f "${DEST}/ai/speaker/.env" "${DEST}/ai/speaker/.envrc" || true
fi

# Replace docker symlinks with real directories for FAT boot partition copies.
if [ -L "${DEST}/ai/speaker/docker/scripts" ]; then
  rm -f "${DEST}/ai/speaker/docker/scripts"
  cp -a "${DEST}/ai/speaker/scripts" "${DEST}/ai/speaker/docker/scripts"
fi
if [ -L "${DEST}/ai/speaker/docker/wakewords" ]; then
  rm -f "${DEST}/ai/speaker/docker/wakewords"
  cp -a "${DEST}/ai/speaker/wakewords" "${DEST}/ai/speaker/docker/wakewords"
fi

echo "Pi release staged at ${DEST}/ai/speaker"
