#!/usr/bin/env bash
set -euo pipefail

# Create symlinks to docker json logs for the core services so they are easy to read from host.
# Usage: ./link_logs.sh

LOG_DIR=/ai/server/logs
mkdir -p "${LOG_DIR}"

services=(llm stt tts orchestrator)

for svc in "${services[@]}"; do
  cid="$(docker ps -aqf "name=^${svc}$" | head -n1 || true)"
  if [[ -z "${cid}" ]]; then
    echo "warn: container not running (skipping): ${svc}"
    continue
  fi

  json_log="/var/lib/docker/containers/${cid}/${cid}-json.log"
  if [[ ! -f "${json_log}" ]]; then
    echo "warn: no json log found for ${svc} (expected ${json_log})"
    continue
  fi

  ln -sf "${json_log}" "${LOG_DIR}/${svc}.log"
  echo "linked ${svc} -> ${LOG_DIR}/${svc}.log"
done

echo "Done. Tail with: tail -f /ai/server/logs/<svc>.log"
