from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import httpx

from common.logging import get_logger
from config.settings import settings
from tools import registry as tools_registry

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import mcp.types as types

_LOG = get_logger("tools.mcp")


class MCPToolClient:
    def __init__(
        self,
        url: str,
        *,
        logger=None,
        retry_delay_s: float = 1.0,
    ) -> None:
        self._url = url
        self._logger = logger or _LOG
        self._retry_delay_s = max(float(retry_delay_s), 0.1)
        self._session: ClientSession | None = None
        self._stop_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._refresh_lock = asyncio.Lock()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="mcp_tools_client")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def wait_ready(self, timeout: float | None = None) -> bool:
        try:
            if timeout is None:
                await self._ready_event.wait()
                return True
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                timeout = httpx.Timeout(30.0, read=60.0)
                async with httpx.AsyncClient(http2=True, timeout=timeout) as client:
                    async with streamable_http_client(self._url, http_client=client) as (
                        read_stream,
                        write_stream,
                        _,
                    ):
                        async with ClientSession(
                            read_stream,
                            write_stream,
                            message_handler=self._handle_message,
                        ) as session:
                            await session.initialize()
                            self._session = session
                            await self.refresh_tools()
                            self._ready_event.set()
                            self._logger.info("MCP tools connected url=%s", self._url)
                            await self._stop_event.wait()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._logger.warning("MCP tools connection failed: %r", exc)
                self._ready_event.clear()
                self._session = None
                await asyncio.sleep(self._retry_delay_s)
            else:
                self._ready_event.clear()
                self._session = None

    async def _handle_message(
        self,
        message: types.ServerNotification | types.ServerRequest | Exception,
    ) -> None:
        if isinstance(message, Exception):
            self._logger.warning("MCP tools message error: %r", message)
            return
        if isinstance(message, types.ServerNotification):
            notice = message.root
            if isinstance(notice, types.ToolListChangedNotification):
                self._logger.info("MCP tools list changed notification")
                # Refresh asynchronously so notification handling does not block
                # on a long list_tools round-trip.
                task = asyncio.create_task(self.refresh_tools(), name="mcp_tools_refresh")

                def _log_refresh_exc(t: "asyncio.Task[bool]") -> None:
                    if not t.cancelled() and t.exception() is not None:
                        self._logger.warning("MCP tools refresh task failed: %r", t.exception())

                task.add_done_callback(_log_refresh_exc)

    async def refresh_tools(self, *, timeout_s: float = 8.0) -> bool:
        session = self._session
        if session is None:
            return False
        async with self._refresh_lock:
            try:
                result = await asyncio.wait_for(session.list_tools(), timeout=max(1.0, float(timeout_s)))
            except asyncio.TimeoutError:
                self._logger.warning("MCP tools refresh timed out after %.1fs", float(timeout_s))
                return False
            except Exception as exc:
                self._logger.warning("MCP tools refresh failed: %r", exc)
                return False
            tools = [self._to_openai_tool(tool) for tool in result.tools]
            changed = tools_registry.set_tools(tools)
            if changed:
                self._logger.info("MCP tools refreshed count=%d", len(tools))
            else:
                self._logger.debug("MCP tools unchanged count=%d", len(tools))
            return True

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        session = self._session
        if session is None:
            return "Tool error: tools server not connected"
        try:
            arg_preview = json.dumps(arguments, ensure_ascii=True)[:500]
        except Exception:
            arg_preview = str(arguments)
        self._logger.debug("MCP tool call start name=%s args=%s", name, arg_preview)
        try:
            # Bypass ClientSession.call_tool validation to avoid extra list_tools round-trips.
            timeout_s = max(2.0, float(settings.ORCH_TOOL_TIMEOUT_S) - 2.0)
            result = await asyncio.wait_for(
                session.send_request(
                    types.ClientRequest(
                        types.CallToolRequest(
                            params=types.CallToolRequestParams(name=name, arguments=arguments),
                        )
                    ),
                    types.CallToolResult,
                    request_read_timeout_seconds=timedelta(seconds=30),
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            self._logger.warning("MCP tool call timeout; retrying with one-off session: %s", name)
            try:
                result = await self._call_tool_one_off(name, arguments)
            except Exception as exc:
                self._logger.warning("MCP tool call retry failed: %s error=%r", name, exc)
                return f"Tool error: {exc!r}"
        except Exception as exc:
            self._logger.warning("MCP tool call failed: %s error=%r", name, exc)
            return f"Tool error: {exc!r}"

        if result.isError:
            self._logger.warning("MCP tool call error: %s", name)
        else:
            self._logger.debug("MCP tool call success name=%s", name)

        parts: list[str] = []
        for block in result.content or []:
            if isinstance(block, types.TextContent):
                if block.text:
                    parts.append(block.text)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
            elif isinstance(block, types.EmbeddedResource):
                parts.append("[tool returned resource output]")
            elif isinstance(block, types.ImageContent):
                parts.append("[tool returned image output]")
            else:
                text = getattr(block, "text", None)
                if isinstance(text, str) and text:
                    parts.append(text)

        if parts:
            return "\n".join(parts).strip()
        structured = result.structuredContent
        if structured is None:
            structured = getattr(result, "structured_content", None)
        if structured is not None:
            return json.dumps(structured, ensure_ascii=True)
        return ""

    async def _call_tool_one_off(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        timeout = httpx.Timeout(10.0, read=20.0)
        async with httpx.AsyncClient(http2=True, timeout=timeout) as client:
            async with streamable_http_client(self._url, http_client=client) as (
                read_stream,
                write_stream,
                _,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await session.send_request(
                        types.ClientRequest(
                            types.CallToolRequest(
                                params=types.CallToolRequestParams(name=name, arguments=arguments),
                            )
                        ),
                        types.CallToolResult,
                        request_read_timeout_seconds=timedelta(seconds=20),
                    )

    def _to_openai_tool(self, tool: types.Tool) -> dict[str, Any]:
        description = tool.description or "No description."
        parameters = tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
        if not parameters:
            parameters = {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": description,
                "parameters": parameters,
            },
        }


_TOOLS_CLIENT: MCPToolClient | None = None


def set_tools_client(client: MCPToolClient | None) -> None:
    global _TOOLS_CLIENT
    _TOOLS_CLIENT = client


def get_tools_client() -> MCPToolClient | None:
    return _TOOLS_CLIENT
