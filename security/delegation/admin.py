"""Operator CLI for the STS admin routes (prototype).

    STS_URL=http://127.0.0.1:8099 STS_CLIENT_OPS_CONSOLE=<secret> STS_KEY_OPS_CONSOLE=<path to PEM> \\
        python admin.py revoke-grant <grant_id> | revoke-client <client_id> | enable-client <client_id> | revoke-key <jkt>

Also runs as `python -m delegation.admin ...` from security/ (namespace package,
no __init__.py needed). It obtains an ops-console actor token with the client
secret and a DPoP proof from the console's key (`python dpop.py keygen <file>`
makes one; the STS registers its thumbprint as STS_JKT_OPS_CONSOLE), then calls
the route with the token under the DPoP scheme and a second proof carrying ath
over it. Nothing is printed but the server's JSON answer; the secret and the
key are never echoed.
"""
from __future__ import annotations

import json
import os
import sys

import httpx

from dpop import ath, load_private_key_pem, make_proof

COMMANDS = {
    "revoke-grant": ("/admin/revoke/grant", "grant_id"),
    "revoke-client": ("/admin/revoke/client", "client_id"),
    "enable-client": ("/admin/enable/client", "client_id"),
    "revoke-key": ("/admin/revoke/key", "jkt"),
}


def run(argv: list[str], client: httpx.Client, secret: str, key) -> int:
    if len(argv) != 2 or argv[0] not in COMMANDS:
        print(f"usage: admin.py ({' | '.join(COMMANDS)}) <id>", file=sys.stderr)
        return 2
    path, field = COMMANDS[argv[0]]
    base = str(client.base_url).rstrip("/")
    tok = client.post(
        "/token",
        data={"grant_type": "client_credentials", "client_id": "ops-console", "client_secret": secret},
        headers={"DPoP": make_proof(key, "POST", f"{base}/token")},
    )
    if tok.status_code != 200:
        print(json.dumps(tok.json()), file=sys.stderr)
        return 1
    access = tok.json()["access_token"]
    r = client.post(
        path,
        data={field: argv[1]},
        headers={"Authorization": f"DPoP {access}", "DPoP": make_proof(key, "POST", f"{base}{path}", ath=ath(access))},
    )
    print(json.dumps(r.json(), sort_keys=True))
    return 0 if r.status_code == 200 else 1


def main(argv: list[str] | None = None) -> int:
    secret = os.environ.get("STS_CLIENT_OPS_CONSOLE")
    if not secret:
        print("set STS_CLIENT_OPS_CONSOLE to the ops-console client secret", file=sys.stderr)
        return 2
    key_path = os.environ.get("STS_KEY_OPS_CONSOLE")
    if not key_path:
        print("set STS_KEY_OPS_CONSOLE to the path of the ops-console DPoP private key (PEM)", file=sys.stderr)
        return 2
    with open(key_path, encoding="ascii") as f:
        key = load_private_key_pem(f.read())
    base = os.environ.get("STS_URL", "http://127.0.0.1:8099")
    with httpx.Client(base_url=base, timeout=5.0) as client:
        return run(sys.argv[1:] if argv is None else argv, client, secret, key)


if __name__ == "__main__":
    sys.exit(main())
