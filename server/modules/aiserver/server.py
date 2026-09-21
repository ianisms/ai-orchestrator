#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import AsyncIterator, Optional
from urllib.parse import urlsplit

modules_root = str(Path(__file__).resolve().parents[1])
project_root = str(Path(__file__).resolve().parents[2])
if modules_root not in sys.path:
    sys.path.insert(0, modules_root)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

gen_path = os.getenv("ORCH_GEN_PATH")
if gen_path:
    sys.path.insert(0, gen_path)

import grpc
import httpx

import orchestrator_pb2 as pb
import orchestrator_pb2_grpc as pbg

from config.settings import settings
from llm.llm_client import stream_chat_completions, get_system_prompt, close_llm_client
from orchestrator.core.history import ChatHistory
from orchestrator.core.metrics import (
    inc_barge_in_interrupts,
    inc_queue_drops,
    inc_session_evictions,
    inc_sessions_created,
    inc_sessions_destroyed,
    observe_barge_in_to_audio_last_seconds,
    set_queue_depth,
    set_sessions_active,
    start_metrics_server,
)
from orchestrator.core.history_store import HistoryStore
from orchestrator.core.session import OrchestratorSession
from stt.stream import STTStream
from tools.registry import list_tools, set_hidden_tools
from orchestrator.tools.mcp_client import MCPToolClient, set_tools_client
from tts.stream import close_tts_client
from common.logging import setup_logging, format_kv


_SESSION_HISTORIES: dict[str, ChatHistory] = {}
_SESSION_LAST_SEEN: dict[str, float] = {}
_SESSION_HISTORY_DEGRADED = False
_HISTORY_STORE: HistoryStore | None = None
_RESOLVED_LLM_MODEL: str | None = None
_RESOLVED_LLM_MAX_MODEL_LEN: int = 0
_RESOLVED_HISTORY_BASE_TOKENS: int = 0
_STATUS_HTTPD: ThreadingHTTPServer | None = None
_SERVER_PROTO_PATH: str = ""
_SERVER_PROTO_SHA256: str = ""


def _safe_int(value) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _select_model_entry(models: list[dict], preferred_model: str) -> tuple[str, int]:
    preferred = str(preferred_model or "").strip()
    selected: dict | None = None

    if preferred:
        for item in models:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "").strip()
            item_root = str(item.get("root") or "").strip()
            if preferred == item_id or preferred == item_root:
                selected = item
                break

    if selected is None:
        for item in models:
            if isinstance(item, dict) and str(item.get("id") or "").strip():
                selected = item
                break

    if not isinstance(selected, dict):
        return "", 0

    model_id = str(selected.get("id") or "").strip()
    max_len = _safe_int(selected.get("max_model_len"))
    return model_id, max_len


def _resolve_startup_history_budget(max_model_len: int) -> tuple[int, int, int, int]:
    configured = max(0, _safe_int(settings.LLM_HISTORY_TOKENS))
    model_ctx = max(0, _safe_int(max_model_len))
    max_gen = max(0, _safe_int(settings.LLM_MAX_TOKENS))

    if model_ctx <= 0:
        # Could not detect model context; keep configured value.
        return configured, configured, 0, max_gen

    if max_gen <= 0:
        # If generation max isn't configured, reserve ~10% for completion and tool overhead.
        max_gen = max(256, model_ctx // 10)

    reserve = max(1024, max_gen + 512)
    cap = max(256, model_ctx - reserve)
    effective = cap if configured <= 0 else min(configured, cap)
    return effective, cap, reserve, max_gen


async def _auto_detect_llm_model_info(base_url: str, logger, preferred_model: str = "") -> tuple[str, int]:
    url = base_url.rstrip("/") + "/v1/models"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        models = data.get("data") if isinstance(data, dict) else None
        if isinstance(models, list):
            model_id, max_len = _select_model_entry(models, preferred_model)
            if model_id:
                return model_id, max_len
    except Exception as exc:
        logger.warning("LLM model autodetect failed: %s", exc)
    return "", 0


async def _warmup_llm(
    base_url: str,
    model: str,
    delay_s: float,
    logger,
    *,
    tools_client: MCPToolClient | None = None,
) -> None:
    if delay_s > 0:
        await asyncio.sleep(delay_s)
    if tools_client is not None:
        ready = await tools_client.wait_ready(timeout=10.0)
        if not ready:
            logger.warning("LLM warmup skipped: tools client not ready")
            return
    tools_payload = list_tools()
    for idx in range(3):
        try:
            chunks: list[str] = []
            async for delta in stream_chat_completions(
                base_url,
                model,
                "hello, testing comms",
                cancel=None,
                temperature=0.0,
                top_p=float(settings.LLM_TOP_P),
                messages=None,
                tools=tools_payload if tools_payload else None,
            ):
                if isinstance(delta, str) and delta:
                    chunks.append(delta)
                if len("".join(chunks)) >= 64:
                    break
            text = "".join(chunks).strip()
            logger.debug("LLM warmup #%d result chars=%d", idx + 1, len(text or ""))
            break
        except Exception as exc:
            logger.warning("LLM warmup #%d failed: %s", idx + 1, exc)


def _first_oneof_name(msg) -> Optional[str]:
    oneofs = list(msg.DESCRIPTOR.oneofs)
    return oneofs[0].name if oneofs else None


def _peer_to_client_key(peer: str) -> str:
    raw = str(peer or "").strip()
    if not raw:
        return "unknown"
    if raw.startswith("ipv4:"):
        host_port = raw[len("ipv4:") :]
        host = host_port.rsplit(":", 1)[0] if ":" in host_port else host_port
        return host or "unknown"
    if raw.startswith("ipv6:"):
        host_port = raw[len("ipv6:") :]
        if host_port.startswith("[") and "]" in host_port:
            return host_port[1 : host_port.find("]")] or "unknown"
        return host_port.rsplit(":", 1)[0] if ":" in host_port else (host_port or "unknown")
    return raw


def _sanitize_client_key(value: str, *, max_len: int = 128) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    allowed = "._:-"
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in allowed)
    if not cleaned:
        return ""
    return cleaned[:max_len]


def _client_key_from_context(md: dict[str, str], peer: str) -> tuple[str, str]:
    for header in ("x-client-uuid", "x-client-id"):
        value = _sanitize_client_key(md.get(header, ""))
        if value:
            return value, header
    return _peer_to_client_key(peer), "peer"


def _touch_session(session_id: str) -> None:
    if not session_id:
        return
    _SESSION_LAST_SEEN[session_id] = time.time()
    set_sessions_active(len(_SESSION_HISTORIES))


def _evict_sessions(logger) -> None:
    global _SESSION_HISTORY_DEGRADED
    ttl_s = max(0.0, float(settings.ORCH_SESSION_TTL_S))
    max_sessions = max(1, int(settings.ORCH_SESSION_MAX))
    degrade_threshold = min(1.0, max(0.1, float(settings.ORCH_SESSION_DEGRADE_THRESHOLD)))
    hard_evict_factor = max(1.0, float(settings.ORCH_SESSION_HARD_EVICT_FACTOR))
    now = time.time()

    ttl_evict: set[str] = set()
    max_evict: set[str] = set()
    if ttl_s > 0:
        for sid, last_seen in list(_SESSION_LAST_SEEN.items()):
            if now - float(last_seen) > ttl_s:
                ttl_evict.add(sid)

    active_after_ttl = len(_SESSION_LAST_SEEN) - len(ttl_evict)
    degrade_at = int(max_sessions * degrade_threshold)
    _SESSION_HISTORY_DEGRADED = active_after_ttl >= max(1, degrade_at)
    hard_cap = int(max_sessions * hard_evict_factor)

    # Prefer degraded-memory mode over aggressive evictions; only hard-evict above hard cap.
    if active_after_ttl > max(1, hard_cap):
        keep = max_sessions
        ordered = sorted(_SESSION_LAST_SEEN.items(), key=lambda item: float(item[1]), reverse=True)
        for sid, _ in ordered[keep:]:
            if sid not in ttl_evict:
                max_evict.add(sid)

    to_evict = ttl_evict | max_evict

    if not to_evict:
        return

    for sid in to_evict:
        _SESSION_HISTORIES.pop(sid, None)
        _SESSION_LAST_SEEN.pop(sid, None)
        if _HISTORY_STORE is not None:
            try:
                _HISTORY_STORE.delete(sid)
            except Exception as exc:
                log.warning("history delete failed for session %s: %r", sid, exc)
    if ttl_evict:
        inc_session_evictions("ttl", len(ttl_evict))
        inc_sessions_destroyed("ttl", len(ttl_evict))
    if max_evict:
        inc_session_evictions("max_sessions", len(max_evict))
        inc_sessions_destroyed("max_sessions", len(max_evict))
    set_sessions_active(len(_SESSION_HISTORIES))
    logger.info("evicted session histories count=%d", len(to_evict))


def _effective_history_budget_tokens() -> int:
    normal = max(0, int(_RESOLVED_HISTORY_BASE_TOKENS or 0))
    if not _SESSION_HISTORY_DEGRADED:
        return normal
    degraded = max(0, int(settings.ORCH_HISTORY_DEGRADED_TOKENS))
    return min(normal, degraded) if normal > 0 else degraded


async def _wait_for_readiness(logger, readiness_provider) -> None:
    wait_s = max(0.0, float(settings.ORCH_READINESS_WAIT_S))
    poll_s = max(0.2, float(settings.ORCH_READINESS_POLL_S))
    if wait_s <= 0:
        return
    deadline = time.monotonic() + wait_s
    while True:
        payload = readiness_provider()
        if payload.get("ready"):
            logger.info("readiness checks green")
            return
        checks = payload.get("checks", {}) if isinstance(payload, dict) else {}
        failed = [k for k, ok in checks.items() if not bool(ok)]
        logger.warning("readiness pending failed_checks=%s", ",".join(failed) if failed else "unknown")
        if time.monotonic() >= deadline:
            logger.warning("readiness wait timed out after %.1fs; continuing startup", wait_s)
            return
        await asyncio.sleep(poll_s)


def _path_meta(path_str: str) -> dict:
    p = Path(path_str)
    if not p.exists():
        return {"exists": False}
    try:
        raw = p.read_bytes()
        h = hashlib.sha256(raw).hexdigest()[:16]
    except Exception:
        h = ""
    return {
        "exists": True,
        "size": p.stat().st_size,
        "mtime": int(p.stat().st_mtime),
        "sha256_16": h,
    }


def _build_self_check_payload(*, tools_connected: bool) -> dict:
    return {
        "status": "ok",
        "time_unix": int(time.time()),
        "resolved_model": _RESOLVED_LLM_MODEL or "",
        "resolved_model_max_context": int(_RESOLVED_LLM_MAX_MODEL_LEN or 0),
        "orchestrator_proto": {
            "path": _SERVER_PROTO_PATH,
            "sha256": _SERVER_PROTO_SHA256,
        },
        "config": {
            "llm_base_url": settings.LLM_BASE_URL,
            "tts_base_url": settings.TTS_BASE_URL,
            "stt_grpc_target": settings.STT_GRPC_TARGET,
            "tools_mcp_url": settings.TOOLS_MCP_URL,
            "metrics_port": int(settings.ORCH_METRICS_PORT),
            "status_port": int(settings.ORCH_STATUS_PORT),
        },
        "prompt": _path_meta(settings.LLM_PROMPT_PATH),
        "sessions": {
            "active": len(_SESSION_HISTORIES),
            "tracked": len(_SESSION_LAST_SEEN),
            "history_degraded_mode": bool(_SESSION_HISTORY_DEGRADED),
            "effective_history_tokens": int(_effective_history_budget_tokens()),
        },
        "tools_connected": bool(tools_connected),
    }


def _parse_host_port_from_url(url: str, default_port: int) -> tuple[str, int]:
    try:
        parts = urlsplit(url)
    except Exception:
        return "", 0
    host = parts.hostname or ""
    port = int(parts.port or default_port)
    return host, port


def _parse_host_port_from_target(target: str) -> tuple[str, int]:
    raw = (target or "").strip()
    if not raw:
        return "", 0
    # Support "host:port" and "[::1]:50051"
    if raw.startswith("[") and "]" in raw:
        host = raw[1 : raw.find("]")]
        rest = raw[raw.find("]") + 1 :]
        if rest.startswith(":"):
            try:
                return host, int(rest[1:])
            except ValueError:
                return host, 0
        return host, 0
    if ":" in raw:
        host, port = raw.rsplit(":", 1)
        try:
            return host, int(port)
        except ValueError:
            return host, 0
    return raw, 0


def _tcp_reachable(host: str, port: int, timeout_s: float = 0.5) -> bool:
    if not host or port <= 0:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except Exception:
        return False


def _build_readiness_payload(*, tools_connected: bool, model_resolved: bool) -> dict:
    llm_host, llm_port = _parse_host_port_from_url(settings.LLM_BASE_URL, 8000)
    tts_host, tts_port = _parse_host_port_from_url(settings.TTS_BASE_URL, 9001)
    stt_host, stt_port = _parse_host_port_from_target(settings.STT_GRPC_TARGET)

    llm_ok = _tcp_reachable(llm_host, llm_port)
    tts_ok = _tcp_reachable(tts_host, tts_port)
    stt_ok = _tcp_reachable(stt_host, stt_port)
    tools_ok = bool(tools_connected)
    model_ok = bool(model_resolved)

    ready = all([llm_ok, tts_ok, stt_ok, tools_ok, model_ok])
    return {
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "checks": {
            "llm_reachable": llm_ok,
            "tts_reachable": tts_ok,
            "stt_reachable": stt_ok,
            "tools_connected": tools_ok,
            "llm_model_resolved": model_ok,
        },
    }


def _start_status_server(port: int, provider, readiness_provider, logger) -> ThreadingHTTPServer:
    class _Handler(BaseHTTPRequestHandler):
        def _write_json(self, payload: dict, code: int = 200) -> None:
            raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                self._write_json({"status": "ok"})
                return
            if self.path == "/readyz":
                payload = readiness_provider()
                self._write_json(payload, code=200 if payload.get("ready") else 503)
                return
            if self.path == "/selfcheck":
                self._write_json(provider())
                return
            self._write_json({"status": "not_found"}, code=404)

        def log_message(self, _format, *_args):
            return

    httpd = ThreadingHTTPServer(("0.0.0.0", int(port)), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, name="orch_status_http", daemon=True)
    thread.start()
    logger.info("orchestrator status HTTP listening on 0.0.0.0:%s", port)
    return httpd


class OrchestratorService(pbg.OrchestratorServicer):
    async def Converse(self, request_iter, context) -> AsyncIterator[pb.ServerMessage]:
        log = setup_logging(settings.LOG_DIR, settings.LOG_LEVEL)
        session_id = ""
        last_turn_id = ""

        # Proto compatibility handshake via gRPC metadata.
        md: dict[str, str] = {}
        client_proto_sha = ""
        try:
            for k, v in context.invocation_metadata():
                key = str(k or "").strip().lower()
                if key:
                    md[key] = str(v or "").strip()
            client_proto_sha = md.get("x-orch-proto-sha256", "").lower()
        except Exception:
            client_proto_sha = ""
        server_proto_sha = str(_SERVER_PROTO_SHA256 or "").lower()
        proto_mismatch = bool(client_proto_sha and server_proto_sha and client_proto_sha != server_proto_sha)
        try:
            initial_md = [
                ("x-orch-proto-sha256", server_proto_sha),
                ("x-orch-proto-mismatch", "1" if proto_mismatch else "0"),
            ]
            # send_initial_metadata only accepts non-empty keys/values
            initial_md = [(k, v) for (k, v) in initial_md if k and v is not None]
            if initial_md:
                await context.send_initial_metadata(initial_md)
        except Exception:
            pass
        if proto_mismatch:
            log.warning(
                "proto checksum mismatch client=%s server=%s path=%s",
                client_proto_sha,
                server_proto_sha,
                _SERVER_PROTO_PATH or "unknown",
            )
        client_key, client_key_source = _client_key_from_context(md, context.peer())

        audio_q: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=int(settings.ORCH_INQ_AUDIO_MAX))
        text_q: asyncio.Queue[str] = asyncio.Queue(maxsize=int(settings.ORCH_INQ_TEXT_MAX))
        out_q: asyncio.Queue[pb.ServerMessage] = asyncio.Queue(maxsize=int(settings.ORCH_OUTQ_MAX))
        set_queue_depth("audio_in", audio_q.qsize())
        set_queue_depth("text_in", text_q.qsize())
        set_queue_depth("out_q", out_q.qsize())

        # defaults, overridden by Start
        sample_rate_hz = int(settings.DEFAULT_SAMPLE_RATE_HZ)
        request_partials = True
        language_code = "en-US"
        no_tts = False
        start_seen = asyncio.Event()
        eou_event = asyncio.Event()
        client_done = asyncio.Event()
        interrupt_event = asyncio.Event()
        current_phase = "IDLE"
        barge_in_started_at: dict[str, float] = {}

        drop_counts = {"audio_in": 0, "text_in": 0, "out_q": 0}
        log_streaming = bool(settings.ORCH_LOG_STREAMING)
        try:
            stream_every = max(1, int(settings.ORCH_LOG_STREAM_EVERY_N))
        except Exception:
            stream_every = 50
        stream_counts = {"audio": 0, "transcript": 0, "reply": 0, "ingest_audio": 0}

        def _log(level: str, msg: str, *, turn_id: str = "", **optional) -> None:
            trace_id = f"{session_id}:{turn_id}" if session_id and turn_id else (turn_id or session_id)
            suffix = format_kv(
                {"session_id": session_id, "turn_id": turn_id, "trace_id": trace_id},
                optional,
                enabled=settings.ORCH_LOG_KV,
            )
            getattr(log, level)(f"{msg}{suffix}")

        def _maybe_log_stream(kind: str, msg: str, *, turn_id: str = "", **optional) -> None:
            if not log_streaming:
                return
            stream_counts[kind] = stream_counts.get(kind, 0) + 1
            count = stream_counts[kind]
            if count <= 3 or count % stream_every == 0:
                _log("debug", msg, turn_id=turn_id, **optional)

        def _record_drop(queue_name: str, *, turn_id: str = "", **optional) -> None:
            drop_counts[queue_name] += 1
            inc_queue_drops(queue_name, 1)
            count = drop_counts[queue_name]
            if count == 1 or count % 50 == 0:
                _log("warning", "queue drop", turn_id=turn_id, queue=queue_name, dropped=count, **optional)

        async def _out_q_put(msg: pb.ServerMessage, *, msg_kind: str, turn_id: str = "") -> None:
            for _ in range(out_q.maxsize + 1):
                try:
                    out_q.put_nowait(msg)
                    set_queue_depth("out_q", out_q.qsize())
                    return
                except asyncio.QueueFull:
                    dropped_kind = ""
                    try:
                        dropped = out_q.get_nowait()
                        dropped_kind = dropped.WhichOneof("msg") or ""
                    except asyncio.QueueEmpty:
                        pass
                    set_queue_depth("out_q", out_q.qsize())
                    _record_drop("out_q", turn_id=turn_id, msg_kind=msg_kind, dropped_kind=dropped_kind)
            _record_drop("out_q", turn_id=turn_id, msg_kind=msg_kind, dropped_kind="overflow")

        def _put_audio_frame(frame: Optional[bytes], *, turn_id: str, label: str) -> None:
            try:
                audio_q.put_nowait(frame)
                set_queue_depth("audio_in", audio_q.qsize())
            except asyncio.QueueFull:
                try:
                    _ = audio_q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                set_queue_depth("audio_in", audio_q.qsize())
                try:
                    audio_q.put_nowait(frame)
                    set_queue_depth("audio_in", audio_q.qsize())
                except asyncio.QueueFull:
                    pass
                _record_drop("audio_in", turn_id=turn_id, label=label, bytes=len(frame or b""))

        def _put_text(text: str, *, turn_id: str) -> None:
            try:
                text_q.put_nowait(text)
                set_queue_depth("text_in", text_q.qsize())
            except asyncio.QueueFull:
                try:
                    _ = text_q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                set_queue_depth("text_in", text_q.qsize())
                try:
                    text_q.put_nowait(text)
                    set_queue_depth("text_in", text_q.qsize())
                except asyncio.QueueFull:
                    pass
                _record_drop("text_in", turn_id=turn_id, size=len(text))

        _log(
            "info",
            "client connected",
            client_key=client_key,
            client_key_source=client_key_source,
            peer=context.peer(),
        )


        async def emit_state(phase: str, turn_id: str = ""):
            nonlocal current_phase
            current_phase = phase
            enum_val = getattr(pb.State.Phase, phase, pb.State.PHASE_UNSPECIFIED)
            msg = pb.ServerMessage(state=pb.State(phase=enum_val, turn_id=turn_id))
            await _out_q_put(msg, msg_kind="state", turn_id=turn_id)

        async def emit_transcript(turn_id: str, is_final: bool, text: str):
            msg = pb.ServerMessage(transcript=pb.Transcript(
                turn_id=turn_id, is_final=is_final, text=text
            ))
            await _out_q_put(msg, msg_kind="transcript", turn_id=turn_id)

        async def emit_audio(turn_id: str, sr: int, pcm: bytes, is_last: bool):
            msg = pb.ServerMessage(audio=pb.TtsAudio(
                turn_id=turn_id, sample_rate_hz=sr, pcm_s16le=pcm, is_last=is_last
            ))
            await _out_q_put(msg, msg_kind="audio", turn_id=turn_id)
            if is_last and turn_id and turn_id in barge_in_started_at:
                started = barge_in_started_at.pop(turn_id)
                observe_barge_in_to_audio_last_seconds(time.monotonic() - started)

        async def emit_reply(turn_id: str, text: str, is_final: bool):
            msg = pb.ServerMessage(reply=pb.Reply(
                turn_id=turn_id, text=text, is_final=is_final
            ))
            await _out_q_put(msg, msg_kind="reply", turn_id=turn_id)

        async def send(event_type: str, payload: dict):
            if event_type == "state":
                await emit_state(payload["phase"], payload.get("turn_id", ""))
                return
            if event_type == "transcript":
                await emit_transcript(payload.get("turn_id", ""), bool(payload["is_final"]), str(payload["text"]))
                return
            if event_type == "audio":
                sr = int(payload.get("sample_rate_hz") or 0) or 22050
                await emit_audio(payload.get("turn_id", ""), sr, payload.get("pcm", b"") or b"", bool(payload.get("is_last", False)))
                return
            if event_type == "reply":
                await emit_reply(payload.get("turn_id", ""), str(payload.get("text", "")), bool(payload.get("is_final", False)))
                return
            if event_type == "error":
                msg = pb.ServerMessage(error=pb.Error(turn_id="", message=str(payload.get("message", "unknown error"))))
                await _out_q_put(msg, msg_kind="error", turn_id="")
                return

            msg = pb.ServerMessage(error=pb.Error(turn_id="", message=f"unknown event {event_type}"))
            await _out_q_put(msg, msg_kind="error", turn_id="")

        stt = STTStream(
            settings.STT_GRPC_TARGET,
            logger=log,
            grpc_timeout_s=settings.STT_GRPC_TIMEOUT_S,
            req_timeout_s=settings.STT_REQ_TIMEOUT_S,
        )
        pipeline = OrchestratorSession(
            stt=stt,
            llm_base_url=settings.LLM_BASE_URL,
            llm_model=_RESOLVED_LLM_MODEL or "",
            tts_base_url=settings.TTS_BASE_URL,
            batch_max_chars=settings.BATCH_MAX_CHARS,
            batch_max_ms=settings.BATCH_MAX_MS,
            logger=log,
        )
        pipeline.set_client_key(client_key)

        async def ingest():
            nonlocal sample_rate_hz, request_partials, language_code, session_id, last_turn_id, no_tts
            oneof_name = None
            audio_frames = 0
            try:
                async for msg in request_iter:
                    if oneof_name is None:
                        oneof_name = _first_oneof_name(msg)
                        _log("info", "client message stream", oneof=oneof_name)

                    which = msg.WhichOneof(oneof_name) if oneof_name else None

                    if which == "start":
                        sample_rate_hz = int(msg.start.sample_rate_hz or settings.DEFAULT_SAMPLE_RATE_HZ)
                        request_partials = bool(msg.start.request_partials)
                        language_code = (getattr(msg.start, "locale", None) or "en-US")
                        session_id = getattr(msg.start, "session_id", "") or ""
                        last_turn_id = getattr(msg.start, "turn_id", "") or ""
                        reset_context = bool(getattr(msg.start, "reset_context", False))
                        no_tts = bool(getattr(msg.start, "no_tts", False))
                        pipeline.set_session_id(session_id)
                        _touch_session(session_id)
                        _evict_sessions(log)
                        history = None
                        if session_id:
                            budget_tokens = _effective_history_budget_tokens()
                            history = _SESSION_HISTORIES.get(session_id)
                            if history is None:
                                history = ChatHistory(budget_tokens)
                                if _HISTORY_STORE is not None:
                                    try:
                                        loaded = await asyncio.wait_for(
                                            asyncio.get_running_loop().run_in_executor(
                                                None, _HISTORY_STORE.load, session_id
                                            ),
                                            timeout=5.0,
                                        )
                                    except asyncio.TimeoutError:
                                        _log("warning", "history load timed out; starting fresh")
                                        loaded = None
                                    if loaded:
                                        history.set_messages(loaded)
                                        _log("info", "history loaded", count=len(loaded))
                                    history.set_on_change(
                                        lambda messages, sid=session_id: _HISTORY_STORE.save(sid, messages)
                                    )
                                else:
                                    history.set_on_change(None)
                                _SESSION_HISTORIES[session_id] = history
                                inc_sessions_created(1)
                                set_sessions_active(len(_SESSION_HISTORIES))
                            else:
                                history.set_token_budget(budget_tokens)
                                if _HISTORY_STORE is not None:
                                    history.set_on_change(
                                        lambda messages, sid=session_id: _HISTORY_STORE.save(sid, messages)
                                    )
                                else:
                                    history.set_on_change(None)
                            pipeline.set_history(history)
                            _touch_session(session_id)
                        if reset_context:
                            if session_id:
                                _SESSION_HISTORIES[session_id].reset()
                                if _HISTORY_STORE is not None:
                                    _HISTORY_STORE.delete(session_id)
                            else:
                                pipeline.reset_context()
                            _log("info", "context reset", turn_id=last_turn_id)
                        history_count = len(history.snapshot()) if history is not None else 0
                        _log(
                            "info",
                            "start received",
                            turn_id=last_turn_id,
                            sample_rate_hz=sample_rate_hz,
                            request_partials=request_partials,
                            locale=language_code,
                            no_tts=no_tts,
                            reset_context=reset_context,
                            history_messages=history_count,
                            history_budget_tokens=_effective_history_budget_tokens(),
                            history_degraded=_SESSION_HISTORY_DEGRADED,
                        )
                        start_seen.set()
                        # Surface LISTENING state with the provided turn_id (if any)
                        await emit_state("LISTENING", last_turn_id)

                    elif which == "audio":
                        pcm = msg.audio.pcm_s16le
                        if pcm:
                            if (
                                current_phase == "SPEAKING"
                                and len(pcm) >= int(settings.ORCH_BARGE_IN_MIN_BYTES)
                                and not interrupt_event.is_set()
                            ):
                                interrupt_event.set()
                                inc_barge_in_interrupts()
                                if last_turn_id:
                                    barge_in_started_at[last_turn_id] = time.monotonic()
                                _log("info", "barge-in detected", turn_id=last_turn_id, bytes=len(pcm))
                            _put_audio_frame(pcm, turn_id=last_turn_id, label="audio")
                            audio_frames += 1
                            _maybe_log_stream(
                                "ingest_audio",
                                "ingest audio frame",
                                turn_id=last_turn_id,
                                idx=audio_frames,
                                bytes=len(pcm),
                            )

                    elif which == "eou":
                        _log("info", "EOU received", turn_id=last_turn_id)
                        eou_event.set()
                        # Use last_turn_id if available
                        if last_turn_id:
                            await emit_state("SPEAKING", last_turn_id)
                        _put_audio_frame(None, turn_id=last_turn_id, label="eou")

                    elif which == "cancel":
                        _log("warning", "client cancel", reason=getattr(msg.cancel, "reason", ""))
                        if not interrupt_event.is_set():
                            inc_barge_in_interrupts("cancel")
                        interrupt_event.set()
                        eou_event.set()
                        _put_audio_frame(None, turn_id=last_turn_id, label="cancel")
                        break
                    elif which == "text":
                        text = (getattr(msg.text, "text", None) or "").strip()
                        if text:
                            _log("debug", "client text input", turn_id=last_turn_id, text=text)
                            _put_text(text, turn_id=last_turn_id)

                    else:
                        _log("warning", "unknown client message", kind=which)

            except Exception as e:
                log.exception(
                    "ingest crashed%s",
                    format_kv(
                        {"session_id": session_id, "turn_id": last_turn_id},
                        {"error": repr(e)},
                        enabled=settings.ORCH_LOG_KV,
                    ),
                    exc_info=e,
                )
            finally:
                client_done.set()
                eou_event.set()
                _put_audio_frame(None, turn_id=last_turn_id, label="client_done")
                _log("info", "ingest finished", turn_id=last_turn_id, frames=audio_frames)


        async def run_pipeline():
            try:
                await asyncio.wait_for(start_seen.wait(), timeout=float(settings.ORCH_START_TIMEOUT_S))
            except asyncio.TimeoutError:
                _log(
                    "warning",
                    "no Start received; using defaults",
                    sample_rate_hz=sample_rate_hz,
                    request_partials=request_partials,
                    locale=language_code,
                )

            # Interactive mode: keep handling turns until client closes.
            await pipeline.run_session(
                client_in_q=audio_q,
                send=send,
                sample_rate_hz=sample_rate_hz,
                request_partials=request_partials,
                language_code=language_code,
                eou_event=eou_event,
                client_done=client_done,
                text_in_q=text_q,
                no_tts=no_tts,
                interrupt_event=interrupt_event,
            )

        ingest_task = asyncio.create_task(ingest(), name="ingest")
        pipeline_task = asyncio.create_task(run_pipeline(), name="pipeline")
        cancel_signal_sent = False
        last_out_at = time.monotonic()

        try:
            # Keep streaming until pipeline finishes AND we drained output.
            while True:
                now = time.monotonic()
                idle_s = float(settings.ORCH_PIPELINE_IDLE_TIMEOUT_S)
                if context.cancelled():
                    # Client half-closed send; keep draining any queued audio so TTS isn't cut off.
                    if not cancel_signal_sent:
                        cancel_signal_sent = True
                        client_done.set()
                        if not interrupt_event.is_set():
                            inc_barge_in_interrupts("context_cancel")
                        interrupt_event.set()
                        eou_event.set()
                        _put_audio_frame(None, turn_id=last_turn_id, label="context_cancel")
                    if out_q.empty() and pipeline_task.done():
                        _log("info", "context cancelled; no more output; closing")
                        break
                    else:
                        _log("info", "context cancelled; draining pending output")
                    if idle_s > 0 and now - last_out_at >= idle_s and not pipeline_task.done():
                        _log("warning", "context cancelled; cancelling pipeline")
                        pipeline_task.cancel()

                # If pipeline is done, surface exceptions immediately
                if pipeline_task.done():
                    if pipeline_task.cancelled():
                        _log("warning", "pipeline cancelled")
                    else:
                        exc = pipeline_task.exception()
                        if exc:
                            log.exception(
                                "pipeline crashed%s",
                                format_kv(
                                    {"session_id": session_id, "turn_id": ""},
                                    {"error": repr(exc)},
                                    enabled=settings.ORCH_LOG_KV,
                                ),
                                exc_info=exc,
                            )
                            yield pb.ServerMessage(error=pb.Error(turn_id="", message=f"pipeline crashed: {exc!r}"))
                    # Drain anything already queued (best effort)
                    while not out_q.empty():
                        msg = out_q.get_nowait()
                        set_queue_depth("out_q", out_q.qsize())
                        if msg.HasField("audio"):
                            a = msg.audio
                            _maybe_log_stream(
                                "audio",
                                "streaming audio",
                                turn_id=getattr(a, "turn_id", ""),
                                bytes=len(getattr(a, "pcm_s16le", b"") or b""),
                                sample_rate_hz=getattr(a, "sample_rate_hz", 0),
                                is_last=getattr(a, "is_last", False),
                            )
                        elif msg.HasField("transcript"):
                            t = msg.transcript
                            _maybe_log_stream(
                                "transcript",
                                "streaming transcript",
                                turn_id=getattr(t, "turn_id", ""),
                                is_final=getattr(t, "is_final", False),
                                text=getattr(t, "text", ""),
                            )
                        elif msg.HasField("reply"):
                            r = msg.reply
                            _maybe_log_stream(
                                "reply",
                                "streaming reply",
                                turn_id=getattr(r, "turn_id", ""),
                                is_final=getattr(r, "is_final", False),
                                text=getattr(r, "text", ""),
                            )
                        yield msg
                        last_out_at = time.monotonic()
                    break

                # Otherwise stream whatever is available
                try:
                    msg = await asyncio.wait_for(out_q.get(), timeout=0.1)
                    set_queue_depth("out_q", out_q.qsize())
                    if msg.HasField("audio"):
                        a = msg.audio
                        _maybe_log_stream(
                            "audio",
                            "streaming audio",
                            turn_id=getattr(a, "turn_id", ""),
                            bytes=len(getattr(a, "pcm_s16le", b"") or b""),
                            sample_rate_hz=getattr(a, "sample_rate_hz", 0),
                            is_last=getattr(a, "is_last", False),
                        )
                    elif msg.HasField("transcript"):
                        t = msg.transcript
                        _maybe_log_stream(
                            "transcript",
                            "streaming transcript",
                            turn_id=getattr(t, "turn_id", ""),
                            is_final=getattr(t, "is_final", False),
                            text=getattr(t, "text", ""),
                        )
                    elif msg.HasField("reply"):
                        r = msg.reply
                        _maybe_log_stream(
                            "reply",
                            "streaming reply",
                            turn_id=getattr(r, "turn_id", ""),
                            is_final=getattr(r, "is_final", False),
                            text=getattr(r, "text", ""),
                        )
                    yield msg
                    last_out_at = time.monotonic()
                except asyncio.TimeoutError:
                    pass
        finally:
            for t in (ingest_task, pipeline_task):
                if not t.done():
                    t.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(ingest_task, pipeline_task, return_exceptions=True),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                _log("warning", "tasks did not finish within shutdown timeout")
            await stt.close()
            _log("info", "client disconnected")


async def main():
    log = setup_logging(settings.LOG_DIR, settings.LOG_LEVEL)
    config_path = os.getenv("ORCH_CONFIG_PATH", "/ai/server/config/config.yaml")
    component_config_path = os.getenv(
        "ORCH_COMPONENT_CONFIG_PATH",
        "/ai/server/modules/orchestrator/config/config.yaml",
    )
    log.info("config path: %s", config_path)
    log.info("component config path: %s", component_config_path)
    log.info("config ORCH_GRPC_BIND=%s", settings.ORCH_GRPC_BIND)
    log.info("config STT_GRPC_TARGET=%s", settings.STT_GRPC_TARGET)
    log.info("config LLM_BASE_URL=%s", settings.LLM_BASE_URL)
    resolved_llm_model, resolved_llm_ctx = await _auto_detect_llm_model_info(
        settings.LLM_BASE_URL,
        log,
        preferred_model=str(settings.LLM_MODEL or ""),
    )
    if settings.LLM_MODEL and resolved_llm_model and resolved_llm_model != settings.LLM_MODEL:
        log.warning(
            "configured LLM_MODEL=%s not found in /v1/models; using %s",
            settings.LLM_MODEL,
            resolved_llm_model,
        )
    if resolved_llm_model:
        log.info("config LLM_MODEL=%s", resolved_llm_model)
        if resolved_llm_ctx > 0:
            log.info("config LLM_MAX_MODEL_LEN=%s", resolved_llm_ctx)
    else:
        log.error("LLM model is not configured and auto-detect failed; set LLM_MODEL or ensure /v1/models works")
        raise SystemExit(1)
    global _RESOLVED_LLM_MODEL, _RESOLVED_LLM_MAX_MODEL_LEN, _RESOLVED_HISTORY_BASE_TOKENS
    _RESOLVED_LLM_MODEL = resolved_llm_model
    _RESOLVED_LLM_MAX_MODEL_LEN = max(0, int(resolved_llm_ctx or 0))
    history_base, history_cap, reserve, max_gen = _resolve_startup_history_budget(_RESOLVED_LLM_MAX_MODEL_LEN)
    _RESOLVED_HISTORY_BASE_TOKENS = history_base
    log.info(
        "startup history budget resolved configured=%s effective=%s cap=%s model_ctx=%s reserve=%s max_gen=%s",
        settings.LLM_HISTORY_TOKENS,
        _RESOLVED_HISTORY_BASE_TOKENS,
        history_cap,
        _RESOLVED_LLM_MAX_MODEL_LEN,
        reserve,
        max_gen,
    )
    global _SERVER_PROTO_PATH, _SERVER_PROTO_SHA256
    proto_path = (Path(__file__).resolve().parents[1] / "orchestrator" / "orchestrator.proto").resolve()
    _SERVER_PROTO_PATH = str(proto_path)
    _SERVER_PROTO_SHA256 = _file_sha256(proto_path)
    if _SERVER_PROTO_SHA256:
        log.info("orchestrator proto checksum path=%s sha256=%s", _SERVER_PROTO_PATH, _SERVER_PROTO_SHA256)
    else:
        log.warning("orchestrator proto checksum unavailable path=%s", _SERVER_PROTO_PATH)
    log.info("config TTS_BASE_URL=%s", settings.TTS_BASE_URL)
    log.info("config TOOLS_MCP_URL=%s", settings.TOOLS_MCP_URL)
    log.info("config LLM_PROMPT_PATH=%s", settings.LLM_PROMPT_PATH)
    log.info("config DEFAULT_SAMPLE_RATE_HZ=%s", settings.DEFAULT_SAMPLE_RATE_HZ)
    log.info("config ENERGY_BARGE_IN=%s", settings.ENERGY_BARGE_IN)
    log.info("config ORCH_BARGE_IN_MIN_BYTES=%s", settings.ORCH_BARGE_IN_MIN_BYTES)
    log.info("config STT_EOS_IDLE_MS=%s", settings.STT_EOS_IDLE_MS)
    log.info("config BATCH_MAX_CHARS=%s", settings.BATCH_MAX_CHARS)
    log.info("config BATCH_MAX_MS=%s", settings.BATCH_MAX_MS)
    log.info("config TTS_EXPAND=%s", settings.TTS_EXPAND)
    log.info("config TTS_SENTENCE_PAUSE_MS=%s", settings.TTS_SENTENCE_PAUSE_MS)
    log.info("config TTS_CROSSFADE_MS=%s", settings.TTS_CROSSFADE_MS)
    log.info("config TTS_MIN_CHUNK_CHARS=%s", settings.TTS_MIN_CHUNK_CHARS)
    log.info("config TTS_MIN_CHUNK_WORDS=%s", settings.TTS_MIN_CHUNK_WORDS)
    log.info("config TTS_HIGHPASS_HZ=%s", settings.TTS_HIGHPASS_HZ)
    log.info("config TTS_TARGET_PEAK=%s", settings.TTS_TARGET_PEAK)
    log.info("config TTS_MAX_GAIN=%s", settings.TTS_MAX_GAIN)
    log.info("config TTS_FADE_MS=%s", settings.TTS_FADE_MS)
    log.info("config TTS_TAIL_MS=%s", settings.TTS_TAIL_MS)
    log.info("config TTS_PITCH_SEMITONES=%s", settings.TTS_PITCH_SEMITONES)
    log.info("config TTS_SPEAKING_RATE=%s", settings.TTS_SPEAKING_RATE)
    log.info("config TTS_MOOD=%s", settings.TTS_MOOD)
    if isinstance(settings.TTS_MOODS, dict) and settings.TTS_MOODS:
        log.info("config TTS_MOODS=%s", ", ".join(sorted(settings.TTS_MOODS.keys())))
    log.info("config LOG_DIR=%s", settings.LOG_DIR)
    log.info("config LOG_LEVEL=%s", settings.LOG_LEVEL)
    log.info("config ORCH_METRICS_PORT=%s", settings.ORCH_METRICS_PORT)
    log.info("config ORCH_STATUS_ENABLED=%s", settings.ORCH_STATUS_ENABLED)
    log.info("config ORCH_STATUS_PORT=%s", settings.ORCH_STATUS_PORT)
    log.info("config ORCH_SHUTDOWN_GRACE_S=%s", settings.ORCH_SHUTDOWN_GRACE_S)
    log.info("config ORCH_START_TIMEOUT_S=%s", settings.ORCH_START_TIMEOUT_S)
    log.info("config ORCH_OUTQ_MAX=%s", settings.ORCH_OUTQ_MAX)
    log.info("config ORCH_INQ_AUDIO_MAX=%s", settings.ORCH_INQ_AUDIO_MAX)
    log.info("config ORCH_INQ_TEXT_MAX=%s", settings.ORCH_INQ_TEXT_MAX)
    log.info("config ORCH_PIPELINE_IDLE_TIMEOUT_S=%s", settings.ORCH_PIPELINE_IDLE_TIMEOUT_S)
    log.info("config ORCH_TURN_MAX_S=%s", settings.ORCH_TURN_MAX_S)
    log.info("config ORCH_LOG_KV=%s", settings.ORCH_LOG_KV)
    log.info("config ORCH_LOG_STREAMING=%s", settings.ORCH_LOG_STREAMING)
    log.info("config ORCH_LOG_STREAM_EVERY_N=%s", settings.ORCH_LOG_STREAM_EVERY_N)
    log.info("config ENABLE_STORED_HISTORY=%s", settings.ENABLE_STORED_HISTORY)
    log.info("config ORCH_HISTORY_DB=%s", settings.ORCH_HISTORY_DB)
    log.info("config ORCH_SESSION_TTL_S=%s", settings.ORCH_SESSION_TTL_S)
    log.info("config ORCH_SESSION_MAX=%s", settings.ORCH_SESSION_MAX)
    log.info("config ORCH_SESSION_DEGRADE_THRESHOLD=%s", settings.ORCH_SESSION_DEGRADE_THRESHOLD)
    log.info("config ORCH_SESSION_HARD_EVICT_FACTOR=%s", settings.ORCH_SESSION_HARD_EVICT_FACTOR)
    log.info("config ORCH_HISTORY_DEGRADED_TOKENS=%s", settings.ORCH_HISTORY_DEGRADED_TOKENS)
    log.info("config ORCH_TOOL_PARALLELISM=%s", settings.ORCH_TOOL_PARALLELISM)
    log.info("config STT_GRPC_TIMEOUT_S=%s", settings.STT_GRPC_TIMEOUT_S)
    log.info("config STT_REQ_TIMEOUT_S=%s", settings.STT_REQ_TIMEOUT_S)
    log.info("config ORCH_READINESS_WAIT_S=%s", settings.ORCH_READINESS_WAIT_S)
    log.info("config ORCH_READINESS_POLL_S=%s", settings.ORCH_READINESS_POLL_S)
    log.info("config LLM_IDLE_TIMEOUT_S=%s", settings.LLM_IDLE_TIMEOUT_S)
    log.info("config LLM_TEMPERATURE=%s", settings.LLM_TEMPERATURE)
    log.info("config LLM_TOP_P=%s", settings.LLM_TOP_P)
    log.info("config LLM_HISTORY_TOKENS=%s", settings.LLM_HISTORY_TOKENS)
    log.info("config LLM_WARMUP_ENABLED=%s", settings.LLM_WARMUP_ENABLED)
    log.info("config LLM_TOOL_DESCRIPTIONS_MAX=%s", settings.LLM_TOOL_DESCRIPTIONS_MAX)
    log.info("config TTS_HTTP_TIMEOUT_S=%s", settings.TTS_HTTP_TIMEOUT_S)
    def _log_discovered_tools(label: str) -> None:
        try:
            tool_names = []
            for tool in list_tools():
                if not isinstance(tool, dict):
                    continue
                fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
                name = fn.get("name") if fn else tool.get("name")
                if isinstance(name, str) and name:
                    tool_names.append(name)
            if tool_names:
                log.info("%s count=%d tools=%s", label, len(tool_names), ", ".join(sorted(tool_names)))
            else:
                log.warning("%s count=0", label)
        except Exception as exc:
            log.warning("%s failed: %s", label, exc)

    prompt_path = Path(settings.LLM_PROMPT_PATH)
    if not prompt_path.exists():
        log.info("config prompt: <missing>")
    else:
        prompt_text = get_system_prompt().strip()
        if prompt_text:
            log.info("config prompt: %s", prompt_text)
        else:
            log.info("config prompt: <empty>")

    history_db = str(settings.ORCH_HISTORY_DB or "").strip()
    if settings.ENABLE_STORED_HISTORY and history_db:
        global _HISTORY_STORE
        _HISTORY_STORE = HistoryStore(history_db, logger=log)
        log.info("history persistence enabled db=%s", history_db)
    elif settings.ENABLE_STORED_HISTORY:
        log.info("history persistence requested but ORCH_HISTORY_DB missing; history disabled")
    else:
        log.info("history persistence disabled (ENABLE_STORED_HISTORY=false)")

    set_hidden_tools({
        "audio_quality_guard",
        "speaker_enrollment_coach",
        "context_cache",
        "fallback_response_bank",
    })
    tools_client = MCPToolClient(settings.TOOLS_MCP_URL, logger=log)
    tools_client.start()
    set_tools_client(tools_client)

    if settings.LLM_WARMUP_ENABLED:
        try:
            delay_ms = int(settings.LLM_WARMUP_DELAY_MS)
        except Exception:
            delay_ms = 0
        delay_s = max(0.0, delay_ms / 1000.0)
        asyncio.create_task(
            _warmup_llm(
                settings.LLM_BASE_URL,
                _RESOLVED_LLM_MODEL,
                delay_s,
                log,
                tools_client=tools_client,
            ),
            name="llm_warmup",
        )

    start_metrics_server(int(settings.ORCH_METRICS_PORT))
    def _readiness_provider() -> dict:
        connected = bool(getattr(tools_client, "_session", None) is not None)
        return _build_readiness_payload(
            tools_connected=connected,
            model_resolved=bool(_RESOLVED_LLM_MODEL),
        )

    global _STATUS_HTTPD
    if bool(settings.ORCH_STATUS_ENABLED):
        def _status_provider() -> dict:
            connected = bool(getattr(tools_client, "_session", None) is not None)
            return _build_self_check_payload(tools_connected=connected)

        _STATUS_HTTPD = _start_status_server(
            int(settings.ORCH_STATUS_PORT),
            _status_provider,
            _readiness_provider,
            log,
        )

    # Startup diagnostics: log failed readiness checks until green or timeout.
    await _wait_for_readiness(log, _readiness_provider)
    try:
        refreshed = await asyncio.wait_for(tools_client.refresh_tools(timeout_s=8.0), timeout=10.0)
        if not refreshed:
            log.warning("startup tools refresh did not complete; continuing with current tool registry")
    except asyncio.TimeoutError:
        log.warning("startup tools refresh timed out; continuing with current tool registry")
    except Exception as exc:
        log.warning("startup tools refresh failed: %s", exc)
    _log_discovered_tools("startup tools discovered")

    server = grpc.aio.server(options=[
        ("grpc.max_receive_message_length", 32 * 1024 * 1024),
        ("grpc.max_send_message_length", 32 * 1024 * 1024),
        ("grpc.max_concurrent_rpcs", int(settings.ORCH_SESSION_MAX)),
    ])

    pbg.add_OrchestratorServicer_to_server(OrchestratorService(), server)

    bind = settings.ORCH_GRPC_BIND
    server.add_insecure_port(bind)

    stop_event = asyncio.Event()

    def _request_stop(signame: str):
        log.info("shutdown signal received: %s", signame)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop, sig.name)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _request_stop(sig.name))

    await server.start()
    log.info("orchestrator gRPC listening on %s", bind)

    stop_task = asyncio.create_task(stop_event.wait(), name="stop_event")
    term_task = asyncio.create_task(server.wait_for_termination(), name="server_termination")
    cancelled = False
    try:
        await asyncio.wait({stop_task, term_task}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        cancelled = True
        log.info("shutdown requested (task cancelled)")
    finally:
        for task in (stop_task, term_task):
            if not task.done():
                task.cancel()
        await tools_client.stop()
        if _STATUS_HTTPD is not None:
            try:
                _STATUS_HTTPD.shutdown()
                _STATUS_HTTPD.server_close()
            except Exception:
                log.exception("orchestrator status HTTP stop failed")
            finally:
                _STATUS_HTTPD = None
        grace_s = float(settings.ORCH_SHUTDOWN_GRACE_S)
        log.info("stopping gRPC server (grace=%ss)", grace_s)
        try:
            await asyncio.shield(server.stop(grace=grace_s))
            log.info("orchestrator gRPC stopped")
        except asyncio.CancelledError:
            cancelled = True
            # Hot-reload can cancel shutdown; suppress noisy tracebacks.
            log.info("orchestrator gRPC stop cancelled during shutdown")
        except Exception:
            log.exception("orchestrator gRPC stop failed")
        await close_llm_client()
        await close_tts_client()
        if cancelled:
            task = asyncio.current_task()
            if task is not None and hasattr(task, "uncancel"):
                task.uncancel()


if __name__ == "__main__":
    asyncio.run(main())
