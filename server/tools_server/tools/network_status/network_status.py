import logging
import socket
import time
from pathlib import Path
from typing import Annotated, Any, List

import httpx
from pydantic import Field

from utils.tool_config import load_tool_config
from utils.tool_logging import log_tool_call, log_tool_result
from utils.tool_meta import get_param_meta, get_tool_config


def _load_config() -> dict[str, Any]:
    return load_tool_config(__file__)


def _check_tcp(host: str, port: int, timeout_s: float) -> tuple[bool, float]:
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            latency_ms = (time.perf_counter() - start) * 1000.0
            return True, latency_ms
    except OSError:
        return False, (time.perf_counter() - start) * 1000.0


def _best_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 53))
            return str(s.getsockname()[0] or "")
    except OSError:
        return ""


def _default_gateway() -> str:
    route_path = Path("/proc/net/route")
    if not route_path.exists():
        return ""
    try:
        lines = route_path.read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return ""
    for line in lines:
        cols = line.split()
        if len(cols) < 3:
            continue
        destination, gateway = cols[1], cols[2]
        if destination != "00000000":
            continue
        try:
            raw = bytes.fromhex(gateway)
            return socket.inet_ntoa(raw[::-1])
        except (OSError, ValueError):
            return ""
    return ""


def _dns_ok(timeout_s: float) -> tuple[bool, float]:
    start = time.perf_counter()
    try:
        socket.getaddrinfo("example.com", 443, proto=socket.IPPROTO_TCP)
        return True, (time.perf_counter() - start) * 1000.0
    except OSError:
        return False, (time.perf_counter() - start) * 1000.0


def _http_ok(timeout_s: float) -> tuple[bool, int]:
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.get("https://www.gstatic.com/generate_204")
            return resp.status_code in {200, 204}, int(resp.status_code)
    except Exception:
        return False, 0


def register(server) -> List[str]:
    log = logging.getLogger("tools.network_status")
    config = _load_config()
    default_target = str(config.get("default_target") or "1.1.1.1")
    default_timeout_ms = int(config.get("default_timeout_ms") or 1200)

    tool_description, param_meta = get_tool_config(
        config,
        "network_status",
        "Quick network health checks for connectivity, DNS, and gateway reachability.",
    )
    action_desc, action_alias, action_title = get_param_meta(
        param_meta,
        "action",
        "Action: summary, internet, dns, gateway.",
    )
    target_desc, target_alias, target_title = get_param_meta(
        param_meta,
        "target",
        "Optional target host for internet checks.",
    )
    timeout_desc, timeout_alias, timeout_title = get_param_meta(
        param_meta,
        "timeout_ms",
        "Optional timeout in milliseconds.",
    )

    def _field_kwargs(description: str, alias: str | None, title: str | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"description": description}
        if alias:
            kwargs["alias"] = alias
        if title:
            kwargs["title"] = title
        return kwargs

    @server.tool(
        name="network_status",
        description=tool_description,
    )
    def network_status(
        action: Annotated[str, Field(**_field_kwargs(action_desc, action_alias, action_title))] = "summary",
        target: Annotated[str | None, Field(**_field_kwargs(target_desc, target_alias, target_title))] = None,
        timeout_ms: Annotated[int | None, Field(**_field_kwargs(timeout_desc, timeout_alias, timeout_title))] = None,
    ) -> str:
        log_tool_call(log, "network_status", action=action, target=target, timeout_ms=timeout_ms)
        action_value = str(action or "summary").strip().lower()
        target_host = str(target or default_target).strip() or default_target
        timeout_s = max(0.2, float((timeout_ms if isinstance(timeout_ms, int) else default_timeout_ms)) / 1000.0)
        local_ip = _best_local_ip()
        gateway_ip = _default_gateway()

        if action_value == "gateway":
            if not gateway_ip:
                return log_tool_result(log, "network_status", "Default gateway not detected.")
            ok, latency_ms = _check_tcp(gateway_ip, 53, timeout_s)
            status = "reachable" if ok else "unreachable"
            return log_tool_result(
                log,
                "network_status",
                f"Gateway {gateway_ip} is {status} ({latency_ms:.1f} ms).",
            )

        if action_value == "dns":
            ok, latency_ms = _dns_ok(timeout_s)
            status = "ok" if ok else "failed"
            return log_tool_result(log, "network_status", f"DNS resolution {status} ({latency_ms:.1f} ms).")

        if action_value == "internet":
            tcp_ok, tcp_ms = _check_tcp(target_host, 53, timeout_s)
            http_ok, http_code = _http_ok(timeout_s)
            return log_tool_result(
                log,
                "network_status",
                (
                    f"Internet check target={target_host}: tcp53={'ok' if tcp_ok else 'fail'} ({tcp_ms:.1f} ms), "
                    f"http={'ok' if http_ok else 'fail'} status={http_code or 'n/a'}."
                ),
            )

        if action_value != "summary":
            return log_tool_result(
                log,
                "network_status",
                "Unsupported action. Use one of: summary, internet, dns, gateway.",
            )

        dns_ok, dns_ms = _dns_ok(timeout_s)
        tcp_ok, tcp_ms = _check_tcp(target_host, 53, timeout_s)
        http_ok, http_code = _http_ok(timeout_s)
        gateway_line = "unknown"
        if gateway_ip:
            gw_ok, gw_ms = _check_tcp(gateway_ip, 53, timeout_s)
            gateway_line = f"{gateway_ip} {'ok' if gw_ok else 'fail'} ({gw_ms:.1f} ms)"
        lines = [
            "Network summary:",
            f"- local_ip: {local_ip or 'unavailable'}",
            f"- gateway: {gateway_line}",
            f"- dns: {'ok' if dns_ok else 'fail'} ({dns_ms:.1f} ms)",
            f"- internet_tcp53({target_host}): {'ok' if tcp_ok else 'fail'} ({tcp_ms:.1f} ms)",
            f"- internet_http: {'ok' if http_ok else 'fail'} status={http_code or 'n/a'}",
        ]
        return log_tool_result(log, "network_status", "\n".join(lines))

    return ["network_status"]

