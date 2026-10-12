"""Minimal MCP tools/call handler showing where A and C sit.

    1. verify the delegated token (A): signature, audience, expiry, act chain
    2. ask the PDP about this call for the whole chain (C), fail closed
    3. only then dispatch

The tools are fakes. In the real gateway, steps 1 and 2 go in the tools/call
path before the call is forwarded upstream.
"""
from __future__ import annotations

from pep import PEP
from verify import DelegationRejected, UpstreamVerifier

FAKE_TOOLS = {
    "github.get_pr": lambda a: {"repo": a["repo"], "pr": a["pr"], "title": "Fix retry jitter"},
    "github.list_files": lambda a: {"repo": a["repo"], "files": ["retry.py", "README.md"]},
    "github.comment": lambda a: {"commented": True},
    "github.merge_pr": lambda a: {"merged": True, "repo": a["repo"], "pr": a["pr"]},
}


class ToolServer:
    def __init__(self, verifier: UpstreamVerifier, pep: PEP):
        self.verifier = verifier
        self.pep = pep

    def handle(self, rpc: dict, bearer: str) -> dict:
        rid = rpc.get("id")
        if rpc.get("method") != "tools/call":
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}}
        try:
            principal = self.verifier.verify(bearer)
        except DelegationRejected as e:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32001, "message": f"unauthorized: {e}"}}

        name = rpc["params"]["name"]
        args = rpc["params"].get("arguments", {})
        d = self.pep.authorize(principal, name, args)
        if not d.allow:
            return {"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32003, "message": "forbidden by policy",
                "data": {"reasons": d.reasons, "request_id": d.request_id},
            }}
        result = FAKE_TOOLS[name](args)
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": str(result)}]}}
