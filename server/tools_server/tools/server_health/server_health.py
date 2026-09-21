"""server_health — simple "is everything up?" health check tool.

Checks reachability of STT, LLM, and TTS services via HTTP/TCP.
Reports GPU memory if nvidia-smi is available.
Returns a short TTS-friendly summary.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
from typing import Any, List
from urllib.parse import urlsplit

import httpx

from utils.tool_config import load_tool_config
from utils.tool_logging import log_tool_call, log_tool_result

_TIMEOUT = 3.0  # seconds per check


def _load_config() -> dict[str, Any]:
    return load_tool_config(__file__)


def _tcp_ok(host: str, port: int, timeout: float = _TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _parse_host_port(url: str, default_port: int) -> tuple[str, int]:
    try:
        parts = urlsplit(url if "://" in url else f"http://{url}")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or default_port
        return host, port
    except Exception:
        return "127.0.0.1", default_port


def _http_ok(url: str) -> bool:
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.get(url)
            return r.status_code < 500
    except Exception:
        return False


def _gpu_summary() -> str | None:
    """Return a brief GPU memory line or None if nvidia-smi is unavailable."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            timeout=3,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return None
    lines = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3:
            name, used, total = parts
            try:
                pct = int(round(100 * int(used) / int(total)))
                lines.append(f"{name}: {pct}% VRAM used ({used}/{total} MiB)")
            except Exception:
                lines.append(line)
    return "; ".join(lines) if lines else None


def register(server) -> List[str]:
    log = logging.getLogger("tools.server_health")
    config = _load_config()

    llm_url = str(config.get("llm_url") or os.getenv("LLM_BASE_URL") or "http://127.0.0.1:8000")
    tts_url = str(config.get("tts_url") or os.getenv("TTS_BASE_URL") or "http://127.0.0.1:9001")
    stt_target = str(config.get("stt_target") or os.getenv("STT_GRPC_TARGET") or "127.0.0.1:50051")

    @server.tool(
        name="server_health",
        description=(
            "Check whether all AI backend services are reachable and healthy. "
            "Use when the user asks if the server is up, something seems slow or broken, "
            "or they ask about system status."
        ),
    )
    def server_health() -> str:
        log_tool_call(log, "server_health")

        issues: list[str] = []
        ok_parts: list[str] = []

        # LLM
        llm_host, llm_port = _parse_host_port(llm_url, 8000)
        if _tcp_ok(llm_host, llm_port):
            ok_parts.append("LLM")
        else:
            issues.append("LLM is unreachable")

        # TTS
        tts_host, tts_port = _parse_host_port(tts_url, 9001)
        if _tcp_ok(tts_host, tts_port):
            ok_parts.append("TTS")
        else:
            issues.append("TTS is unreachable")

        # STT (gRPC, TCP check)
        stt_host, stt_port = _parse_host_port(stt_target, 50051)
        if _tcp_ok(stt_host, stt_port):
            ok_parts.append("STT")
        else:
            issues.append("STT is unreachable")

        # GPU
        gpu = _gpu_summary()

        if not issues:
            msg = "All systems are healthy."
            if gpu:
                msg += f" GPU: {gpu}."
        else:
            msg = "Warning: " + "; ".join(issues) + "."
            if ok_parts:
                msg += f" {', '.join(ok_parts)} are up."
            if gpu:
                msg += f" GPU: {gpu}."

        return log_tool_result(log, "server_health", msg)

    return ["server_health"]
