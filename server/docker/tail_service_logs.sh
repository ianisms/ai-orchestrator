#!/usr/bin/env bash
set -euo pipefail

# Tail llm and stt container logs to host files under /ai/server/logs.
# Similar to orchestrator/tts file logs, but without modifying the NIM images.

LOG_DIR="/ai/server/logs"
mkdir -p "${LOG_DIR}"

services=(llm stt)

for svc in "${services[@]}"; do
  cid="$(docker ps -qf "name=^${svc}$" | head -n1 || true)"
  if [[ -z "${cid}" ]]; then
    echo "skip ${svc}: not running"
    continue
  fi

  outfile="${LOG_DIR}/${svc}.log"
  pidfile="${LOG_DIR}/${svc}.log.pid"

  # Stop any previous tail for this service
  if [[ -f "${pidfile}" ]]; then
    oldpid="$(cat "${pidfile}" || true)"
    if [[ -n "${oldpid}" ]] && ps -p "${oldpid}" >/dev/null 2>&1; then
      kill "${oldpid}" >/dev/null 2>&1 || true
    fi
    rm -f "${pidfile}"
  fi

  echo "tailing ${svc} logs to ${outfile}"
  nohup docker logs -f "${cid}" >> "${outfile}" 2>&1 &
  echo $! > "${pidfile}"
done

echo "Done. Use tail -f ${LOG_DIR}/llm.log (or stt.log) to view. To stop a tail, kill the PID in the matching .pid file."
