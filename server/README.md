# Server

GPU-hosted inference and orchestration for the smart speaker. Everything runs on one Linux box on a private LAN; nothing in the request path leaves the house.

## Host

- AMD Ryzen 9 9950X, 128 GB DDR5
- NVIDIA RTX PRO 6000 Blackwell (96 GB), driver 580, CUDA 13
- Ubuntu 24.04, Docker with the NVIDIA container toolkit
- Deployed to `/ai/server` on the host. Model weights, caches, and runtime state also live under that path and are not part of this repository.

One GPU, no MIG or partitioning. The LLM, STT, and TTS containers share it, which shapes several decisions below (vLLM pinned to 85% of VRAM, a global lock in the TTS service, health-gated startup order).

## Services

| Service | Port | Runtime | Notes |
|---|---|---|---|
| `orchestrator` | 7000 gRPC, 9110 metrics, 9111 status | Python `asyncio`, `grpc.aio` | One bidirectional `Converse` stream per client. Session management, barge-in, history, tool loop. |
| `llm` | 8000 | vLLM, OpenAI-compatible API | Local Qwen3 checkpoint, 32k context, `qwen3_xml` tool-call parser, `qwen3` reasoning parser, prefix caching and chunked prefill. |
| `stt` | 50051 gRPC | NVIDIA NIM, Parakeet CTC 0.6B en-US | Streaming, VAD enabled, batch size 1. 16 kHz mono PCM in, unpunctuated text out. |
| `tts` | 9001 HTTP | FastAPI + NeMo FastPitch and HiFi-GAN | Custom single-speaker voice (model not included). Streams PCM16LE at 22.05 kHz. |
| `tools` | 9102 HTTP/2 | FastMCP, Hypercorn | 20 local tools plus an MCP proxy to upstream servers (Home Assistant). Hot reload. |
| `prometheus`, `grafana`, `loki`, `promtail`, `cadvisor`, `node-exporter`, `dcgm-exporter` | 9090, 3000, 3100, ... | | Metrics, dashboards, logs, host and GPU telemetry. |

`docker/docker-compose.yaml` brings all of it up. The orchestrator waits on `llm`, `stt`, `tts`, and `tools` being healthy, then runs its own readiness poll that names any check still failing.

## Layout

```
config/            config.yaml (orchestrator settings), prompt.md (system prompt)
docker/            compose file, Prometheus/Loki/Promtail config, Grafana provisioning
modules/
  aiserver/        gRPC server, session lifecycle, eviction, readiness, metrics endpoints
  orchestrator/    proto, settings, core/ (turn runner, history, audio tap, metrics), tools/ (MCP client)
  stt/             Parakeet gRPC streaming client
  tts/             TTS streaming client and mood presets
  tools/           tool registry (hidden tools, change detection)
  common/          logging, audio helpers
tools_server/      FastMCP app, loader with hot reload, MCP proxy, tools/<name>/{tool.py,config.yaml}
tts/               TTS service: FastAPI app, chunking, normalization, engine, Dockerfile
test/              unit tests and a text-mode chat client
scripts/           manual harnesses and container maintenance
```

## Request flow

```
Speaker ──gRPC Converse──► orchestrator :7000
                              ├──► stt   :50051   streaming transcript, partials
                              ├──► llm   :8000    streamed completion, tool calls
                              ├──► tools :9102    MCP; local tools + proxied upstream
                              └──► tts   :9001    streamed PCM
                                   └──► TtsAudio chunks back to the speaker
```

Per turn: audio frames stream to STT as they arrive; partial transcripts start a speculative LLM call; on commit the LLM streams a reply, tool calls are validated and dispatched in parallel, a second pass synthesizes the final answer from tool output, the text is sanitized for speech, and TTS audio streams back in chunks. Barge-in cancels TTS mid-stream.

## Tools

Local tools live in `tools_server/tools/<name>/` as a Python module plus a `config.yaml` carrying the description and parameter metadata the model sees. Each registers itself with `register(server) -> list[str]`.

| Tool | Backend |
|---|---|
| `weather_forecast` | OpenWeatherMap, with active alerts formatted first |
| `stock_quote` | Finnhub |
| `news_search` | NewsData |
| `current_time` | server clock, any timezone |
| `local_places_search`, `place_details`, `place_summary` | Google Places |
| `location_preferences` | per-device default location, SQLite, scoped by the gRPC-derived client id |
| `get_person_id`, `speaker_identity`, `speaker_enrollment_coach` | NeMo speaker embeddings over the turn's audio reference |
| `household_memory`, `household_profiles` | persistent facts and per-person profiles, SQLite |
| `timer_alarm`, `push_notify` | timers and alarms; ntfy push |
| `translate` | Google Translate |
| `network_status`, `server_health`, `audio_quality_guard`, `fallback_response_bank` | operational and internal |

Upstream MCP servers are declared in `tools_server/mcp-servers.yaml` with per-server allow and deny lists, header templating from environment variables, and per-tool description and schema overrides. Home Assistant is the one in use.

## Configuration

`config/config.yaml` holds orchestrator settings (queue sizes, session limits, timeouts, barge-in threshold, TTS mood presets). Component-level `config.yaml` files under `tools_server/` and `tts/config/` override it. Secrets come from the environment; see `.env.example`.

`config/prompt.md` is the system prompt. It carries the tool-routing rules the model follows, including the `[AUDIO_REF]` protocol for speaker identification and the Home Assistant naming conventions.

## Running

1. Place model weights under `/ai/server/llm/local-models/`, the TTS checkpoints under `/ai/server/tts/models/`, and let the NIM container populate `/ai/server/stt/nim/` on first start.
2. `cp .env.example docker/.env` and fill in whatever keys you want enabled.
3. `cd docker && docker compose up -d`. The LLM takes a few minutes to load; everything else gates on it.
4. Check `http://<host>:9111/readyz` and `/selfcheck`. Grafana is on `:3000`.
5. `python test/chat_client.py --orch <host>:7000` talks to the orchestrator over text without a microphone.

## Tests

`test/` uses `unittest`. Coverage is on the pure parts: TTS abbreviation expansion, place-details formatting, settings coercion, and selected orchestrator behaviors. The streaming handler, tool loop, and MCP proxy are exercised by the manual harnesses in `scripts/` and by the readiness endpoints.

## Constraints this was built under

- English only.
- Latency over throughput; single user, single tenant.
- MCP is the only tool interface. The orchestrator has no embedded tool logic.
- One GPU shared by three inference services.
