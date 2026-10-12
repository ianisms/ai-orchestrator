from __future__ import annotations

import anyio
import asyncio
import contextlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import mcp
import yaml
from mcp.client.session import ClientSession
from mcp.client.streamable_http import (
    DEFAULT_RECONNECTION_DELAY_MS,
    MAX_RECONNECTION_ATTEMPTS,
    StreamableHTTPTransport,
    aconnect_sse,
)
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.message import SessionMessage
from mcp.types import (
    CallToolResult,
    Implementation,
    Tool,
    ToolAnnotations,
    ToolListChangedNotification,
    ListToolsResult,
)
from mcp.types import TextContent as MCPTextContent


class MethodStreamableHTTPTransport(StreamableHTTPTransport):
    def __init__(self, url: str, sse_method: str = "GET") -> None:
        super().__init__(url)
        self._sse_method = (sse_method or "GET").upper()

    async def handle_get_stream(
        self,
        client: httpx.AsyncClient,
        read_stream_writer,
    ) -> None:
        """Handle streaming of server-initiated messages, honoring custom method."""
        last_event_id: str | None = None
        retry_interval_ms: int | None = None
        attempt: int = 0

        while attempt < MAX_RECONNECTION_ATTEMPTS:
            try:
                if not self.session_id:
                    return

                headers = self._prepare_headers()
                if last_event_id:
                    headers["last-event-id"] = last_event_id

                async with aconnect_sse(
                    client,
                    self._sse_method,
                    self.url,
                    headers=headers,
                ) as event_source:
                    event_source.response.raise_for_status()
                    while True:
                        try:
                            sse = await event_source.__anext__()
                        except StopAsyncIteration:
                            break
                        if sse.id:
                            last_event_id = sse.id
                        if sse.retry is not None:
                            retry_interval_ms = sse.retry
                        await self._handle_sse_event(sse, read_stream_writer)

                attempt = 0

            except Exception as exc:
                logging.debug(f"GET stream error: {exc}")
                attempt += 1

            if attempt >= MAX_RECONNECTION_ATTEMPTS:
                logging.debug(f"GET stream max reconnection attempts ({MAX_RECONNECTION_ATTEMPTS}) exceeded")
                return

            delay_ms = retry_interval_ms if retry_interval_ms is not None else DEFAULT_RECONNECTION_DELAY_MS
            logging.info(f"GET stream disconnected, reconnecting in {delay_ms}ms...")
            await anyio.sleep(delay_ms / 1000.0)


@contextlib.asynccontextmanager
async def streamable_http_client_with_method(
    url: str,
    *,
    sse_method: str = "GET",
    http_client: httpx.AsyncClient | None = None,
    terminate_on_close: bool = True,
):
    """Variant of streamable_http_client that allows customizing the SSE method."""
    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

    read_stream_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream[SessionMessage](0)

    client_provided = http_client is not None
    client = http_client or create_mcp_http_client()

    transport = MethodStreamableHTTPTransport(url, sse_method=sse_method)

    async with anyio.create_task_group() as tg:
        try:
            async with contextlib.AsyncExitStack() as stack:
                if not client_provided:
                    await stack.enter_async_context(client)

                def start_get_stream() -> None:
                    tg.start_soon(transport.handle_get_stream, client, read_stream_writer)

                tg.start_soon(
                    transport.post_writer,
                    client,
                    write_stream_reader,
                    read_stream_writer,
                    write_stream,
                    start_get_stream,
                    tg,
                )

                try:
                    yield (
                        read_stream,
                        write_stream,
                        transport.get_session_id,
                    )
                finally:
                    if transport.session_id and terminate_on_close:
                        await transport.terminate_session(client)
                    tg.cancel_scope.cancel()
        finally:
            await read_stream_writer.aclose()
            await write_stream.aclose()


@dataclass
class ToolOverride:
    description: str | None
    params: dict[str, Any] | None


@dataclass
class MCPServerConfig:
    name: str
    url: str
    allowed_tools: set[str]
    denied_tools: set[str]
    headers: dict[str, str]
    verify_ssl: bool
    sse_method: str
    added_prompts: list[str]
    tool_overrides: dict[str, ToolOverride]


def _coerce_str_set(value: object) -> set[str]:
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def _expand_env(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def load_mcp_servers_config(path: Path, logger: logging.Logger) -> list[MCPServerConfig]:
    if not path.exists():
        logger.info("MCP servers config not found: %s", path)
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse MCP servers config %s error=%r", path, exc)
        return []
    servers_raw = data.get("mcp_servers")
    if not isinstance(servers_raw, list):
        logger.warning("mcp_servers key missing or invalid in %s", path)
        return []

    configs: list[MCPServerConfig] = []
    for entry in servers_raw:
        if not isinstance(entry, dict):
            logger.warning("Skipping MCP server entry that is not a mapping: %r", entry)
            continue
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not name or not url:
            logger.warning("Skipping MCP server entry missing name or url: %r", entry)
            continue
        allowed = _coerce_str_set(entry.get("allowed_tools"))
        denied = _coerce_str_set(entry.get("denied_tools"))
        headers_raw = entry.get("headers") if isinstance(entry.get("headers"), dict) else {}
        headers: dict[str, str] = {}
        for key, value in headers_raw.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            headers[key] = _expand_env(value)
        verify_ssl = bool(entry.get("verify_ssl", True))
        sse_method = str(entry.get("sse_method") or "GET").strip() or "GET"
        added_prompts_raw = entry.get("added_prompts") if isinstance(entry.get("added_prompts"), list) else []
        added_prompts: list[str] = []
        for prompt in added_prompts_raw:
            if isinstance(prompt, str) and prompt.strip():
                added_prompts.append(prompt.strip())
        overrides_raw = entry.get("overrides") if isinstance(entry.get("overrides"), dict) else {}
        tools_override_raw = (
            overrides_raw.get("tools") if isinstance(overrides_raw.get("tools"), dict) else {}
        )
        tool_overrides: dict[str, ToolOverride] = {}
        for tool_name, override_raw in tools_override_raw.items():
            if not isinstance(tool_name, str) or not tool_name.strip():
                continue
            if not isinstance(override_raw, dict):
                continue
            description_raw = override_raw.get("description")
            description = _expand_env(description_raw.strip()) if isinstance(description_raw, str) else None
            params_raw = override_raw.get("params")
            if not isinstance(params_raw, dict):
                params_raw = override_raw.get("parameters")
            params = params_raw if isinstance(params_raw, dict) else None
            if description is None and params is None:
                continue
            tool_overrides[tool_name.strip()] = ToolOverride(description=description, params=params)
        configs.append(
            MCPServerConfig(
                name=_expand_env(name),
                url=_expand_env(url),
                allowed_tools=allowed,
                denied_tools=denied,
                headers=headers,
                verify_ssl=verify_ssl,
                sse_method=sse_method,
                added_prompts=added_prompts,
                tool_overrides=tool_overrides,
            )
        )
    return configs


class ProxyTool:
    def __init__(
        self,
        *,
        proxy_name: str,
        remote_name: str,
        server_name: str,
        url: str,
        description: str,
        parameters: dict[str, Any],
        output_schema: dict[str, Any] | None,
        annotations: ToolAnnotations | None,
        icons: list[Any] | None,
        meta: dict[str, Any] | None,
        session: ClientSession | None,
        logger: logging.Logger,
        signature: str,
    ) -> None:
        self.name = proxy_name
        self.title = None
        self.description = description
        self.parameters = parameters
        self.output_schema = output_schema
        self.annotations = annotations
        self.icons = icons
        self.meta = meta
        self._remote_name = remote_name
        self._server_name = server_name
        self._url = url
        self._session = session
        self._logger = logger
        self.signature = signature

    def update(
        self,
        *,
        description: str,
        parameters: dict[str, Any],
        output_schema: dict[str, Any] | None,
        annotations: ToolAnnotations | None,
        icons: list[Any] | None,
        meta: dict[str, Any] | None,
        session: ClientSession | None,
        signature: str,
    ) -> None:
        self.description = description
        self.parameters = parameters
        self.output_schema = output_schema
        self.annotations = annotations
        self.icons = icons
        self.meta = meta
        self._session = session
        self.signature = signature

    async def run(self, arguments: dict[str, Any], context: Any = None, convert_result: bool = False) -> CallToolResult:
        session = self._session
        if session is None:
            message = f"Remote server '{self._server_name}' is not connected."
            self._logger.warning("proxy tool unavailable server=%s tool=%s", self._server_name, self._remote_name)
            return CallToolResult(content=[MCPTextContent(type="text", text=message)], isError=True)
        try:
            result = await session.call_tool(self._remote_name, arguments)
            self._logger.debug(
                "proxy tool call server=%s tool=%s is_error=%s",
                self._server_name,
                self._remote_name,
                result.isError,
            )
            return result
        except Exception as exc:
            message = f"Remote tool '{self._remote_name}' on '{self._server_name}' failed: {exc}"
            self._logger.warning(message)
            return CallToolResult(content=[MCPTextContent(type="text", text=message)], isError=True)


class RemoteMCPServer:
    def __init__(
        self,
        *,
        config: MCPServerConfig,
        server,
        logger: logging.Logger,
        notify_changed: Callable[[], Awaitable[None]] | None,
        retry_seconds: float = 10.0,
    ) -> None:
        self._config = config
        self._server = server
        self._logger = logger.getChild(f"remote.{config.name}")
        self._notify_changed = notify_changed
        self._retry_seconds = retry_seconds
        self._task: asyncio.Task | None = None
        self._proxies: dict[str, ProxyTool] = {}
        self._lock = asyncio.Lock()
        self._running = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name=f"mcp_remote_{self._config.name}")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._clear_proxies()

    async def _run(self) -> None:
        while self._running:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning("remote server loop error: %r", exc)
            await self._clear_proxies()
            await asyncio.sleep(self._retry_seconds)

    async def _connect_once(self) -> None:
        closed_event = asyncio.Event()

        def _log_http_error(exc: Exception, msg: str) -> None:
            resp = getattr(exc, "response", None)
            if resp is None:
                return
            try:
                body = resp.text
            except Exception:
                body = "<unavailable>"
            self._logger.debug(
                "%s status=%s reason=%s body=%s",
                msg,
                getattr(resp, "status_code", "unknown"),
                getattr(resp, "reason_phrase", None) or getattr(resp, "reason", None) or "",
                str(body)[:400],
            )

        def _status_reason(exc: Exception) -> str:
            resp = getattr(exc, "response", None)
            if resp is None:
                return ""
            return f" status={getattr(resp, 'status_code', 'unknown')} reason={(getattr(resp, 'reason_phrase', None) or getattr(resp, 'reason', None) or '').strip()}"

        async def _message_handler(message: Any) -> None:
            if isinstance(message, mcp.types.ServerNotification) and isinstance(
                message.root, ToolListChangedNotification
            ):
                await self._refresh_tools(session)
                return
            if isinstance(message, Exception):
                self._logger.warning("remote server %s connection error: %r", self._config.name, message)
                closed_event.set()

        async with contextlib.AsyncExitStack() as stack:
            timeout = httpx.Timeout(30.0, read=300.0)
            if self._config.verify_ssl:
                http_client = create_mcp_http_client(
                    headers=self._config.headers or None,
                    timeout=timeout,
                )
            else:
                http_client = httpx.AsyncClient(
                    follow_redirects=True,
                    headers=self._config.headers or None,
                    timeout=timeout,
                    verify=False,
                )
                self._logger.warning(
                    "SSL verification disabled for remote MCP server name=%s url=%s",
                    self._config.name,
                    self._config.url,
                )
            await stack.enter_async_context(http_client)

            client = streamable_http_client_with_method(
                url=self._config.url,
                sse_method=self._config.sse_method,
                http_client=http_client,
                terminate_on_close=True,
            )
            try:
                read, write, _ = await stack.enter_async_context(client)
            except Exception as exc:
                self._logger.warning(
                    "failed to connect to remote MCP server name=%s url=%s error=%r%s",
                    self._config.name,
                    self._config.url,
                    exc,
                    _status_reason(exc),
                )
                _log_http_error(exc, "connect failed")
                return

            try:
                session = await stack.enter_async_context(
                    ClientSession(
                        read,
                        write,
                        message_handler=_message_handler,
                        client_info=Implementation(name="tools-proxy", version="0.1.0"),
                    )
                )
                init_result = await session.initialize()
            except Exception as exc:
                self._logger.warning(
                    "failed to initialize remote MCP session name=%s url=%s error=%r%s",
                    self._config.name,
                    self._config.url,
                    exc,
                    _status_reason(exc),
                )
                _log_http_error(exc, "initialize failed")
                return
            server_info = init_result.serverInfo
            self._logger.info(
                "connected to remote MCP server name=%s version=%s url=%s",
                self._config.name,
                getattr(server_info, "version", "unknown"),
                self._config.url,
            )
            await self._refresh_tools(session)

            ping_task = asyncio.create_task(self._ping_loop(session, closed_event), name=f"ping_{self._config.name}")
            try:
                await closed_event.wait()
            finally:
                ping_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ping_task

    async def _ping_loop(self, session: ClientSession, closed_event: asyncio.Event) -> None:
        while not closed_event.is_set():
            try:
                await asyncio.wait_for(closed_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                try:
                    await session.send_ping()
                except Exception as exc:
                    self._logger.warning("ping to %s failed: %r", self._config.name, exc)
                    closed_event.set()

    def _should_include(self, tool_name: str) -> bool:
        if self._config.denied_tools and tool_name in self._config.denied_tools:
            return False
        if self._config.allowed_tools and tool_name not in self._config.allowed_tools:
            return False
        return True

    def _proxy_name(self, remote_tool_name: str) -> str:
        prefix = re.sub(r"[^a-zA-Z0-9]+", "_", self._config.name).strip("_").lower() or "remote"
        tool_part = re.sub(r"[^a-zA-Z0-9]+", "_", remote_tool_name).strip("_") or "tool"
        return f"{prefix}__{tool_part}"

    def _tool_signature(
        self,
        *,
        description: str,
        parameters: dict[str, Any],
        output_schema: dict[str, Any] | None,
        annotations: ToolAnnotations | None,
        meta: dict[str, Any] | None,
    ) -> str:
        return json.dumps(
            {
                "description": description,
                "input": parameters,
                "output": output_schema,
                "annotations": annotations.model_dump() if annotations else None,
                "meta": meta,
            },
            sort_keys=True,
            default=str,
        )

    def _build_description(self, tool: Tool, base_description: str | None = None) -> str:
        base = base_description or tool.description or "Remote tool."
        prompt_block = ""
        if self._config.added_prompts:
            prompt_lines = "\n".join(f"- {p}" for p in self._config.added_prompts)
            prompt_block = f"\n\nAdditional guidance:\n{prompt_lines}"
        return f"{base}{prompt_block}"

    def _build_meta(self, tool: Tool) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        if isinstance(tool.meta, dict):
            meta = dict(tool.meta)
        meta.setdefault("_source", {})
        if isinstance(meta["_source"], dict):
            meta["_source"].setdefault("server", self._config.name)
            meta["_source"].setdefault("url", self._config.url)
            meta["_source"].setdefault("tool", tool.name)
            if self._config.added_prompts:
                meta["_source"].setdefault("added_prompts", self._config.added_prompts)
        return meta

    def _resolve_override(self, remote_name: str, proxy_name: str) -> ToolOverride | None:
        overrides = self._config.tool_overrides
        if not overrides:
            return None
        return overrides.get(proxy_name) or overrides.get(remote_name)

    async def _refresh_tools(self, session: ClientSession) -> None:
        async with self._lock:
            try:
                tool_result: ListToolsResult = await session.list_tools()
            except Exception as exc:
                self._logger.warning("failed to list tools for %s: %r", self._config.name, exc)
                return

            tools = tool_result.tools
            filtered: dict[str, tuple[Tool, str]] = {}
            for tool in tools:
                if not self._should_include(tool.name):
                    self._logger.debug(
                        "tool filtered server=%s tool=%s allowed=%s denied=%s",
                        self._config.name,
                        tool.name,
                        bool(self._config.allowed_tools),
                        bool(self._config.denied_tools),
                    )
                    continue
                proxy_name = self._proxy_name(tool.name)
                override = self._resolve_override(tool.name, proxy_name)
                base_description = override.description if override and override.description else tool.description
                description = self._build_description(tool, base_description)
                parameters = (
                    override.params
                    if override and override.params is not None
                    else tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
                )
                output_schema = tool.outputSchema if isinstance(tool.outputSchema, dict) else None
                annotations = tool.annotations if isinstance(tool.annotations, ToolAnnotations) else None
                icons = tool.icons if isinstance(tool.icons, list) else None
                meta = self._build_meta(tool)
                signature = self._tool_signature(
                    description=description,
                    parameters=parameters,
                    output_schema=output_schema,
                    annotations=annotations,
                    meta=meta,
                )
                if override:
                    override_payload = {
                        "description": override.description,
                        "params": override.params,
                    }
                    self._logger.debug(
                        "applied tool override server=%s tool=%s proxy=%s override=%s",
                        self._config.name,
                        tool.name,
                        proxy_name,
                        json.dumps(override_payload, sort_keys=True, default=str),
                    )
                filtered[proxy_name] = (tool, signature, description, parameters, output_schema, annotations, icons, meta)

            changed = False
            to_remove = set(self._proxies.keys()) - set(filtered.keys())
            for name in to_remove:
                if await self._unregister_proxy(name):
                    changed = True

            for proxy_name, (
                tool,
                signature,
                description,
                parameters,
                output_schema,
                annotations,
                icons,
                meta,
            ) in filtered.items():
                existing = self._proxies.get(proxy_name)
                if existing:
                    if existing.signature != signature or existing._session is not session:
                        existing.update(
                            description=description,
                            parameters=parameters,
                            output_schema=output_schema,
                            annotations=annotations,
                            icons=icons,
                            meta=meta,
                            session=session,
                            signature=signature,
                        )
                        self._logger.info(
                            "updated proxy tool server=%s remote_tool=%s name=%s",
                            self._config.name,
                            tool.name,
                            proxy_name,
                        )
                        changed = True
                    continue

                if self._server._tool_manager.get_tool(proxy_name) is not None:
                    self._logger.warning(
                        "skipping proxy tool due to name collision name=%s server=%s remote_tool=%s",
                        proxy_name,
                        self._config.name,
                        tool.name,
                    )
                    continue

                proxy_tool = ProxyTool(
                    proxy_name=proxy_name,
                    remote_name=tool.name,
                    server_name=self._config.name,
                    url=self._config.url,
                    description=description,
                    parameters=parameters,
                    output_schema=output_schema,
                    annotations=annotations,
                    icons=icons,
                    meta=meta,
                    session=session,
                    logger=self._logger,
                    signature=signature,
                )
                self._server._tool_manager._tools[proxy_name] = proxy_tool
                self._proxies[proxy_name] = proxy_tool
                self._logger.info(
                    "registered proxy tool server=%s remote_tool=%s as=%s",
                    self._config.name,
                    tool.name,
                    proxy_name,
                )
                changed = True

            if changed and self._notify_changed is not None:
                await self._notify_changed()

    async def _unregister_proxy(self, name: str) -> bool:
        proxy = self._proxies.pop(name, None)
        if proxy is None:
            return False
        try:
            self._server.remove_tool(name)
            self._logger.info("removed proxy tool name=%s server=%s", name, self._config.name)
            return True
        except Exception as exc:
            self._logger.warning("failed to remove proxy tool %s: %r", name, exc)
            return False

    async def _clear_proxies(self) -> None:
        if not self._proxies:
            return
        async with self._lock:
            removed_any = False
            for name in list(self._proxies.keys()):
                if await self._unregister_proxy(name):
                    removed_any = True
            if removed_any and self._notify_changed is not None:
                await self._notify_changed()


class MCPProxyManager:
    def __init__(
        self,
        *,
        server,
        logger: logging.Logger,
        notify_changed: Callable[[], Awaitable[None]] | None,
        config_path: Path,
    ) -> None:
        self._server = server
        self._logger = logger.getChild("mcp_proxy")
        self._notify_changed = notify_changed
        self._config_path = config_path
        self._remotes: list[RemoteMCPServer] = []

    async def start(self) -> None:
        configs = load_mcp_servers_config(self._config_path, self._logger)
        if not configs:
            self._logger.info("No remote MCP servers configured.")
            return
        for cfg in configs:
            remote = RemoteMCPServer(
                config=cfg,
                server=self._server,
                logger=self._logger,
                notify_changed=self._notify_changed,
            )
            await remote.start()
            self._remotes.append(remote)

    async def stop(self) -> None:
        for remote in self._remotes:
            await remote.stop()


def resolve_config_path(default_path: Path | None = None) -> Path:
    env_value = os.getenv("TOOLS_MCP_SERVERS_CONFIG")
    if env_value:
        return Path(env_value)
    if default_path is not None:
        return default_path
    return Path("mcp-servers.yaml")
