"""AuthZEN-shaped policy decision point in front of OPA.

POST /access/v1/evaluation takes an AuthZEN evaluation request and returns
{"decision": bool, "context": {"reason_admin": {"reasons": [...]}}}.
Any OPA failure returns 503 with no decision, so the PEP fails closed.

    OPA_URL=http://127.0.0.1:8181 uvicorn pdp:app --port 8282
"""
from __future__ import annotations

import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

DECISION_PATH = "/v1/data/agent/tools/decision"


def create_app(opa_url: str, timeout_s: float = 0.5) -> FastAPI:
    app = FastAPI(title="agent-tools PDP (AuthZEN facade over OPA)")
    opa = httpx.Client(base_url=opa_url, timeout=timeout_s)

    @app.post("/access/v1/evaluation")
    async def evaluate(req: Request):
        body = await req.json()
        try:
            r = opa.post(DECISION_PATH, json={"input": body})
            r.raise_for_status()
            result = r.json().get("result")
        except (httpx.HTTPError, ValueError) as e:
            return JSONResponse({"error": f"policy engine unavailable: {type(e).__name__}"}, status_code=503)
        if not isinstance(result, dict) or not isinstance(result.get("allow"), bool):
            return JSONResponse({"error": "policy returned no decision"}, status_code=503)
        return {"decision": result["allow"], "context": {"reason_admin": {"reasons": result.get("reasons", [])}}}

    return app


app = create_app(os.environ.get("OPA_URL", "http://127.0.0.1:8181"))
