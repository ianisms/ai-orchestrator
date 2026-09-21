from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from hypercorn.asyncio import serve
from hypercorn.config import Config
from mcp.server.fastmcp import FastMCP
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification, ToolListChangedNotification
from starlette.responses import JSONResponse, Response

from loader import ToolLoader
from mcp_proxy import MCPProxyManager, resolve_config_path

from utils import metrics
import yaml


def _read_yaml(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).upper(): v for k, v in data.items()}


def _apply_config_env() -> None:
    global_path = Path(os.getenv("TOOLS_GLOBAL_CONFIG_PATH", "/ai/server/config/config.yaml"))
    component_path = Path(os.getenv("TOOLS_CONFIG_PATH", str(Path(__file__).parent / "config.yaml")))
    global_cfg = _read_yaml(global_path)
    component_cfg = _read_yaml(component_path)
    config = {**global_cfg, **component_cfg}
    if "LOG_LEVEL" not in os.environ and "LOG_LEVEL" in config:
        os.environ["LOG_LEVEL"] = str(config["LOG_LEVEL"])
    for key in ("TOOLS_MCP_HOST", "TOOLS_MCP_PORT", "TOOLS_DIR", "TOOLS_MCP_SERVERS_CONFIG"):
        if key not in os.environ and key in config:
            os.environ[key] = str(config[key])


def _setup_logging() -> logging.Logger:
    level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("tools.server")


async def main() -> None:
    _apply_config_env()
    log = _setup_logging()

    host = os.getenv("TOOLS_MCP_HOST", "0.0.0.0")
    port = int(os.getenv("TOOLS_MCP_PORT", "9102"))
    tools_dir = Path(os.getenv("TOOLS_DIR", str(Path(__file__).parent / "tools")))

    server = FastMCP(
        name="tools",
        host=host,
        port=port,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        streamable_http_path="/mcp",
    )

    @server.custom_route("/healthz", methods=["GET"])
    async def health_check(_request):
        return JSONResponse({"status": "ok"})

    @server.custom_route("/metrics", methods=["GET"])
    async def metrics_route(_request):
        return Response(
            metrics.metrics_payload(),
            media_type=metrics.metrics_content_type(),
        )

    @server.custom_route("/stats/reset", methods=["POST"])
    async def stats_reset(_request):
        metrics.reset_uptime()
        return JSONResponse({"status": "ok"})

    @server.custom_route("/stats/reset_lifetime", methods=["POST"])
    async def stats_reset_lifetime(_request):
        metrics.reset_all()
        return JSONResponse({"status": "ok"})

    async def notify_tool_list_changed() -> None:
        session_manager = server._session_manager
        if session_manager is None:
            return
        transports = list(session_manager._server_instances.values())
        if not transports:
            return
        notice = ToolListChangedNotification()
        jsonrpc_notice = JSONRPCNotification(
            jsonrpc="2.0",
            **notice.model_dump(by_alias=True, mode="json", exclude_none=True),
        )
        for transport in transports:
            write_stream = getattr(transport, "_write_stream", None)
            if write_stream is None:
                continue
            message = SessionMessage(message=JSONRPCMessage(jsonrpc_notice))
            await write_stream.send(message)
        log.info("tool list changed notified sessions=%d", len(transports))

    app = server.streamable_http_app()

    loader = ToolLoader(server, tools_dir=tools_dir, logger=log, notify_changed=notify_tool_list_changed)
    await loader.load_all()
    asyncio.create_task(loader.watch(), name="tools_watch")

    proxy_manager = MCPProxyManager(
        server=server,
        logger=log,
        notify_changed=notify_tool_list_changed,
        config_path=resolve_config_path(Path(__file__).parent / "mcp-servers.yaml"),
    )
    await proxy_manager.start()

    config = Config()
    config.bind = [f"{host}:{port}"]
    config.h2c = True
    config.use_reloader = False

    log.info("tools server listening on %s:%s", host, port)
    await serve(app, config)


if __name__ == "__main__":
    asyncio.run(main())
