"""Operator CLI for the STS admin routes (prototype).

    STS_URL=http://127.0.0.1:8099 STS_CLIENT_OPS_CONSOLE=<secret> \\
        python admin.py revoke-grant <grant_id> | revoke-client <client_id> | enable-client <client_id>

Also runs as `python -m delegation.admin ...` from security/ (namespace package,
no __init__.py needed). It obtains an ops-console actor token with the client
secret from the environment, then calls the route with it as a bearer. Nothing
is printed but the server's JSON answer; the secret is never echoed.
"""
from __future__ import annotations

import json
import os
import sys

import httpx

COMMANDS = {
    "revoke-grant": ("/admin/revoke/grant", "grant_id"),
    "revoke-client": ("/admin/revoke/client", "client_id"),
    "enable-client": ("/admin/enable/client", "client_id"),
}


def run(argv: list[str], client: httpx.Client, secret: str) -> int:
    if len(argv) != 2 or argv[0] not in COMMANDS:
        print(f"usage: admin.py ({' | '.join(COMMANDS)}) <id>", file=sys.stderr)
        return 2
    path, field = COMMANDS[argv[0]]
    tok = client.post("/token", data={"grant_type": "client_credentials", "client_id": "ops-console", "client_secret": secret})
    if tok.status_code != 200:
        print(json.dumps(tok.json()), file=sys.stderr)
        return 1
    r = client.post(path, data={field: argv[1]}, headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
    print(json.dumps(r.json(), sort_keys=True))
    return 0 if r.status_code == 200 else 1


def main(argv: list[str] | None = None) -> int:
    secret = os.environ.get("STS_CLIENT_OPS_CONSOLE")
    if not secret:
        print("set STS_CLIENT_OPS_CONSOLE to the ops-console client secret", file=sys.stderr)
        return 2
    base = os.environ.get("STS_URL", "http://127.0.0.1:8099")
    with httpx.Client(base_url=base, timeout=5.0) as client:
        return run(sys.argv[1:] if argv is None else argv, client, secret)


if __name__ == "__main__":
    sys.exit(main())
