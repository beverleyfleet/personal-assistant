"""Replay transport: serves recorded Smartcar v3 responses from a directory instead of the network.

Enabled only when env FLEET_SMARTCAR_REPLAY_DIR is set (used by the stdio smoke test and for demos
without a Smartcar account). Fixtures are shaped from the documented OpenAPI examples, not captured
from a live account. It also enforces the documented request contract so client bugs surface:
  * token: POST form with grant_type=client_credentials, client_id, client_secret
  * every vehicle-API call: Authorization: Bearer <token>
  * signals and commands: sc-user-id header

Files (all JSON):
  token.json                          token response
  connections.json                    GET /v3/connections
  vehicle_<id>.json                   GET /v3/vehicles/<id>
  signals_<id>.json                   GET /v3/vehicles/<id>/signals (and /signals/<code>)
  Timestamps may be written as __HOURS_AGO_<n>__ and are filled in relative to now.
  command_<id>_<security-lock>.json   optional per-vehicle override, else command_ok.json
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx2


def _err(status: int, type_: str, code: str, detail: str) -> httpx2.Response:
    return httpx2.Response(status, json={"errors": [{"status": str(status), "type": type_, "code": code,
                                                     "title": code.title(), "detail": detail}]})


class ReplayTransport(httpx2.AsyncBaseTransport):
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.calls: list[tuple[str, str]] = []

    def _load(self, name: str) -> dict | None:
        p = self.dir / name
        if not p.exists():
            return None
        now = datetime.now(UTC)
        text = re.sub(r"__HOURS_AGO_(\d+)__",
                      lambda m: (now - timedelta(hours=int(m.group(1)))).strftime("%Y-%m-%dT%H:%M:%SZ"), p.read_text())
        return json.loads(text)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if request.url.host == "iam.smartcar.com" and path == "/oauth2/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("grant_type") != "client_credentials" or not form.get("client_id") or not form.get("client_secret"):
                return httpx2.Response(400, json={"error": "invalid_request"})
            return httpx2.Response(200, json=self._load("token.json"))
        if request.url.host != "vehicle.api.smartcar.com" or not path.startswith("/v3/"):
            return _err(404, "RESOURCE_NOT_FOUND", "PATH_NOT_FOUND", f"replay: no route {method} {request.url}")
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return _err(401, "AUTHENTICATION", "UNAUTHORIZED", "The Authorization token is missing or invalid.")
        parts = path.split("/")[2:]  # after /v3
        if parts == ["connections"]:
            return httpx2.Response(200, json=self._load("connections.json") or {"data": [], "meta": {"totalCount": 0}})
        if len(parts) >= 2 and parts[0] == "vehicles":
            vid = parts[1]
            if len(parts) == 2:
                body = self._load(f"vehicle_{vid}.json")
                return httpx2.Response(200, json=body) if body else _err(404, "RESOURCE_NOT_FOUND", "NOT_FOUND", "vehicle")
            if not request.headers.get("sc-user-id"):
                return _err(400, "VALIDATION", "BAD_REQUEST", "sc-user-id header is required")
            if parts[2] == "signals" and len(parts) == 3:
                body = self._load(f"signals_{vid}.json")
                return httpx2.Response(200, json=body) if body else _err(404, "RESOURCE_NOT_FOUND", "NOT_FOUND", "vehicle")
            if parts[2] == "signals" and len(parts) == 4:
                body = self._load(f"signals_{vid}.json") or {"data": []}
                hit = next((x for x in body["data"] if x["attributes"]["code"] == parts[3]), None)
                if hit is None:
                    return _err(404, "RESOURCE_NOT_FOUND", "NOT_FOUND", f"signal {parts[3]}")
                return httpx2.Response(200, json={"data": hit, "included": body.get("included", {})})
            if parts[2] == "commands" and method == "POST":
                cmd = "-".join(parts[3:])
                body = self._load(f"command_{vid}_{cmd}.json") or self._load("command_ok.json")
                body = json.loads(json.dumps(body).replace("__COMMAND__", cmd))
                return httpx2.Response(200, json=body)
        return _err(404, "RESOURCE_NOT_FOUND", "PATH_NOT_FOUND", f"replay: no route {method} {path}")
