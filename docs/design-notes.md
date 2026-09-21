# Design notes

Decisions visible in the code, with where to find them. Written for someone reviewing the repository rather than running it.

## Orchestrator (`server/modules/`)

**Wire protocol.** One RPC, `Converse(stream ClientMessage) returns (stream ServerMessage)`, defined in `orchestrator/orchestrator.proto`. Client messages are `Start`, `AudioFrame`, `EndOfUtterance`, `Cancel`, `TextInput`. Server messages are `State` (LISTENING, THINKING, SPEAKING, IDLE), `Transcript`, `TtsAudio`, `Reply`, `Error`. The text path exists so the pipeline can be exercised without a microphone (`test/chat_client.py`).

**Concurrency.** Pure `asyncio` on `grpc.aio`. Each `Converse` call runs an ingest task, a pipeline task, and an output drain. Anything blocking (SQLite writes, WAV encoding, stats file writes) goes to an executor. Threads appear only for the status HTTP server and the SQLite connection lock. `aiserver/server.py`.

**Turn state machine.** `orchestrator/core/utterance.py` races the STT iterator, a text-input queue, and a timeout with `asyncio.wait(FIRST_COMPLETED)`. Five commit paths, each logged with its reason. The idle timeout is dynamic: short once partial text exists, long before it.

**Speculative LLM prefetch.** On each non-final partial transcript the runner starts a full LLM round and cancels any prior one. At commit the result is reused only if the prefetch text equals the committed text exactly. This spends GPU time to buy time-to-first-audio. `_start_prefetch`, `_think_and_speak`.

**Turn watchdog.** The whole think-and-speak phase runs under `asyncio.wait_for`; on expiry it emits `Error`, forces `IDLE`, and increments a counter. No turn can wedge a session.

**Barge-in.** While the phase is `SPEAKING`, any inbound audio frame of at least one 20 ms frame sets `interrupt_event`. That same event is the cancel token handed to the TTS stream, so the `async for` over TTS chunks breaks on the next chunk. `Cancel` messages and gRPC context cancellation feed the same event with distinct metric labels. Latency from detection to last audio chunk is histogrammed.

**History.** In-memory message list with a chars-per-token heuristic; trimming walks backwards and stops at the first message that does not fit, so history stays a contiguous tail. Leading non-user messages are stripped so the model never sees an orphaned tool result. Persistence is SQLite in WAL mode with an upsert per change, wired through an `on_change` callback. `orchestrator/core/history.py`, `history_store.py`.

**Degrade before evict.** Sessions expire on TTL. Above 85% of `ORCH_SESSION_MAX` every session's history budget drops to `ORCH_HISTORY_DEGRADED_TOKENS`; only above 1.25x the max does LRU eviction start. `_evict_sessions`, `_effective_history_budget_tokens` in `aiserver/server.py`.

**Budget negotiation.** At startup the server queries `/v1/models`, prefers the configured model, falls back with a warning, and exits non-zero if nothing works. The history budget is derived from the model's `max_model_len` minus a generation reserve; config can only lower it. `_resolve_startup_history_budget`.

**Audio tap.** `orchestrator/core/audio_tap.py`. `AudioTapQueue` duck-types `asyncio.Queue` and records every frame the STT client consumes. On commit it writes a WAV and returns `(session_id, audio_id)`, which is injected into the prompt as `[AUDIO_REF ...]` immediately before the final user turn. Tools receive the reference, not the audio. Files are swept on a 300 s TTL.

**Tool-call loop.** `_collect_llm_with_tools` in `utterance.py`:

1. Parser tolerance for three output shapes: Qwen `<tool_call>` XML blocks, bare JSON, and `[name]{args}`. Names resolve case-insensitively against the registry; unknown names are dropped.
2. Local JSON Schema validation (`required`, types including unions, `enum`, `additionalProperties: false`, array items) before any dispatch. Failures go back to the model as text.
3. At most three tool rounds per turn.
4. Calls within a round run under `asyncio.gather` with a semaphore, then are re-sorted to original order so the message log is deterministic.
5. After tools run, a system message tells the model to treat tool output as the sole source of truth and ignore persona, and a second completion runs at temperature 0.
6. If a places tool ran and the synthesized prose lost the address, the raw tool output is returned instead.

**Rules before the model.** Regex intent classifiers decide whether a query needs a location; a pending-location state machine expands a short follow-up like a city name into the original query. Explicit set/get/clear-default phrasings never reach the LLM. `_maybe_resolve_location_query`.

**TTS sanitization.** Markdown stripped (links keep label text), URLs and special tokens removed, degree, percent, currency, and math symbols expanded to words, then a Unicode-category whitelist. Abbreviation groups are additive; the `address` group is enabled only on turns where a places tool ran. `_sanitize_tts_text`, `_expand_context`.

**Backpressure.** Three bounded queues with distinct policies. `audio_in` and `text_in` drop oldest. `out_q` evicts oldest and records which `oneof` kind was dropped. Drops are logged on the first and every fiftieth occurrence. Shutdown gathers tasks under a 15 s timeout.

**Metrics.** `orchestrator/core/metrics.py` exposes Prometheus on `:9110` plus a JSON stats file for lifetime counters. Every emitter is individually try/except-wrapped so instrumentation cannot break a turn. A separate status server on `:9111` serves `/healthz`, `/readyz` (503 until every dependency is green), and `/selfcheck` (resolved model, context length, proto hash, prompt hash, session and degradation state).

**Proto drift guard.** The server hashes its `.proto` at startup and exchanges it as `x-orch-proto-sha256` metadata; a mismatch is logged with `x-orch-proto-mismatch` in the reply metadata.

**MCP client.** `orchestrator/tools/mcp_client.py` is a supervised reconnect loop. The session is assigned only after `initialize()` returns, so concurrent callers see `None` and get a graceful error rather than a crash. It subscribes to `ToolListChangedNotification` and refreshes off the notification path. `call_tool` hand-builds the JSON-RPC request to avoid the SDK's extra `list_tools` round trip, and falls back to a throwaway session on timeout.

**Hidden tools.** `modules/tools/registry.py` keeps a `_HIDDEN` set: tools omitted from the schema the LLM sees but still callable from orchestrator code. Four internal tools use this.

## Tools server (`server/tools_server/`)

**Discovery.** `loader.py` scans `tools/` for modules or packages exposing `register(server) -> list[str]`. Tool descriptions and per-parameter metadata live in a `config.yaml` beside each tool (`utils/tool_meta.py`), so prompt text is data.

**Hot reload.** `watchfiles` maps a changed path back to its owning module, unregisters that module's previously returned names, reloads, and re-registers. Any change to the tool set pushes a raw `ToolListChangedNotification` down every live transport (`app.py`), which is exactly what the orchestrator's client listens for.

**MCP proxy.** `mcp_proxy.py` aggregates upstream MCP servers declared in `mcp-servers.yaml`.

- `MethodStreamableHTTPTransport` subclasses the SDK transport to use `POST` for the SSE stream (Home Assistant requires it), with `last-event-id` resumption and a bounded reconnect counter.
- Upstream tools are namespaced `server__tool`; collisions are skipped with a warning.
- Per-server `allowed_tools` and `denied_tools`, deny evaluated first. Empty allow means all.
- `overrides.tools.<name>.{description,params}` rewrites an upstream tool's description and schema without touching the upstream server. Provenance is stamped into `meta._source`.
- Each proxy tool carries a signature over description, schemas, annotations, and meta; refresh only re-registers on change.
- A 60 s ping loop; a failed ping unwinds the connection, clears that server's tools, and retries after 10 s.

**Identity.** `tools/get_person_id/` resolves in three tiers: session cache hit, NeMo speaker embedding against enrolled profiles at a 0.78 cosine threshold, or a sentinel instruction telling the model to ask the speaker's name and call again with `stated_name`, which enrolls the sample. The embedding call runs in a single-worker pool with a hard 10 s deadline because NeMo can hang. The model is shared with the sibling `speaker_identity` tool through `sys.modules` rather than loaded twice into VRAM.

**Tools return prose.** Weather, news, and places tools format spoken English, not JSON, because the consumer is a TTS engine. Weather alerts are formatted first, in coordination with the system prompt.

## TTS service (`server/tts/server/`)

FastAPI, one streaming endpoint returning `audio/L16` with the sample rate in a header. Pipeline: normalize (NeMo text normalizer under a 150 ms budget with raw-text fallback) then optional abbreviation expansion with protected-phrase sentinels, then sentence chunking with backwards merge of short fragments, then FastPitch and HiFi-GAN under `torch.no_grad()`, then a DSP chain (DC removal, pitch shift, time stretch, high-pass, downward expander, peak-target gain, fades, silence tail), then equal-power crossfade between chunks or real silence at sentence boundaries. A single module-level lock serializes the GPU.

## Compose stack (`server/docker/docker-compose.yaml`)

Eleven services. `llm`, `stt`, `tts`, and `dcgm-exporter` share the GPU without partitioning. The orchestrator depends on all four inference and tool services with `condition: service_healthy`; STT and TTS wait on the LLM because it is the slowest to load. The orchestrator additionally runs its own readiness poll that names failing checks, since a compose healthcheck can pass before a service is usable. Prometheus scrapes the host-network services through `host.docker.internal`; Promtail uses Docker service discovery into Loki; Grafana provisions datasources and two dashboards.

## Speaker client (`speaker/modules/speaker/`)

**Supervision.** `orchestrator.py` wires seven tasks, each under `_supervise()`: exponential-backoff restart, clean stop on the shared event. Shared state is constructor-injected; there are no module globals.

**Fan-out.** `tee_mic()` is the single point where mic frames split to the wakeword, utterance, and barge-in queues, and it holds the echo policy: during playback only the barge-in queue is fed; for a guard window after playback frames are discarded; optional echo suppression attenuates the mic when a playback chunk was seen within 250 ms and mic RMS is below 1.5x playback RMS.

**Capture.** `capture_streamer.py`: clear barge state, drain stale frames, wait out the post-playback skip, wait for settled silence with a deadline, then stream VAD-gated frames with a pre-roll so the first syllable is not clipped. A turn ends only on silence hangover plus committed speech plus a minimum duration, so a cough cannot end it.

**VAD.** `vad.py`: RMS with an adaptive threshold clamped between a floor and a ceiling; the ceiling switches up in a noisy room. The noise floor updates only on non-speech frames, survives `reset()`, and is persisted to disk between runs. Two instances run so barge-in detection cannot perturb utterance state.

**Barge-in.** `_barge_in_monitor` runs only while playing and not capturing. It drains stale frames at playback start, ignores the first 600 ms as a chime guard, and requires 80 ms of sustained speech. On trigger it saves the pre-buffer as the new turn's context, drains the playback queue, and backs off until capture starts. The cancel then propagates to the turn, the playback worker, and the gRPC stream.

**Playback.** `playback_worker.py` stashes chunks belonging to another turn instead of dropping them, and clears the stash on barge-in so a cancelled turn's audio cannot play ahead of the new answer. Backpressure above a high-water mark drops non-final chunks but preserves `is_last` so the turn terminates. ALSA writes happen on a dedicated thread behind a bounded queue; the writer thread and queue are recreated on format change. Optional `SCHED_FIFO`, affinity, and nice for audio processes.

**gRPC client.** `grpc_client.py`: one persistent `Converse` stream, a receive loop that pushes a `None` sentinel and marks disconnected in its `finally`. `connect()` awaits the old receive loop before draining queues, because otherwise a stale sentinel aborts the next turn (the reasoning is in a comment). Keepalive is tuned for LAN. Transmit stops early on `THINKING` or on the first TTS chunk, with a guard against duplicate end-of-utterance.

**Config.** A flat dataclass of about 70 fields. `from_yaml` warns on unknown keys and overlays a named benchmark profile (`latency`, `balanced`, `robust`, `noisy`) from `config/benchmarks.yaml`, so the latency-versus-robustness tradeoff is a one-line change.

## Hardware (`speaker/hardware/`, `speaker/overlays/`, `speaker/xvf3800/`)

`overlays/xvf3800-jab5-i2s.dts` makes the Pi the I2S bitclock and frame master for two slaves on one bus with no MCLK, exposing capture and playback as one ALSA card. `xvf3800/` holds the bring-up scripts and notes that got there, including two earlier overlay approaches. `hardware/pcb/theboard/` is revision 3.7 of a CM5 carrier board that replaces the Geekworm X1500 plus separate breakouts: internal 28 V supply, ribbon header to the mic array, JST I2S link to the amplifier. `specs.md` is the design brief; the KiCad schematic, board, and gerbers are included.

## Known rough edges

Left in place rather than tidied for publication.

- `modules/common/utils/batching.py` (`TextBatcher`) is exported and its config is plumbed through, but nothing instantiates it. TTS owns chunking now.
- `ENERGY_BARGE_IN` is defined and logged but unused; server-side barge-in is byte-count based.
- `ORCH_TOOL_PARALLELISM` defaults to 1 in `settings.py` and 8 in `config.yaml`.
- The speaker container healthcheck (`docker/healthcheck.py`) expects fields the health monitor does not currently write.
- `server/test/test_orchestrator_improvements.py` puts `modules/orchestrator` first on `sys.path`, so `tools` resolves to the orchestrator's MCP client package rather than `modules/tools`, and all four tests skip with an import error.
- The gRPC stream handler, tool loop, MCP proxy, and session eviction have no automated tests; they are covered by the manual harnesses in `server/test/` and `server/scripts/` and by the `/readyz` and `/selfcheck` endpoints. Pure functions (text formatting, config coercion, VAD math, state machine, event bus) are unit tested.
